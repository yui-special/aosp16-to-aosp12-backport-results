import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import httpx

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.callbacks import FileCallbackHandler
from langchain_openai import ChatOpenAI, AzureChatOpenAI

from agent.prompt import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_GROUP,
    SYSTEM_PROMPT_PTACH,
    USER_PROMPT_HUNK,
    USER_PROMPT_HUNK_GROUP,
    USER_PROMPT_PATCH,
)
from tools.logger import logger
from tools.project import Project
from tools.utils import (
    dump_merged_hunks,
    group_patch,
    hunk_coverage_report,
    load_merged_hunk_files,
    select_merged_hunks,
    split_patch_indexed,
)


def build_llm(data):
    """按配置构造 LLM 客户端（与是否分组无关）。"""
    use_azure = data.use_azure

    if use_azure:
        azure_endpoint = data.azure_endpoint
        azure_deployment = data.azure_deployment
        azure_api_version = data.azure_api_version
        api_key = data.openai_key

        logger.info(f"Using Azure OpenAI: {azure_endpoint} (deployment: {azure_deployment})")

        llm = AzureChatOpenAI(
            temperature=1.0,  # Set to 1.0 for GPT-5 model; can be changed if using other models
            azure_deployment=azure_deployment,
            api_key=api_key,
            azure_endpoint=azure_endpoint,
            api_version=azure_api_version,
            verbose=True,
        )
    else:
        logger.info("Using OpenAI API compatible endpoint (GreatRouter)")
        # 1. 从配置读取 base_url，如果读取不到就默认用 GreatRouter 的地址
        base_url = getattr(data, 'llm_base_url', "https://endpoint.greatrouter.com/v1")
        # 2. 从配置读取模型，如果读取不到就默认用 gpt-4o
        model_name = getattr(data, 'llm_model', "gpt-5.5")
        # 3. 强制使用 httpx 客户端走代理，彻底解决 Network is unreachable 问题
        # [asan-runner] 不再硬编码代理：默认直连；如需代理用 LLM_PROXY 环境变量指定。
        # 这里用 local_address="0.0.0.0" 强制 IPv4，避免解析到 AAAA 却没 IPv6 路由时
        # 报 "Network is unreachable"（这正是当初加代理想解决的问题）。
        _proxy = os.environ.get("LLM_PROXY", "").strip()
        # 强制 TLS 1.2：实测该网关的 TLS 1.3 握手会卡死（Client Hello 后无响应），
        # 而 TLS 1.2 完全正常。这是与具体 CVE 无关的环境适配。
        try:
            _ssl_ctx = ssl.create_default_context()
            _ssl_ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        except Exception:  # noqa: BLE001
            _ssl_ctx = None
        if _proxy:
            http_client = httpx.Client(proxy=_proxy, verify=_ssl_ctx or True)
        else:
            http_client = httpx.Client(
                transport=httpx.HTTPTransport(local_address="0.0.0.0",
                                              verify=_ssl_ctx or True)
            )
        # gpt-5 系列（含 gpt-5.5）不接受非默认 temperature，端点会报
        # GR_UNSUPPORTED_TEMPERATURE，所以按模型族选择：gpt-5* 用 1.0。
        _temp = 1.0 if model_name.lower().startswith("gpt-5") else 0.5
        llm = ChatOpenAI(
            temperature=_temp,
            model=model_name,
            api_key=data.openai_key,
            openai_api_base=base_url, # 指向 GreatRouter 的地址
            http_client=http_client,  # 强制走代理
            verbose=True,
        )
    return llm


