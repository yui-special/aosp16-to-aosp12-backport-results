import argparse
import datetime
import glob
import logging
import os
import shutil
import time
from types import SimpleNamespace

import git
import yaml

from agent.invoke_llm import build_hunk_executor, do_backport, initial_agent
from tools.logger import add_file_handler, logger
from tools.project import Project
from tools.utils import (
    dump_merged_hunks,
    group_patch,
    load_hunk_groups,
    load_merged_hunk_files,
    select_merged_hunks,
    split_patch_indexed,
    uncovered_hunks,
)


def is_commit_valid(commit_id: str, project_dir: str):
    try:
        repo = git.Repo(project_dir)
        repo.commit(commit_id)
        return True
    except git.exc.BadName:
        logger.error(f"Commit id {commit_id} in .yml is invalid.")
        return False


def rev_parse_commit(commit_id: str, project_dir: str):
    try:
        repo = git.Repo(project_dir)
        return repo.git.rev_parse(commit_id)
    except git.exc.BadName:
        logger.error(f"Commit id {commit_id} in .yml is invalid.")
        return False


def load_yml(file_path: str):
    """
    Load YAML configuration from a file and return the data as a SimpleNamespace object.

    Args:
        file_path (str): The path to the YAML file.

    Returns:
        data (SimpleNamespace): The configuration data stored in a SimpleNamespace object.
    """
    with open(file_path, "r") as file:
        config = yaml.safe_load(file)

    data = SimpleNamespace()
    data.project = config.get("project")
    data.project_url = config.get("project_url")
    data.project_dir = config.get("project_dir")
    data.patch_dataset_dir = config.get("patch_dataset_dir")
    data.api_key = (
        os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("LLM_KEY")
        or config.get("deepseek_api_key")
        or config.get("api_key")
        or config.get("openai_key")
    )
    data.llm_base_url = (
        config.get("llm_base_url")
        or os.environ.get("LLM_BASE_URL")
        or "https://api.deepseek.com/v1"
    )
    data.llm_model = (
        config.get("llm_model")
        or os.environ.get("DEEPSEEK_MODEL")
        or "deepseek-flash"
    )
    data.coverage_model = config.get("coverage_model") or data.llm_model
    for name, default, minimum in (
        ('llm_timeout_seconds', 300, 1), ('llm_max_retries', 1, 0),
        ('coverage_retries', 0, 0), ('coverage_review_iterations', 20, 1),
        ('coverage_review_retries', 1, 0), ('final_repair_iterations', 20, 1),
    ):
        value = config.get(name, default)
        if type(value) is not int or value < minimum:
            raise ValueError('%s must be an integer >= %d' % (name, minimum))
        setattr(data, name, value)
    data.tag = config.get("tag")

    data.new_patch = config.get("new_patch", "")
    if not data.new_patch or not data.new_patch:
        logger.error(
            "Please check your configuration to make sure new_patch is correct!\n"
        )
        exit(1)

    data.new_patch_parent = config.get("new_patch_parent", "")
    if not data.new_patch_parent or not data.new_patch_parent:
        logger.error(
            "Please check your configuration to make sure new_patch_parent is correct!\n"
        )
        exit(1)

    data.target_release = config.get("target_release", "")
    if not data.target_release or not data.target_release:
        logger.error(
            "Please check your configuration to make sure target_release is correct!\n"
        )
        exit(1)

    data.error_message = config.get("error_message", "")
    if not data.error_message:
        logger.warning(
            "Dataset without error info which means that this vulnerability may not have PoC\n"
        )

    data.project_dir = os.path.expanduser(
        data.project_dir if data.project_dir.endswith("/") else data.project_dir + "/"
    )
    data.patch_dataset_dir = os.path.expanduser(
        data.patch_dataset_dir
        if data.patch_dataset_dir.endswith("/")
        else data.patch_dataset_dir + "/"
    )
    if not os.path.isdir(data.project_dir):
        logger.error(f"Project directory does not exist: {data.project_dir}")
        exit(1)
    if not os.path.isdir(data.patch_dataset_dir):
        logger.error(
            f"Patch dataset directory does not exist: {data.patch_dataset_dir}"
        )
        exit(1)

    # ---- hunk 分组（可选）--------------------------------------------------
    # 把「互相依赖的 hunk」合并成一组（merged hunk），一组作为一个整体移植。
    # 优先级: 配置项 hunk_groups > dataset 目录下的 *hunk*groups*.json
    groups_path = config.get("hunk_groups", "") or ""
    if groups_path and not os.path.isabs(groups_path):
        groups_path = os.path.join(data.patch_dataset_dir, groups_path)
    if not groups_path:
        candidates = sorted(
            glob.glob(os.path.join(data.patch_dataset_dir, "*hunk*groups*.json"))
        )
        groups_path = candidates[0] if candidates else ""
    if groups_path and os.path.isfile(groups_path):
        data.hunk_groups_path = groups_path
        data.hunk_groups_spec = load_hunk_groups(groups_path)
        logger.info(
            "Hunk groups: %s (%d groups)"
            % (groups_path, len(data.hunk_groups_spec.get("groups", [])))
        )
    else:
        data.hunk_groups_path = ""
        data.hunk_groups_spec = None
        logger.info("Hunk groups: not configured, will port hunks one by one")

    # ---- merged hunk 输入目录（可选）---------------------------------------
    # 若目录里已经是「合并好的 merged hunk 文件」，直接把它们当输入：
    # 轮到哪一组，就把哪个 *.patch 作为该 session 的 new_patch。
    # 这样合并结果完全由外部文件决定（也可以手工改写某个组）。
    merged_dir = config.get("merged_hunks_dir", "") or ""
    if merged_dir and not os.path.isabs(merged_dir):
        merged_dir = os.path.join(data.patch_dataset_dir, merged_dir)
    if not merged_dir:
        cand = os.path.join(data.patch_dataset_dir, "merged_hunks", "original")
        if os.path.isdir(cand):
            merged_dir = cand
    data.merged_hunks_input_dir = merged_dir if os.path.isdir(merged_dir) else ""
    if data.merged_hunks_input_dir:
        n_patch = len(glob.glob(os.path.join(data.merged_hunks_input_dir, "*.patch")))
        logger.info(
            "Merged-hunk input dir: %s (%d *.patch, used as session inputs)"
            % (data.merged_hunks_input_dir, n_patch)
        )
    else:
        logger.info(
            "Merged-hunk input dir: not configured, will merge hunks from the patch"
        )

    if (
        not is_commit_valid(data.new_patch, data.project_dir)
        or not is_commit_valid(data.target_release, data.project_dir)
        or not is_commit_valid(data.new_patch_parent, data.project_dir)
    ):
        exit(1)

    data.new_patch = rev_parse_commit(data.new_patch, data.project_dir)
    data.target_release = rev_parse_commit(data.target_release, data.project_dir)
    data.new_patch_parent = rev_parse_commit(data.new_patch_parent, data.project_dir)

    return data