def build_hunk_executor(
    llm, project: Project, debug_mode: bool, group_mode: bool = False, max_iterations: int = 30
):
    """为「一个 hunk / 一个 merged hunk」新建一个 agent session。

    每次调用都会新建 AgentExecutor（新的消息历史），也就是一个新的 session，
    这样不同组之间不会互相污染上下文。

    Args:
        llm: 已构造好的 LLM 客户端（多个 session 共用同一个客户端即可）。
        project (Project): 提供 viewcode / validate 等工具。
        debug_mode (bool): AgentExecutor 的 verbose。
        group_mode (bool): True 表示这一组是「依赖 hunk 合并后的 merged hunk」，
                           使用分组版 prompt。
    """
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT_GROUP if group_mode else SYSTEM_PROMPT),
            ("user", USER_PROMPT_HUNK_GROUP if group_mode else USER_PROMPT_HUNK),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ]
    )
    viewcode, locate_symbol, validate, git_history, git_show = project.get_tools()
    tools = [viewcode, locate_symbol, validate, git_history, git_show]
    agent = create_tool_calling_agent(llm, tools, prompt)
    agent_executor = AgentExecutor(
        agent=agent, tools=tools, verbose=debug_mode, max_iterations=max_iterations
    )
    return agent_executor


def initial_agent(project: Project, data, debug_mode: bool):
    """兼容老接口：返回 (agent_executor, llm)。"""
    llm = build_llm(data)
    agent_executor = build_hunk_executor(llm, project, debug_mode, group_mode=False)
    return agent_executor, llm


def _try_apply(project, patch_text, check_only=False, recount=False):
    """把 patch 打到当前工作树（或只做 --check）。"""
    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, suffix=".patch", encoding="utf-8"
    ) as fh:
        fh.write(patch_text)
        path = fh.name
    args = ["git", "apply"]
    if check_only:
        args.append("--check")
    if recount:
        args.append("--recount")
    args.append(path)
    try:
        project.repo.git.execute(args)
        return True
    except Exception:  # noqa: BLE001
        return False


def compile_group(project, data, gpos, log_path):
    """累积模式下每组提交后立即编译（可选，PG_COMPILE_PER_GROUP=1 时启用）。

    早失败早反馈：不必等所有组跑完才发现签名对不上。
    build.sh 在数据集目录里，用 cwd=项目目录 执行（它内部依赖当前目录的 git 状态）。
    """
    build_sh = os.path.join(data.patch_dataset_dir, "build.sh")
    if not os.path.isfile(build_sh):
        return None
    rc = None
    try:
        p = subprocess.run(["bash", build_sh], cwd=project.dir,
                           capture_output=True, text=True, timeout=900)
        rc = p.returncode
        out = (p.stdout or "")[-500:]
        err = (p.stderr or "")[-500:]
        line = "group %02d compile rc=%d\n%s%s" % (
            gpos, rc, out, ("\n[stderr]\n" + err) if err.strip() else "")
    except subprocess.TimeoutExpired:
        line = "group %02d compile TIMEOUT" % gpos
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    logger.info(line.split("\n")[0])
    return rc


def rebuild_from_candidate(project, base, branch, candidate):
    """把 candidate（修复循环产出的全量补丁）重放到干净 base 上，再 diff 出产物。

    只在修复循环真的改过补丁时调用；没发生修复的运行完全不走这里，保证历史结果一致。
    返回 None 表示重放失败。
    """
    repo = project.repo
    repo.git.reset("--hard")
    try:
        repo.git.checkout(base)
    except Exception:  # noqa: BLE001
        repo.git.checkout(branch)
        return None

    try:
        head = repo.git.rev_parse("HEAD")
        base_sha = repo.git.rev_parse(base)
    except Exception:  # noqa: BLE001
        repo.git.checkout(branch)
        return None
    if head != base_sha:
        repo.git.checkout(branch)
        return None

    if not _try_apply(project, candidate, check_only=True):
        # 行数写错的情况很常见，--recount 修一下
        if not _try_apply(project, candidate, recount=True):
            repo.git.reset("--hard")
            repo.git.checkout(branch)
            return None
    else:
        _try_apply(project, candidate)

    fixed = repo.git.diff(base)
    repo.git.reset("--hard")
    repo.git.checkout(branch)
    return fixed if fixed.strip() else None


def commit_group(project, patches, gpos, group):
    """累积模式：把本组已接受的补丁落到工作树并提交，供后面的组在其上继续。

    不累积时（默认），每组都从干净的 target 开始，实测会出现「同一件事被两个组
    各实现一遍」（48649: PermissionManagerServiceInternal.java 与
    PermissionManagerService.java 被重复修改），最后拼起来的 patch 打不上。

    本组成功时工作树里已经是「branch head + 本组改动」的状态（_apply_hunk 在累积
    模式下不再还原），这里只负责把它提交。不能拿 patches 重新打一遍：实测一组里
    可能存了多份互相重叠的补丁（模型改完又整份重发），重打必然冲突。
    """
    repo = project.repo
    try:
        # 只提交已被跟踪文件的改动，避免把仓库里的残留日志一起提交
        repo.git.add("-u")
        # 模型新建的源文件也要带上（绝不提交 *.log 之类的残留）
        for line in repo.git.status("--porcelain").split("\n"):
            if line.startswith("?? "):
                fname = line[3:].strip()
                if fname.lower().endswith(
                    (".java", ".kt", ".aidl", ".c", ".h", ".cc", ".cpp", ".py", ".rs")
                ):
                    repo.git.add("--", fname)
        staged = repo.git.diff("--cached", "--name-only").strip()
        if not staged:
            repo.git.reset("--hard")
            logger.debug("累积: 第 %d 组没有产出，分支不前进" % gpos)
            return
        # 显式带身份：新仓库没配 user.name/user.email 时，git commit 会直接失败
        # （实测 CVE-2026-0045 / 46054 的 linux_510、platform_system_bt 都是这样）
        repo.git.update_environment(
            GIT_AUTHOR_NAME="pgpt-backport",
            GIT_AUTHOR_EMAIL="pgpt@localhost",
            GIT_COMMITTER_NAME="pgpt-backport",
            GIT_COMMITTER_EMAIL="pgpt@localhost",
        )
        repo.git.commit(
            "-m",
            "pgpt cumulative: merged hunk %02d (original hunks %s)" % (gpos, group["hunks"]),
        )
        logger.info(
            "累积: 第 %d 组已提交（%s），分支前进到 %s"
            % (gpos, staged.replace("\n", ", "), repo.git.rev_parse("HEAD")[:12])
        )
    except Exception as e:  # noqa: BLE001  -- 累积失败不能弄死整轮
        logger.error("累积提交失败（第 %d 组）: %s: %s" % (gpos, type(e).__name__, e))


def describe_group(group):
    """把一组的元信息渲染成给模型看的文字。"""
    if len(group["hunks"]) <= 1 and not group.get("concern"):
        return ""
    lines = []
    if group.get("name"):
        lines.append("Group name: %s" % group["name"])
    if group.get("concern"):
        lines.append("What this group does: %s" % group["concern"])
    if group.get("why"):
        lines.append("Why these hunks must be ported together: %s" % group["why"])
    lines.append("Files involved: %s" % ", ".join(group["files"]))
    lines.append(
        "Original hunk ids in the upstream patch: %s" % ", ".join(str(i) for i in group["hunks"])
    )
    if group.get("hints"):
        lines.append("")
        lines.append(group["hints"])
    return "\n".join(lines)