def main():
    # process arguments
    parser = argparse.ArgumentParser(
        description="Backports patch with the help of LLM",
        usage="%(prog)s --config CONFIG.yml\ne.g.: python %(prog)s --config CVE-examaple.yml",
    )
    parser.add_argument(
        "-c", "--config", type=str, required=True, help="CVE config yml"
    )
    parser.add_argument("-d", "--debug", action="store_true", help="enable debug mode")
    parser.add_argument(
        "--list-groups",
        action="store_true",
        help="只打印 hunk 分组（merged hunk）后退出，不调用 LLM",
    )
    parser.add_argument(
        "--only-group",
        type=int,
        default=None,
        help="只移植第 N 个 merged hunk（0 基下标）；不给则一次进程跑完全部组",
    )
    parser.add_argument(
        "--coverage-retries",
        type=int,
        default=None,
        help="组内启发式覆盖检查的重试次数（默认读取配置，否则为 0）；最终语义审查独立执行",
    )
    parser.add_argument(
        "--allow-incomplete-groups",
        action="store_true",
        help="允许「分组规格没覆盖全部 hunk」继续跑（默认会直接报错退出）",
    )
    parser.add_argument(
        "--cumulative",
        action="store_true",
        help="累积模式：每组成功后提交到临时分支，下一组在其上继续（避免组间重复实现）",
    )
    args = parser.parse_args()
    debug_mode = args.debug
    config_file = args.config
    if debug_mode:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    # load and check config, create file log
    data = load_yml(config_file)
    data.hunk_group_index = args.only_group
    if args.coverage_retries is not None:
        data.coverage_retries = args.coverage_retries
    log_dir = "../logs"
    os.makedirs(log_dir, exist_ok=True)
    now = datetime.datetime.now().strftime("%m%d%H%M")
    logfile = os.path.join(log_dir, f"{data.project}-{data.tag}-{now}.log")
    add_file_handler(logger, logfile)

    # use LLM to backport

    project = Project(data)
    project.repo.git.clean("-fdx")

    # 分组完整性：一份漏了 hunk 的规格不该静默退化成"逐个 hunk 移植"
    if getattr(data, "hunk_groups_spec", None):
        patch_text = project._get_patch(data.new_patch)
        _, all_hunks = split_patch_indexed(patch_text, True)
        missing = uncovered_hunks(all_hunks, data.hunk_groups_spec)
        if missing:
            msg = "分组规格 %s 没有覆盖全部 hunk：%d/%d 个不在任何组里 -> %s" % (
                getattr(data, "hunk_groups_path", "?"),
                len(missing),
                len(all_hunks),
                missing,
            )
            if args.allow_incomplete_groups:
                logger.warning(
                    msg + "（已用 --allow-incomplete-groups 放行，这些 hunk 会各自单独成组）"
                )
            else:
                logger.error(msg)
                logger.error(
                    "这通常说明分组文件不完整。要按现状继续跑，加 --allow-incomplete-groups"
                )
                exit(2)
        else:
            logger.info("分组覆盖完整：%d 个 hunk 恰好都在组里" % len(all_hunks))

    if args.list_groups:
        src = getattr(data, "merged_hunks_input_dir", "")
        if src:
            merged = load_merged_hunk_files(src)
            print("输入: 已合并好的 merged hunk 文件目录 %s" % src)
        else:
            patch = project._get_patch(data.new_patch)
            _, merged = group_patch(patch, getattr(data, "hunk_groups_spec", None), True)
            print(
                "输入: 分组文件 %s（现场合并）"
                % (getattr(data, "hunk_groups_path", "") or "(无，逐 hunk 模式)")
            )
        print("共 %d 个 merged hunk:" % len(merged))
        for i, g in enumerate(merged):
            print(
                "  [%2d] gid=%-8s original_hunks=%-30s %s"
                % (i, g["gid"], g["hunks"] or ["?"], g["name"])
            )
            for f in g["files"]:
                print("          %s" % f)
        sel = select_merged_hunks(merged, data.hunk_group_index)
        if len(sel) != len(merged):
            print(
                "本次只处理: %s（--only-group %s）"
                % ([i for i, _ in sel], data.hunk_group_index)
            )
        if not src:
            out_dir = os.path.join(data.patch_dataset_dir, "merged_hunks")
            dump_merged_hunks(merged, out_dir)
            print("已写出: %s/index.md 与 %s/original/*.patch" % (out_dir, out_dir))
        return

    # ---- 累积模式 --------------------------------------------------------
    # 每组跑完后把该组产物提交到临时分支，下一组从「已含前面各组改动」的状态继续。
    # 不累积时实测会出现：两个组各自实现同一个接口/同一个重载，
    # 拼起来的 complete.patch 因上下文重复而 git apply 失败。
    data.cumulative = bool(getattr(data, "cumulative", False) or args.cumulative)
    if data.cumulative:
        base = project.repo.git.rev_parse(data.target_release)
        # Git refs are shared by every linked worktree.  A fixed branch name
        # makes concurrent/isolated case worktrees collide with the main repo
        # (or with each other) as soon as checkout -B runs.  Keep the branch
        # stable for reruns of this worktree, but make it unique per checkout.
        import hashlib
        worktree_key = hashlib.sha256(
            os.path.realpath(project.dir).encode("utf-8", "surrogateescape")
        ).hexdigest()[:12]
        branch = "pgpt-cumulative-" + worktree_key
        # 上一轮结束时 _compile_patch 会把补丁留在工作树里（它的收尾 reset 是注释掉的），
        # 不先还原的话 checkout -B 会因为"local changes would be overwritten"失败。
        project.repo.git.reset("--hard")
        project.repo.git.checkout("-B", branch, base)
        data.cum_base = base
        data.target_release = branch
        # 打成功后不要立刻 reset：工作树里就是下一组要接着用的状态
        project.keep_tree_after_apply = True
        logger.info(
            "累积模式: 分支 %s，base %s（后续各组在同一工作树上叠加）"
            % (branch, base[:12])
        )

    start_time = time.time()
    agent_executor, llm = initial_agent(project, data, debug_mode)

    # 每个 merged hunk 单独开一个 session（新的 AgentExecutor），用分组版 prompt
    def make_agent(group):
        # 组越大需要的工具调用越多（实测一个 3-hunk 组也能撞到 30 次上限）
        budget = 30 + 4 * len(group.get("hunks") or [])
        return build_hunk_executor(
            llm, project, debug_mode, group_mode=True, max_iterations=budget
        )

    try:
        do_backport(agent_executor, project, data, llm, logfile, make_agent=make_agent)
        end_time = time.time()
        time.sleep(10)
        
        logger.debug(
            f"This patch total cost time: {int(end_time - start_time)} Seconds."
        )
    except KeyboardInterrupt:
        logger.debug("Start to calculate cost!")
        end_time = time.time()

        logger.debug(
            f"This patch total cost time: {int(end_time - start_time)} Seconds."
        )

    shutil.copy(logfile, data.patch_dataset_dir)


if __name__ == "__main__":
    main()

#                    Version A           Version A(Fixed)
#   ┌───┐            ┌───┐             ┌───┐
#   │   ├───────────►│   ├────────────►│   │
#   └─┬─┘            └───┘             └───┘
#     │
#     │
#     │
#     │              Version B
#     │              ┌───┐
#     └─────────────►│   ├────────────► ??
#                    └───┘