def do_backport(
    agent_executor: AgentExecutor,
    project: Project,
    data,
    llm: ChatOpenAI,
    logfile: str,
    make_agent=None,
):
    """主流程。

    改动：不再是「一个 hunk 一次」，而是「一个 merged hunk（含若干互相依赖的原始
    hunk）一次」。若配置里带了 hunk 分组文件，则按分组跑；否则退化为每个 hunk 一组
    （与改动前行为一致）。

    Args:
        agent_executor: 兼容参数；当 make_agent 为 None 时沿用这个 executor。
        make_agent: callable(group) -> AgentExecutor；为每个 merged hunk 新建一个
                    独立的 agent session。
    """
    log_handler = FileCallbackHandler(logfile)

    patch = project._get_patch(data.new_patch)
    cumulative = bool(getattr(data, "cumulative", False))
    cum_base = getattr(data, "cum_base", "") or ""

    # 输入有两种来源（优先用「已经合并好的文件」）：
    #   1) merged_hunks 目录里的 *.patch —— 每个文件就是一组已经合并好的 hunk，
    #      轮到谁就把谁的内容作为该 session 的输入（可以手工改写这些文件）；
    #   2) 否则回退到「分组文件 + 现场合并」（hunk_groups.json -> merged hunk）。
    groups_spec = getattr(data, "hunk_groups_spec", None)
    merged_input_dir = getattr(data, "merged_hunks_input_dir", "")

    merged_hunks = []
    if merged_input_dir:
        merged_hunks = load_merged_hunk_files(merged_input_dir)
        if merged_hunks:
            logger.info(
                "Input = %d pre-merged hunk file(s) from %s "
                "(one file = one session input)" % (len(merged_hunks), merged_input_dir)
            )
        else:
            logger.warning(
                "目录 %s 里没有 *.patch，回退到按分组现场合并" % merged_input_dir
            )

    if not merged_hunks:
        _, merged_hunks = group_patch(patch, groups_spec, True)
        if groups_spec:
            logger.info(
                "Input = hunk groups from %s: %d group(s) -> %d merged hunk(s)"
                % (
                    getattr(data, "hunk_groups_path", "?"),
                    len(groups_spec.get("groups", [])),
                    len(merged_hunks),
                )
            )
        else:
            logger.info(
                "No hunk groups configured, backporting %d hunk(s) one by one (legacy mode)"
                % len(merged_hunks)
            )

    if merged_input_dir:
        # 输入就是那些文件，不再覆盖它们；只准备放结果的 ported/ 目录
        ported_dir = os.path.join(os.path.dirname(os.path.normpath(merged_input_dir)), "ported")
        os.makedirs(ported_dir, exist_ok=True)
    else:
        # 把 merged hunk 落盘：original/ 是每组的原始 patch，ported/ 放移植结果
        _, ported_dir = dump_merged_hunks(
            merged_hunks, os.path.join(data.patch_dataset_dir, "merged_hunks")
        )

    if not merged_hunks:
        logger.error(
            "没有拿到任何 merged hunk（分组文件为空 / merged_hunks 目录里没有 *.patch），退出"
        )
        return

    # 选择这次要跑哪些组：默认全部（一次进程跑完）；给了 --only-group N 就只跑第 N 组
    selected = select_merged_hunks(merged_hunks, getattr(data, "hunk_group_index", None))
    if not selected:
        return
    logger.info(
        "Backporting %d of %d merged hunk(s): indices %s"
        % (len(selected), len(merged_hunks), [i for i, _ in selected])
    )

    # 组内成员 hunk 的原文，用来做「这一组是否被完整移植」的机械校验
    _, all_hunks = split_patch_indexed(patch, True)
    hunks_by_idx = {h["idx"]: h for h in all_hunks}
    # 覆盖校验是启发式：实测两次"失败"都是误报（标识符被改名），而每次重试要一整轮
    # session（≈20 分钟）。所以默认只记录、不重试；需要时可 --coverage-retries 1 打开。
    coverage_retries = int(getattr(data, "coverage_retries", 0) or 0)

    # [C] 跨组共享变更账本：后面的组能看到前面各组「实际产出了什么」。
    # 只注入 prompt，不落到工作树（各组仍是独立 session + 干净 checkout），
    # 所以不会破坏"每组可独立复现"的性质。
    ledger = []
    ledger_limit = int(getattr(data, "ledger_chars", 6000) or 6000)

    for gpos, group in selected:
        pp = group["text"]
        members = group["hunks"] or ["?"]
        group_tag = "merged hunk %d (original hunks %s, group %s)" % (
            gpos,
            members,
            group["gid"],
        )
        member_hunks = [hunks_by_idx[i] for i in group["hunks"] if i in hunks_by_idx]
        status_path = os.path.join(ported_dir, "merged-hunk-%02d.status.json" % gpos)
        project.round_succeeded = False
        project.context_mismatch_times = 0
        n_before = len(project.succeeded_patches)

        def produced_patches():
            return list(project.succeeded_patches[n_before:])

        def save_ported():
            """把本组产出的 patch 存到 ported/merged-hunk-NN.patch。"""
            new_patches = produced_patches()
            if not new_patches:
                return
            with open(
                os.path.join(ported_dir, "merged-hunk-%02d.patch" % gpos),
                "w",
                encoding="utf-8",
                newline="\n",
            ) as fh:
                fh.write("\n".join(new_patches) + "\n")

        def check_coverage():
            """本组产出是否覆盖了组内每个成员 hunk（不依赖 LLM）。"""
            produced = produced_patches()
            need_not_ported = project.round_succeeded and not produced
            return hunk_coverage_report(
                member_hunks, "\n".join(produced), need_not_ported
            )

        def save_status(cov, attempts):
            with open(status_path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(
                    {
                        "merged_hunk": gpos,
                        "gid": group["gid"],
                        "member_hunks": group["hunks"],
                        "round_succeeded": project.round_succeeded,
                        "complete": cov["complete"],
                        "missing_hunks": cov["missing"],
                        "need_not_ported": cov["need_not_ported"],
                        "coverage_attempts": attempts,
                        "members": cov["members"],
                    },
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )

        def ledger_note():
            """把前面各组的产出摘要成一段 prompt 文本（[C] 共享变更账本）。"""
            if not ledger:
                logger.debug("%s ledger: 0 chars (no previous groups)" % (group_tag,))
                return ""
            joined = "\n".join(ledger)
            if len(joined) > ledger_limit:
                joined = joined[:ledger_limit] + "\n...(truncated)"
            text = (
                "\n[PREVIOUS CHANGES — shared change ledger]\n"
                "These hunks were already ported by EARLIER groups in this same run. They are NOT\n"
                "applied to your working tree (each group starts from a clean checkout); they will be\n"
                "applied together with yours at the end. Keep your patch CONSISTENT with them --\n"
                "same signatures, same parameter order, same renamed symbols:\n"
                "```diff\n" + joined + "\n```\n"
            )
            logger.debug(
                "%s ledger: %d chars from %d previous patch(es)"
                % (group_tag, len(text), len(ledger))
            )
            return text

        try:
            ret = project._apply_hunk(data.target_release, pp, False)
        except Exception as e:  # noqa: BLE001  -- [P0] 单组异常不能弄死整个运行
            logger.error(
                "%s 在 _apply_hunk 抛异常: %s: %s" % (group_tag, type(e).__name__, e)
            )
            ret = "TARGET_FILE_MISSING: %s could not be applied (%s)" % (group_tag, e)
            project.round_succeeded = False
        if project.round_succeeded:
            logger.debug(f"{group_tag} can be applied without any conflicts")
        else:
            block_list = re.findall(r"older version.\n(.*?)\nBesides,", ret, re.DOTALL)
            similar_block = "\n".join(block_list)
            logger.debug(
                f"{group_tag} can not be applied, using LLM to generate a fix"
            )
            project.now_hunk = pp
            project.now_hunk_num = gpos
            # 每个 merged hunk 用一个新的 session（新的 AgentExecutor）来移植
            executor = make_agent(group) if make_agent is not None else agent_executor
            try:
                executor.invoke(
                    {
                        "project_url": data.project_url,
                        "new_patch_parent": data.new_patch_parent,
                        "new_patch": pp,
                        "target_release": data.target_release,
                        "similar_block": similar_block,
                        "group_size": len(group["hunks"]),
                        "group_ids": group["hunks"],
                        "group_desc": describe_group(group) + ledger_note(),
                    },
                    {"callbacks": [log_handler]},
                )
            except Exception as e:  # noqa: BLE001  -- [P0] 会话异常不许弄死整个运行
                logger.error(
                    "%s 会话抛异常: %s: %s" % (group_tag, type(e).__name__, e)
                )
                project.round_succeeded = False
            if not project.round_succeeded:
                logger.debug(
                    f"Failed to backport the {group_tag} \n----------------------------------\n{pp}\n----------------------------------\n"
                )
                logger.error(f"Reach max_iterations for {group_tag}")
                save_ported()
                # [P0] 一组失败不再终止整个运行：记录状态后继续下一组
                save_status(check_coverage(), 0)
                ledger.extend(produced_patches())
                if cumulative:
                    commit_group(project, produced_patches(), gpos, group)
                continue

        # ---- 后置校验：这一组是不是真的被完整移植了 -------------------------
        cov = check_coverage()
        attempts = 0
        while (not cov["complete"]) and attempts < coverage_retries:
            attempts += 1
            logger.warning(
                "%s 的产出不完整：产出里找不到 hunk %s 的痕迹，重试第 %d 次"
                % (group_tag, cov["missing"], attempts)
            )
            previous = produced_patches()
            # 重试前先丢掉上一次的产出，避免两次产出在最后拼接时互相冲突
            del project.succeeded_patches[n_before:]
            project.round_succeeded = False
            project.context_mismatch_times = 0
            note = (
                "\n[COVERAGE CHECK] Your previous patch did NOT cover every hunk of this group. "
                "Missing hunks (original upstream hunk ids): %s. "
                "Go back to the upstream patch of this group and make sure your patch ports ALL "
                "of its hunks; if a hunk really must not be ported, state that explicitly."
                % (cov["missing"],)
            )
            executor = make_agent(group) if make_agent is not None else agent_executor
            try:
                executor.invoke(
                    {
                        "project_url": data.project_url,
                        "new_patch_parent": data.new_patch_parent,
                        "new_patch": pp,
                        "target_release": data.target_release,
                        "similar_block": "",
                        "group_size": len(group["hunks"]),
                        "group_ids": group["hunks"],
                        "group_desc": describe_group(group) + ledger_note() + note,
                    },
                    {"callbacks": [log_handler]},
                )
            except Exception as e:  # noqa: BLE001  -- [P0] 重试会话异常也不许弄死整个运行
                logger.error(
                    "%s 重试会话抛异常: %s: %s" % (group_tag, type(e).__name__, e)
                )
                project.round_succeeded = False
            if not project.round_succeeded:
                # 重试没成，回滚到上一次的产出，别把已经拿到的部分丢掉
                project.succeeded_patches.extend(previous)
                cov = check_coverage()
                break
            cov = check_coverage()

        save_ported()
        save_status(cov, attempts)
        # 本组产出记入账本，供后面的组参考
        ledger.extend(produced_patches())
        if cumulative:
            commit_group(project, produced_patches(), gpos, group)
            if os.environ.get("PG_COMPILE_PER_GROUP") == "1":
                compile_group(project, data, gpos,
                              os.path.join(ported_dir, "compile-per-group.log"))
        if cov["complete"]:
            logger.debug(
                "%s 覆盖校验通过（%d 个成员 hunk 都有痕迹）"
                % (group_tag, len(member_hunks))
            )
        else:
            logger.error(
                "%s 覆盖校验失败：产出里找不到 hunk %s 的痕迹（已重试 %d 次）"
                % (group_tag, cov["missing"], attempts)
            )
            logger.error("    详情见 %s" % status_path)

    project.all_hunks_applied_succeeded = True
    logger.info(f"Aplly all hunks in the patch      PASS")
    project.now_hunk = "completed"
    if cumulative and cum_base:
        # 累积模式下，各组已经提交到分支；最终产物 = base..HEAD 的 diff
        project.repo.git.reset("--hard")
        # 用分支名而不是 HEAD：模型在会话里用 viewcode 看过别的 ref 时，HEAD 可能
        # 停在别处（实测 48550 因此 diff 出整仓库 42609 个文件、385MB）
        complete_patch = project.repo.git.diff("%s..%s" % (cum_base, data.target_release))
    else:
        complete_patch = "\n".join(project.succeeded_patches)

    # 把「完整 patch」落盘 —— 这是本次移植的最终产物（等价于 PortGPT 平时打印出来的那一份）。
    # 单组模式（--only-group N）不覆盖全量产物，另存为 complete-group-NN.patch。
    _only = getattr(data, "hunk_group_index", None)
    complete_name = (
        "complete.patch" if _only is None or _only == "" else "complete-group-%02d.patch" % int(_only)
    )
    complete_path = os.path.join(data.patch_dataset_dir, complete_name)
    try:
        # 用 surrogateescape 原样写回字节：AOSP 里有少数文件不是严格 UTF-8
        blob = complete_patch if complete_patch.endswith("\n") else complete_patch + "\n"
        with open(complete_path, "wb") as fh:
            fh.write(blob.encode("utf-8", "surrogateescape"))
        logger.info(
            "Complete patch (%d bytes) written to %s"
            % (len(blob.encode("utf-8", "surrogateescape")), complete_path)
        )
    except OSError as e:  # noqa: BLE001
        logger.warning("写完整 patch 失败: %s" % e)

    if not cumulative:
        project.repo.git.clean("-fdx")
    for file in os.listdir(data.patch_dataset_dir):
        src = f"{data.patch_dataset_dir}{file}"
        if os.path.isdir(src):
            # 数据集目录里可能有 merged_hunks/ 之类的子目录，copy2 会抛 IsADirectoryError
            continue
        if os.path.exists(f"{data.project_dir}{file}"):
            os.remove(f"{data.project_dir}{file}")
        shutil.copy2(src, f"{data.project_dir}{file}")
    project.context_mismatch_times = 0
    # 累积模式下 target_release 指向累积分支（改动已经在那里），校验要用干净的 base
    validate_ret = project._validate(cum_base or data.target_release, complete_patch)
    if project.poc_succeeded:
        logger.info(
            f"Successfully backport the patch to the target release {data.target_release}"
        )
        for patch in project.succeeded_patches:
            logger.info(patch)
        return

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT_PTACH),
            ("user", USER_PROMPT_PATCH),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ]
    )
    # XXX maybe refactor initial_agent function to cover
    viewcode, locate_symbol, validate, _, _ = project.get_tools()
    tools = [viewcode, locate_symbol, validate]
    agent = create_tool_calling_agent(llm, tools, prompt)
    agent_executor = AgentExecutor(
        agent=agent, tools=tools, verbose=True, max_iterations=20
    )
    n_before_repair = len(project.succeeded_patches)
    agent_executor.invoke(
        {
            "project_url": data.project_url,
            "new_patch_parent": data.new_patch_parent,
            "target_release": data.target_release,
            "new_patch": patch,
            "complete_patch": complete_patch,
            "compile_ret": validate_ret,
        },
        {"callbacks": [log_handler]},
    )
    if project.poc_succeeded:
        logger.info(
            f"Successfully backport the patch to the target release {data.target_release}"
        )
        # 只有修复循环**真的改过补丁**（succeeded_patches 有新增）才重建产物；
        # 否则保持已落盘的分支 diff 不变，保证未修复的运行结果完全一致。
        if len(project.succeeded_patches) > n_before_repair and cumulative and cum_base:
            repaired = project.succeeded_patches[-1]
            fixed = rebuild_from_candidate(project, cum_base, data.target_release, repaired)
            if fixed:
                try:
                    with open(complete_path, "wb") as fh:
                        blob = fixed if fixed.endswith("\n") else fixed + "\n"
                        fh.write(blob.encode("utf-8", "surrogateescape"))
                    logger.info(
                        "修复循环改过补丁，已用重放结果重建 complete.patch（%d 字节）"
                        % len(blob)
                    )
                except OSError as e:  # noqa: BLE001
                    logger.warning("写修复后的 complete.patch 失败: %s" % e)
        for patch in project.succeeded_patches:
            logger.info(patch)
    else:
        logger.error(
            f"Failed backport the patch to the target release {data.target_release}"
        )
