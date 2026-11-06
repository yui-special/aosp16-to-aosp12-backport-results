import json
import hashlib
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
from copy import copy
import httpx

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.callbacks import FileCallbackHandler
from langchain_core.agents import AgentFinish, AgentStep
from langchain_openai import ChatOpenAI

from agent.prompt import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_GROUP,
    SYSTEM_PROMPT_PTACH,
    USER_PROMPT_HUNK,
    USER_PROMPT_HUNK_GROUP,
    USER_PROMPT_PATCH,
)
from tools.logger import logger
from tools.group_report import GroupComparison
from tools.project import (Project, FinalValidationUnavailable, create_find_references_tool,
                           create_query_api_context_tool, create_submit_candidate_tool)
from tools.source_checks import ApiCatalog, artifact_tree
from tools.porting_context import ApiContext, ChangeLedger, QueryProgress, execution_result, target_context
from agent.coverage import CoverageReviewer, repair_findings, is_test_source
from tools.utils import (
    dump_merged_hunks,
    group_patch,
    hunk_coverage_report,
    load_merged_hunk_files,
    select_merged_hunks,
    split_patch_indexed,
)


class CandidateRepairExecutor(AgentExecutor):
    """End a synchronous repair invocation as soon as one candidate is submitted.

    LangChain's multi-action agent rejects return_direct tools. Stop consuming its
    step iterator instead, so later calls in the same model response cannot run.
    """
    def _take_next_step(self, name_to_tool_map, color_mapping, inputs,
                        intermediate_steps, run_manager=None):
        steps = []
        for step in self._iter_next_step(name_to_tool_map, color_mapping, inputs,
                                        intermediate_steps, run_manager):
            if (isinstance(step, AgentStep) and step.action.tool == 'submit_candidate'
                    and not str(step.observation).startswith('CANDIDATE_SUBMISSION_INVALID:')):
                return AgentFinish({'output': step.observation}, str(step.observation))
            steps.append(step)
        return self._consume_next_step(steps)


def _is_test_coverage_finding(member):
    return is_test_source(member.get('file', ''))


def _repair_feedback_stages(validate_ret, findings, production_required):
    test_findings = [m for m in findings if _is_test_coverage_finding(m)]
    if not test_findings:
        if not production_required:
            return [('test_validation', validate_ret + (
                '\nCURRENT STAGE: repair the reported test/PoC failure using the current '
                'candidate and failure locations. Compilation already passed. '
                'Submit the local diff against candidate_ref (the program records the complete replacement patch) through submit_candidate.\n'))]
        return [('complete', validate_ret)]
    if not production_required:
        return [('test_coverage', validate_ret + (
            '\nCURRENT STAGE: repair only the remaining test-coverage gaps. '
            'Do not revisit unrelated production changes. Submit a local diff against candidate_ref (the program records the complete replacement patch) '
            'through submit_candidate for intermediate checks. After semantic checks pass, '
            'stop this session; the orchestrator will run final build/test/PoC.\n'))]
    production_feedback = validate_ret + (
        '\nCURRENT STAGE: repair production-code completeness only. '
        'Do not investigate or modify test-coverage gaps in this stage; they have a later stage. '
        'Submit the local diff against candidate_ref (the program records the complete replacement patch) through submit_candidate. '
        'Build and tests are deferred until all known repair stages finish.\n')
    test_feedback = (
        'The production-code stage submitted a new complete candidate. '
        'CURRENT STAGE: repair only the remaining test-coverage gaps below. '
        'Do not revisit unrelated production changes. Submit a local diff against candidate_ref (the program records the complete replacement patch) through '
        'submit_candidate for intermediate checks. After semantic checks pass, stop this session; '
        'the orchestrator will run final build/test/PoC.\n'
        + json.dumps({'test_coverage_findings': repair_findings(test_findings)}, ensure_ascii=False))
    return [('production', production_feedback), ('test_coverage', test_feedback)]


def build_llm(data, *, temperature=None):
    """按配置构造 LLM 客户端（与是否分组无关）。"""
    request_options = {}
    if getattr(data, 'llm_timeout_seconds', None) is not None:
        request_options['timeout'] = data.llm_timeout_seconds
    if getattr(data, 'llm_max_retries', None) is not None:
        request_options['max_retries'] = data.llm_max_retries

    logger.info("Using DeepSeek OpenAI-compatible endpoint")
    base_url = getattr(data, 'llm_base_url', "https://api.deepseek.com/v1")
    model_name = getattr(data, 'llm_model', "deepseek-flash")
    _proxy = os.environ.get("LLM_PROXY", "").strip()
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
    _temp = float(os.environ.get("LLM_TEMPERATURE", "0.5")) if temperature is None else temperature
    llm = ChatOpenAI(
        temperature=_temp,
        model=model_name,
        api_key=data.api_key,
        openai_api_base=base_url,
        http_client=http_client,
        verbose=True,
        **request_options,
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
    project.query_progress = QueryProgress(project.query_stale_limit)
    viewcode, locate_symbol, validate, git_history, git_show = project.get_tools()
    tools = [viewcode, locate_symbol, validate, git_history, git_show, create_find_references_tool(project)]
    if group_mode and getattr(project, 'api_context', None) is not None:
        tools.append(create_query_api_context_tool(project))
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
        mode="w", delete=False, suffix=".patch", encoding="utf-8", errors="surrogateescape"
    ) as fh:
        fh.write(patch_text if patch_text.endswith("\n") else patch_text + "\n")
        path = fh.name
    args = ["git", "apply", "--index"]
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
    finally:
        os.unlink(path)


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
    """Replay a full candidate exactly once on its original base."""
    repo = project.repo
    try:
        repo.git.reset("--hard")
        repo.git.checkout(base)
        if repo.git.rev_parse("HEAD") != repo.git.rev_parse(base):
            return None
        if not _try_apply(project, candidate, check_only=True, recount=True):
            return None
        if not _try_apply(project, candidate, recount=True):
            return None
        fixed = repo.git.diff(base)
        return fixed + "\n" if fixed.strip() else None
    except Exception as exc:
        logger.warning("Cannot rebuild full patch on its original base: %s", exc)
        return None
    finally:
        repo.git.reset("--hard")
        repo.git.checkout(branch)


def write_patch_atomic(path, text):
    tmp = path + ".tmp.%d" % os.getpid()
    blob = text if text.endswith("\n") else text + "\n"
    with open(tmp, "wb") as fh:
        fh.write(blob.encode("utf-8", "surrogateescape"))
    os.replace(tmp, path)


def evaluate_global_artifact(
    project, complete_patch, complete_path, ported_dir, hunks_by_idx,
    selected_hunks, accepted_need_not_ported, any_strict_requires_patch, cumulative,
):
    """Re-evaluate the final patch and refresh all global status artifacts."""
    reviewer = getattr(project, 'coverage_reviewer', None)
    global_member_idxs = sorted(selected_hunks if reviewer else selected_hunks - accepted_need_not_ported)
    global_members = [hunks_by_idx[i] for i in global_member_idxs if i in hunks_by_idx]
    heuristic_cov = hunk_coverage_report(
        global_members,
        complete_patch,
        need_not_ported=False,
        strict_requires_patch=any_strict_requires_patch,
        directly_applied=False,
    )
    global_cov = reviewer.review(global_members, complete_patch) if reviewer else heuristic_cov
    if reviewer:
        accepted_need_not_ported = {m['idx'] for m in global_cov['members']
                                   if m.get('status') == 'not_applicable' and m['covered']}

    chain_script = os.environ.get("CHAIN_CHECK_SCRIPT") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), '../../../../../chain_check.py'))
    chain_path = os.path.join(ported_dir, "global-api-chain.txt")
    if cumulative and global_cov.get('candidate_tree'):
        chain_env = os.environ.copy()
        chain_env['CHAIN_CHECK_REF'] = global_cov['candidate_tree']
        chain_proc = subprocess.run(
            [sys.executable, chain_script, project.dir, complete_path],
            capture_output=True,
            text=True,
            errors="replace",
            env=chain_env,
        )
        chain_rc = chain_proc.returncode
        chain_report = (chain_proc.stdout or "") + (chain_proc.stderr or "")
    else:
        chain_rc = 2
        chain_report = "API chain check requires a fixed candidate tree in cumulative mode; no working-tree fallback.\n"
    with open(chain_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(chain_report)

    global_complete = (
        bool(complete_patch.strip()) and global_cov["complete"] and chain_rc == 0
    )
    coverage_by_idx = {
        int(m["idx"]): bool(m["covered"]) for m in global_cov["members"]
    }
    for fn in os.listdir(ported_dir):
        if not (fn.startswith("merged-hunk-") and fn.endswith(".status.json")):
            continue
        status_file = os.path.join(ported_dir, fn)
        try:
            with open(status_file, encoding="utf-8") as fh:
                status_data = json.load(fh)
        except Exception:  # noqa: BLE001
            continue
        members = [int(i) for i in status_data.get("member_hunks", [])]
        status_data["global_covered"] = all(
            i in accepted_need_not_ported or coverage_by_idx.get(i, False)
            for i in members
        )
        status_data["global_api_chain_ok"] = chain_rc == 0
        status_data["global_validation_complete"] = global_complete
        if reviewer:
            status_data.setdefault('local_heuristic_complete', status_data.get('complete', False))
            status_data['complete'] = status_data['global_covered']
            status_data['coverage_method'] = global_cov['method']
            status_data['missing_hunks'] = [i for i in members if i in global_cov['missing']]
            status_data['unreviewed_hunks'] = [i for i in members if i in global_cov.get('unreviewed', [])]
            status_data['semantic_members'] = [m for m in global_cov['members'] if m['idx'] in members]
        with open(status_file, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(status_data, fh, ensure_ascii=False, indent=2)

    global_result = {
        "complete": global_complete,
        "patch_bytes": len(complete_patch.encode("utf-8", "surrogateescape"))
        if complete_patch else 0,
        "selected_hunks": sorted(selected_hunks),
        "accepted_need_not_ported_hunks": sorted(accepted_need_not_ported),
        "coverage_hunks": global_member_idxs,
        "coverage": global_cov,
        "heuristic_coverage": heuristic_cov,
        "api_chain_rc": chain_rc,
        "api_chain_report": chain_report,
    }
    with open(
        os.path.join(ported_dir, "global-validation.json"),
        "w", encoding="utf-8", newline="\n",
    ) as fh:
        json.dump(global_result, fh, ensure_ascii=False, indent=2)
    if global_cov["missing"]:
        logger.error("Global coverage missing hunks: %s" % global_cov["missing"])
    if global_cov.get('unreviewed'):
        logger.error("Global semantic review unavailable for hunks: %s" % global_cov['unreviewed'])
    if chain_rc != 0:
        logger.error("Global API chain validation failed; see %s" % chain_path)
    return global_complete, global_result


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
            return True
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
        return True
    except Exception as e:  # noqa: BLE001  -- 累积失败不能弄死整轮
        logger.error("累积提交失败（第 %d 组）: %s: %s" % (gpos, type(e).__name__, e))
        return False


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
    if group.get("strict_requires_patch"):
        lines.append(
            "This group requires verified semantic coverage. Its original target mapping may "
            "be unresolved or lack prerequisites. Inspect the cumulative target: implement any "
            "remaining obligation, or submit concrete evidence that earlier groups satisfy it "
            "or that it is inapplicable. An empty patch alone is never acceptance."
        )
    if group.get("mapping_status"):
        lines.append("Semantic mapping status: %s" % ", ".join(group["mapping_status"]))
    if group.get("hints"):
        lines.append("")
        lines.append(group["hints"])
    return "\n".join(lines)


def dump_portgpt_summary(ported_dir, gpos, label, payload):
    """Persist the exact group summary payload supplied to a PortGPT session."""
    try:
        json_path = os.path.join(
            ported_dir, "portgpt-input-%02d-%s.json" % (gpos, label)
        )
        with open(json_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

        summary_path = os.path.join(ported_dir, "portgpt-summaries.md")
        with open(summary_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("# Group %d - %s\n\n" % (gpos, label))
            fh.write("- target_release: `%s`\n" % payload.get("target_release", ""))
            fh.write("- group_size: %s\n" % payload.get("group_size", ""))
            fh.write("- group_ids: `%s`\n\n" % payload.get("group_ids", []))
            fh.write("## Summary passed to PortGPT\n\n")
            fh.write("```text\n%s\n```\n\n" % payload.get("group_desc", ""))
            if payload.get("similar_block"):
                fh.write("## Similar target blocks also passed to PortGPT\n\n")
                fh.write("```text\n%s\n```\n\n" % payload["similar_block"])
        logger.info("PortGPT summary written to %s", summary_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write PortGPT summary: %s", exc)


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
    project.full_patch_base = cum_base or None
    repair_candidate = getattr(data, 'repair_candidate', None)
    if repair_candidate is not None and (not cumulative or not repair_candidate.strip()):
        raise ValueError('Candidate replay requires cumulative mode and a nonempty complete patch')

    # 输入有两种来源（优先用「已经合并好的文件」）：
    #   1) merged_hunks 目录里的 *.patch —— 每个文件就是一组已经合并好的 hunk，
    #      轮到谁就把谁的内容作为该 session 的输入（可以手工改写这些文件）；
    #   2) 未指定目录时才使用「分组文件 + 现场合并」。空目录/空索引不触发回退。
    groups_spec = getattr(data, "hunk_groups_spec", None)
    merged_input_dir = getattr(data, "merged_hunks_input_dir", "")

    if merged_input_dir:
        merged_hunks = load_merged_hunk_files(merged_input_dir)
        logger.info(
            "Input = %d pre-merged hunk file(s) from %s "
            "(one file = one session input)" % (len(merged_hunks), merged_input_dir)
        )
    else:
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

    project.checkpoint_dir = os.path.join(ported_dir, "validated-checkpoints")
    project.candidate_checkpoint_dir = ported_dir
    os.makedirs(project.checkpoint_dir, exist_ok=True)

    if not merged_hunks:
        logger.error(
            "输入包含 0 个移植组；不回退到原始 patch，未执行补丁验证，退出"
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
    group_comparison = GroupComparison(ported_dir, selected, logger) if repair_candidate is None else None

    # 组内成员 hunk 的原文，用来做「这一组是否被完整移植」的机械校验
    _, all_hunks = split_patch_indexed(patch, True)
    hunks_by_idx = {h["idx"]: h for h in all_hunks}
    # 覆盖校验是启发式：实测两次"失败"都是误报（标识符被改名），而每次重试要一整轮
    # session（≈20 分钟）。所以默认只记录、不重试；需要时可 --coverage-retries 1 打开。
    coverage_retries = int(getattr(data, "coverage_retries", 0) or 0)

    # [C] 跨组共享变更账本：后面的组能看到前面各组「实际产出了什么」。
    # 各组会话独立；累积模式中的源码已包含前面组的提交。
    ledger = ChangeLedger(int(getattr(data, "ledger_chars", 6000)))
    catalog = (ApiCatalog(project.source_index, data.new_patch_parent, data.new_patch, all_hunks)
               if repair_candidate is None else None)

    all_groups_complete = True
    selected_hunks = set()
    accepted_need_not_ported = set()
    any_strict_requires_patch = False
    if repair_candidate is not None:
        selected_hunks = {int(i) for _, group in selected for i in group.get('hunks', [])}
        any_strict_requires_patch = any(group.get('strict_requires_patch', False) for _, group in selected)
        logger.info('REPLAY_COMPLETE_CANDIDATE: skipping group generation; reviewing %d upstream hunks',
                    len(selected_hunks))
        with open(os.path.join(ported_dir, 'replay-input.json'), 'w', encoding='utf-8') as handle:
            json.dump(dict(base=project.repo.git.rev_parse(cum_base or data.target_release),
                           patch_sha256=hashlib.sha256(repair_candidate.encode('utf-8', 'surrogateescape')).hexdigest(),
                           selected_hunks=sorted(selected_hunks)), handle, indent=2)
    for gpos, group in ([] if repair_candidate is not None else selected):
        pp = group["text"]
        members = group["hunks"] or ["?"]
        group_tag = "merged hunk %d (original hunks %s, group %s)" % (
            gpos,
            members,
            group["gid"],
        )
        member_hunks = [hunks_by_idx[i] for i in group["hunks"] if i in hunks_by_idx]
        inventory = catalog.inventory(data.target_release, member_hunks, group.get("target_symbols", []))
        artifact_name = "merged-hunk-%02d.api-inventory.json" % gpos
        with open(os.path.join(ported_dir, artifact_name), "w", encoding="utf-8") as handle:
            json.dump(inventory, handle, ensure_ascii=False, indent=2)
        project.api_context = ApiContext(
            inventory, artifact_name,
            page_limit=getattr(data, 'context_query_limit', 10),
            max_chars=getattr(data, 'context_query_chars', 24000),
            inline_chars=getattr(data, 'context_inline_chars', 6000))
        api_note = project.api_context.prompt()
        status_path = os.path.join(ported_dir, "merged-hunk-%02d.status.json" % gpos)
        project.round_succeeded = False
        project.context_mismatch_times = 0
        project.no_change_evidence = None
        n_before = len(project.succeeded_patches)
        executions = []
        directly_applied = False
        strict_requires_patch = bool(group.get("strict_requires_patch", False))
        any_strict_requires_patch = any_strict_requires_patch or strict_requires_patch
        selected_hunks.update(int(i) for i in group.get("hunks", []))

        group_base = None
        if cumulative:
            try:
                group_base = project.repo.git.rev_parse(data.target_release)
            except Exception as exc:  # Reporting must not affect porting.
                logger.warning("Cannot read group %d base for comparison: %s", gpos, exc)

        def produced_patches():
            candidates = project.succeeded_patches[n_before:]
            return candidates[-1:]

        def report_group():
            if cumulative:
                if group_base is None:
                    group_comparison.record(gpos, "", "无法读取本组提交差异")
                    return
                try:
                    output = project.repo.git.diff(group_base, data.target_release)
                except Exception as exc:  # Reporting must not affect porting.
                    logger.warning("Cannot read group %d diff for comparison: %s", gpos, exc)
                    group_comparison.record(gpos, "", "无法读取本组提交差异")
                    return
            else:
                # Each validation starts from the same ref; revisions replace
                # the previous candidate rather than being stacked together.
                patches = produced_patches()
                output = patches[-1] if patches else ""
            skipped = project.round_succeeded and not produced_patches() and not strict_requires_patch
            group_comparison.record(gpos, output, "无需移植" if skipped else "未产出补丁")

        def save_ported():
            """把本组产出的 patch 存到 ported/merged-hunk-NN.patch。"""
            new_patches = produced_patches()
            if not new_patches:
                path = os.path.join(ported_dir, "merged-hunk-%02d.patch" % gpos)
                if os.path.isfile(path):
                    os.unlink(path)
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
            need_not_ported = (project.round_succeeded and not produced
                               and not strict_requires_patch and not directly_applied)
            return hunk_coverage_report(
                member_hunks, "\n".join(produced), need_not_ported,
                strict_requires_patch=strict_requires_patch,
                directly_applied=directly_applied,
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
                        "strict_requires_patch": strict_requires_patch,
                        "mapping_status": group.get("mapping_status", []),
                        "coverage_attempts": attempts,
                        "members": cov["members"],
                        "no_change_evidence": project.no_change_evidence,
                        "requires_final_semantic_review": True,
                        "context_reads": project.api_context.read_report(),
                        "executions": executions,
                    },
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )

        def ledger_note():
            files = set(group.get('files', []))
            files.update(d['path'] for api in inventory['apis'] for d in api['definitions'])
            symbols = [api['symbol'] for api in inventory['apis']] + group.get('target_symbols', [])
            return (ledger.render(files, symbols, cumulative) +
                    target_context(project.source_index, project.repo, data.target_release, group, ledger.limit))

        def finalize_group():
            committed = True
            if cumulative:
                committed = commit_group(project, produced_patches(), gpos, group)
                output = project.repo.git.diff(group_base, data.target_release) if group_base else ''
                if not committed:
                    project.round_succeeded = False
                    executions.append(dict(reason='group_commit_failed', output='No accepted group commit'))
            else:
                patches = produced_patches()
                output = patches[-1] if patches else ''
            project.succeeded_patches[n_before:] = [output] if output.strip() else []
            ledger.record(gpos, group['hunks'], group_base,
                          project.repo.git.rev_parse(data.target_release), output)
            save_ported()
            return committed

        def invoke_session(executor, payload):
            project.query_progress = QueryProgress(getattr(project, 'query_stale_limit', 4))
            result, error = None, None
            try:
                project.api_context.record_inline_read(payload.get('group_desc', ''))
                project.query_progress.seed_feedback(payload.get('group_desc', ''))
                for ref, path, line in project.query_progress.seeded_source:
                    project.api_context.record_source_read(ref, path, line, line)
                result = executor.invoke(payload, {"callbacks": [log_handler]})
            except Exception as exc:  # A failed session remains visible in its group status.
                error = exc
                logger.error('%s session failed: %s: %s', group_tag, type(exc).__name__, exc)
                project.round_succeeded = False
            record = execution_result(result, project.round_succeeded, error)
            progress = getattr(project, 'query_progress', None)
            if progress is not None:
                record['query_progress'] = progress.report()
            executions.append(record)

        try:
            ret = project._apply_hunk(data.target_release, pp, False)
        except Exception as e:  # noqa: BLE001  -- [P0] 单组异常不能弄死整个运行
            logger.error(
                "%s 在 _apply_hunk 抛异常: %s: %s" % (group_tag, type(e).__name__, e)
            )
            ret = "TARGET_FILE_MISSING: %s could not be applied (%s)" % (group_tag, e)
            project.round_succeeded = False
        if project.round_succeeded:
            directly_applied = True
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
            payload = {
                "project_url": data.project_url,
                "new_patch_parent": data.new_patch_parent,
                "new_patch": pp,
                "target_release": data.target_release,
                "similar_block": similar_block,
                "group_size": len(group["hunks"]),
                "group_ids": group["hunks"],
                "group_desc": describe_group(group) + api_note + ledger_note(),
            }
            dump_portgpt_summary(ported_dir, gpos, "initial", payload)
            invoke_session(executor, payload)
            if not project.round_succeeded:
                logger.debug(
                    f"Failed to backport the {group_tag} \n----------------------------------\n{pp}\n----------------------------------\n"
                )
                logger.error("%s: %s", group_tag, executions[-1]["reason"])
                finalize_group()
                # [P0] 一组失败不再终止整个运行：记录状态后继续下一组
                save_status(check_coverage(), 0)
                all_groups_complete = False
                report_group()
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
            payload = {
                "project_url": data.project_url,
                "new_patch_parent": data.new_patch_parent,
                "new_patch": pp,
                "target_release": data.target_release,
                "similar_block": "",
                "group_size": len(group["hunks"]),
                "group_ids": group["hunks"],
                "group_desc": describe_group(group) + api_note + ledger_note() + note,
            }
            dump_portgpt_summary(
                ported_dir, gpos, "retry-%02d" % attempts, payload
            )
            invoke_session(executor, payload)
            if not project.round_succeeded:
                # 重试没成，回滚到上一次的产出，别把已经拿到的部分丢掉
                project.succeeded_patches.extend(previous)
                cov = check_coverage()
                break
            cov = check_coverage()

        if not cov["complete"] and strict_requires_patch and not produced_patches() and not project.no_change_evidence:
            project.round_succeeded = False
        if not finalize_group():
            cov = check_coverage()
        save_status(cov, attempts)
        if cov.get("need_not_ported"):
            accepted_need_not_ported.update(int(i) for i in group.get("hunks", []))
        if not cov["complete"]:
            all_groups_complete = False
        if cumulative:
            if os.environ.get("PG_COMPILE_PER_GROUP") == "1":
                compile_group(project, data, gpos,
                              os.path.join(ported_dir, "compile-per-group.log"))
        report_group()
        if cov["complete"]:
            logger.debug(
                "%s 覆盖校验通过（%d 个成员 hunk 都有痕迹）"
                % (group_tag, len(member_hunks))
            )
        else:
            logger.info(
                "%s 局部启发式未确认 hunk %s；等待最终语义审查（组内重试 %d 次）"
                % (group_tag, cov["missing"], attempts)
            )
            logger.info("    详情见 %s" % status_path)

    project.api_context = None
    project.all_hunks_applied_succeeded = all_groups_complete
    logger.info(f"Aplly all hunks in the patch      PASS")
    project.now_hunk = "completed"
    if repair_candidate is not None:
        complete_patch = repair_candidate
    elif cumulative and cum_base:
        # 累积模式下，各组已经提交到分支；最终产物 = base..HEAD 的 diff
        project.repo.git.reset("--hard")
        # 用分支名而不是 HEAD：模型在会话里用 viewcode 看过别的 ref 时，HEAD 可能
        # 停在别处（实测 48550 因此 diff 出整仓库 42609 个文件、385MB）
        complete_patch = project.repo.git.diff("%s..%s" % (cum_base, data.target_release))
    else:
        complete_patch = "\n".join(project.succeeded_patches)

    # An all-need-not-ported result has no auditable patch. Treat it as evidence
    # required, not as a successful backport; this also prevents the final
    # validation path from reporting success for an empty patch.
    if not complete_patch.strip():
        all_groups_complete = False
        project.round_succeeded = False

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

    # ---- Global validation over the FINAL composed patch --------------------
    # Review each candidate after its build/test attempt, and again after the
    # validated artifact is rebuilt. Review outages must not skip compilation.
    validation_base = project.repo.git.rev_parse(cum_base or data.target_release)
    review_temperature = getattr(data, 'coverage_temperature', 0.0)
    review_llm = llm.bind(temperature=review_temperature) if llm is not None else None
    if getattr(data, 'coverage_model', None) and data.coverage_model != getattr(data, 'llm_model', None):
        review_data = copy(data)
        review_data.llm_model = data.coverage_model
        review_llm = build_llm(review_data, temperature=review_temperature)
    project.coverage_reviewer = CoverageReviewer(
        review_llm, project.dir, validation_base,
        max_iterations=getattr(data, 'coverage_review_iterations', 20),
        retries=None if getattr(data, 'unlimited_backend', False) else getattr(data, 'coverage_review_retries', 1),
        artifact_dir=os.path.join(ported_dir, 'coverage-review'),
        upstream_parent=data.new_patch_parent, upstream_commit=data.new_patch)

    def final_artifact_check(candidate):
        # The same check protects acceptance of every compiled repair candidate.
        with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', encoding='utf-8',
                                         errors='surrogateescape') as artifact:
            artifact.write(candidate if candidate.endswith('\n') else candidate + '\n')
            artifact.flush()
            complete, report = evaluate_global_artifact(
                project, candidate, artifact.name, ported_dir, hunks_by_idx,
                selected_hunks, accepted_need_not_ported, any_strict_requires_patch, cumulative)
        project.latest_candidate_patch = candidate
        project.latest_candidate_tree = report['coverage'].get('candidate_tree')
        project.final_validation_api_failed = report['api_chain_rc'] == 1
        review_failed = report['coverage']['method'] == 'semantic_review_failed'
        project.final_validation_findings = [m for m in report['coverage']['members']
            if not m.get('covered') and (not review_failed or m.get('status') == 'missing')]
        chain_failed = report['api_chain_rc'] not in (0, 1)
        if review_failed or chain_failed:
            unavailable, findings = [], []
            if review_failed:
                unavailable.append('Semantic review: ' + str(report['coverage'].get('error')))
                if project.final_validation_findings:
                    findings.append('SEMANTIC_VALIDATION_FAILED: ' + json.dumps({
                        'members': repair_findings(project.final_validation_findings),
                        'missing': [m['idx'] for m in project.final_validation_findings],
                        'unreviewed': report['coverage'].get('unreviewed', []),
                    }, ensure_ascii=False))
            elif not report['coverage']['complete']:
                findings.append('SEMANTIC_VALIDATION_FAILED: ' + json.dumps(dict(report['coverage'],
                    members=repair_findings(report['coverage']['members'])), ensure_ascii=False))
            if chain_failed:
                unavailable.append('API check could not run: ' + report['api_chain_report'])
            elif report['api_chain_rc'] == 1:
                findings.append('API_CHAIN_VALIDATION_FAILED:\n' + report['api_chain_report'])
            raise FinalValidationUnavailable('\n'.join(unavailable), '\n'.join(findings))
        if complete:
            return ''
        return 'SEMANTIC_VALIDATION_FAILED: ' + json.dumps({
            'missing': report['coverage']['missing'],
            'members': repair_findings([m for m in report['coverage']['members'] if not m['covered']]),
            'review_error': report['coverage'].get('error'),
            'api_chain': report['api_chain_report'],
        }, ensure_ascii=False)

    if not complete_patch.strip():
        logger.error('Backport INCOMPLETE: no nonempty candidate to validate or repair.')
        return
    # Full-patch mode runs build/test before checking semantic acceptance.
    project.all_hunks_applied_succeeded = True
    project.final_artifact_check = final_artifact_check

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
    project.full_patch_base = validation_base
    validate_ret = project._validate(validation_base, complete_patch)
    project.latest_validation_result = validate_ret
    if getattr(project, 'validation_infrastructure_failure', None):
        logger.error('VALIDATION_INFRASTRUCTURE_FAILURE: retaining candidate; code repair not started: %s',
                     project.validation_infrastructure_failure)
        return
    if project.final_validation_unavailable and not project.final_repair_required:
        logger.error('Backport INCOMPLETE: build/test passed, but final validation is unavailable. '
                     'Candidate retained; no concrete code defect was found to send for repair.')
        return
    if project.poc_succeeded:
        project.persist_validated_patch(complete_patch, validation_base)
        write_patch_atomic(complete_path, complete_patch)
        final_checkpoint = os.path.join(project.checkpoint_dir, "final-complete.patch")
        write_patch_atomic(final_checkpoint, complete_patch)
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
    unlimited = getattr(data, 'unlimited_backend', False)
    project.unlimited_backend = unlimited
    stale_limit = None if unlimited else project.query_stale_limit
    project.query_progress = QueryProgress(stale_limit)
    project.final_repair_read_limit = None if unlimited else 40000
    viewcode, locate_symbol, _, _, _ = project.get_tools()
    tools = [viewcode, locate_symbol, create_find_references_tool(project),
             create_submit_candidate_tool(project)]
    agent = create_tool_calling_agent(llm, tools, prompt)
    agent_executor = CandidateRepairExecutor(
        agent=agent, tools=tools, verbose=True,
        max_iterations=None if unlimited else getattr(data, 'final_repair_iterations', 20)
    )
    findings = getattr(project, 'final_validation_findings', [])
    production_required = (not project.compile_succeeded or any(
        not _is_test_coverage_finding(m) for m in findings))
    stages = _repair_feedback_stages(validate_ret, findings, production_required)
    retry_limit = None if unlimited else getattr(data, 'final_repair_retries', 2)
    switch_limit = None if unlimited else getattr(data, 'final_repair_category_switches', 2)
    logger.info('BACKEND_LIMITS %s', json.dumps(dict(unlimited_backend=unlimited,
        iterations=None if unlimited else getattr(data, 'final_repair_iterations', 20), repair_retries=retry_limit,
        category_switches=switch_limit, stale_queries=stale_limit,
        query_characters=project.final_repair_read_limit,
        coverage_review_retries=None if unlimited else getattr(data, 'coverage_review_retries', 1))))
    last_stage, stage_attempts = None, 0
    validation_failures = 0
    category_switches = 0

    def candidate_key(candidate):
        return getattr(project, 'latest_candidate_tree', None) or hashlib.sha256(
            candidate.encode('utf-8', 'surrogateescape')).hexdigest()

    # The initial full validation has already run. Do not rebuild an unchanged
    # artifact merely because a model rewrote its diff formatting or verdict.
    validated_candidates = {candidate_key(complete_patch)}
    while stages:
        stage, feedback = stages.pop(0)
        if stage != last_stage:
            last_stage, stage_attempts = stage, 0
        if retry_limit is not None and stage_attempts >= retry_limit + 1:
            logger.error('REPAIR_STAGE_INCOMPLETE stage=%s; repair budget exhausted' % stage)
            project.round_succeeded = False
            return
        stage_attempts += 1
        previous = complete_patch
        project.latest_candidate_patch = previous
        project.candidate_submitted = False
        project.full_validation_allowed = False
        project.semantic_candidate_ready = False
        agent_executor.max_iterations = None if unlimited else getattr(data, 'final_repair_iterations', 20)
        if not unlimited and stage in {'test_coverage', 'test_validation'}:
            agent_executor.max_iterations = max(30, agent_executor.max_iterations)
        if stage == 'test_coverage':
            current_test_findings = [m for m in getattr(project, 'final_validation_findings', [])
                                     if _is_test_coverage_finding(m)]
            feedback = getattr(project, 'latest_validation_result', feedback) + (
                '\nCURRENT STAGE: repair every current test-coverage finding below; this supersedes '
                'earlier finding lists. Submit a local diff against candidate_ref (the program records the complete replacement patch) through '
                'submit_candidate. When semantic checks pass, stop this session without further '
                'edits or tool calls; the orchestrator will run final build/test/PoC.\n'
                + json.dumps({'test_coverage_findings': repair_findings(current_test_findings)}, ensure_ascii=False))
        project.final_repair_reads_remaining = (None if unlimited else
                                               max(4, agent_executor.max_iterations - 4))
        try:
            candidate_ref = artifact_tree(project.dir, validation_base, previous)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error('CANDIDATE_SOURCE_UNAVAILABLE: retaining the unapplied candidate; '
                         'local repair cannot run without a fixed source tree: %s', exc)
            project.round_succeeded = False
            return
        project.repair_candidate_ref = candidate_ref
        # Each invocation has a fresh message history. Deduplication must not hide
        # source delivered only to a previous session.
        project.query_progress = QueryProgress(stale_limit)
        project.query_progress.seed_feedback(feedback)
        try:
            agent_executor.invoke(
                {
                    "project_url": data.project_url,
                    "new_patch_parent": data.new_patch_parent,
                    "target_release": validation_base,
                    "new_patch": patch,
                    "complete_patch": previous,
                    "candidate_ref": candidate_ref,
                    "compile_ret": feedback,
                },
                {"callbacks": [log_handler]},
            )
        except Exception as e:  # noqa: BLE001
            if project.candidate_submitted:
                logger.warning(
                    "Final repair session ended with %s after candidate submission; "
                    "processing the recorded candidate." % type(e).__name__
                )
            else:
                logger.error("Final repair session failed during %s: %s: %s" % (
                    stage, type(e).__name__, e))
                project.round_succeeded = False
                return
        complete_patch = project.latest_candidate_patch
        if not project.candidate_submitted:
            logger.error("REPAIR_NO_CANDIDATE stage=%s; the agent submitted no complete patch" % stage)
            project.round_succeeded = False
            return
        if project.final_validation_unavailable and not project.final_repair_required:
            logger.error('REPAIR_VALIDATION_UNAVAILABLE: no concrete defect to repair')
            project.round_succeeded = False
            return
        if project.semantic_candidate_ready and not project.final_repair_required:
            key = candidate_key(complete_patch)
            if key in validated_candidates:
                logger.error('REPAIR_NO_PROGRESS: this candidate already failed full validation')
                project.round_succeeded = False
                return
            validated_candidates.add(key)
            # Full validation is an orchestration step, never another LLM session.
            logger.info('Final candidate semantics passed; running build/test/PoC')
            latest_result = project._validate(validation_base, complete_patch)
            project.latest_validation_result = latest_result
            if getattr(project, 'validation_infrastructure_failure', None):
                logger.error('VALIDATION_INFRASTRUCTURE_FAILURE: retaining candidate without code retry: %s',
                             project.validation_infrastructure_failure)
                return
            if project.poc_succeeded:
                project.persist_validated_patch(complete_patch, validation_base)
                break
            if project.final_validation_unavailable and not project.final_repair_required:
                logger.error('REPAIR_VALIDATION_UNAVAILABLE: full validation cannot be accepted')
                project.round_succeeded = False
                return
            validation_failures += 1
            if retry_limit is not None and validation_failures > retry_limit:
                logger.error('REPAIR_VALIDATION_FAILED: full validation retry budget exhausted')
                project.round_succeeded = False
                return
            findings = getattr(project, 'final_validation_findings', [])
            if findings or not project.compile_succeeded:
                stages = _repair_feedback_stages(latest_result, findings,
                    not project.compile_succeeded or any(
                        not _is_test_coverage_finding(m) for m in findings))
            else:
                stages = [('test_validation', latest_result + (
                    '\nCURRENT STAGE: repair the reported test/PoC failure. '
                    'Submit the local diff against candidate_ref (the program records the complete replacement patch) through submit_candidate.\n'))]
            last_stage = None
            continue
        if complete_patch == previous:
            logger.error('REPAIR_NO_PROGRESS stage=%s; unchanged candidate still has findings' % stage)
            project.round_succeeded = False
            return
        current_findings = getattr(project, 'final_validation_findings', [])
        production_remaining = [m for m in current_findings if not _is_test_coverage_finding(m)]
        test_remaining = [m for m in current_findings if _is_test_coverage_finding(m)]
        api_failed = getattr(project, 'final_validation_api_failed', False)
        latest_result = getattr(project, 'latest_validation_result', validate_ret)
        if stage in {'test_coverage', 'test_validation'} and (production_remaining or api_failed):
            if switch_limit is not None and category_switches >= switch_limit:
                logger.error("REPAIR_STAGE_INCOMPLETE stage=%s; production findings keep "
                             "reappearing after test repair: %s" % (
                                 stage, json.dumps(production_remaining, ensure_ascii=False)))
                project.round_succeeded = False
                return
            production_feedback = latest_result + (
                '\nCURRENT STAGE: test repair exposed production-code gaps. Repair only these '
                'production findings now; the remaining test findings will be re-evaluated '
                'afterward. Submit the local diff against candidate_ref (the program records the complete replacement patch) through submit_candidate.\n'
                + json.dumps({'production_findings': repair_findings(production_remaining)}, ensure_ascii=False))
            stages = [('production', production_feedback)]
            category_switches += 1
            continue
        if stage == 'production' and not production_remaining and not api_failed and test_remaining:
            stages = [('test_coverage', '')]
            continue
        if stage == 'test_validation' and test_remaining:
            # A compiler repair can expose a coverage gap; route to the current kind
            # of finding instead of asking another session to fix an obsolete test error.
            stages = [('test_coverage', '')]
            continue
        remaining = (production_remaining if stage == 'production' else
                     test_remaining if stage == 'test_coverage' else current_findings)
        stages = [(stage, latest_result + (
            '\nCURRENT STAGE: continue repairing only the unresolved %s findings below. '
            'Use submit_candidate to submit the local diff against candidate_ref (the program records the complete replacement patch).\n%s\n' % (
                stage, json.dumps({'unresolved_findings': repair_findings(remaining)}, ensure_ascii=False))))]
    if project.poc_succeeded:
        logger.info(
            f"Successfully backport the patch to the target release {data.target_release}"
        )
        # Final validation accepts full patches against validation_base only.
        # Rebuild on that same base; never reinterpret a failed full patch as
        # a fragment to stack on the cumulative branch.
        candidates = []
        if project.succeeded_patches:
            candidates.append(project.succeeded_patches[-1])
        latest_checkpoint = os.path.join(
            project.checkpoint_dir, "latest-validated.patch"
        )
        if os.path.isfile(latest_checkpoint):
            with open(latest_checkpoint, encoding="utf-8") as checkpoint_file:
                checkpoint_text = checkpoint_file.read()
            if checkpoint_text and checkpoint_text not in candidates:
                candidates.append(checkpoint_text)

        fixed = None
        if cumulative and cum_base:
            for candidate in reversed(candidates):
                fixed = rebuild_from_candidate(
                    project, validation_base, data.target_release, candidate
                )
                if fixed:
                    break
        elif candidates:
            fixed = candidates[-1]

        if not fixed or not fixed.strip():
            project.poc_succeeded = False
            project.round_succeeded = False
            if os.path.exists(complete_path):
                stale_path = complete_path + ".stale"
                try:
                    os.replace(complete_path, stale_path)
                except OSError:
                    pass
            logger.error(
                "Validated checkpoint could not be rebuilt into a complete patch; "
                "removed the stale complete.patch so it cannot be reported as OK."
            )
            return

        try:
            write_patch_atomic(complete_path, fixed)
            write_patch_atomic(
                os.path.join(project.checkpoint_dir, "final-complete.patch"), fixed
            )
            project.succeeded_patches.clear()
            project.succeeded_patches.append(fixed)
            logger.info(
                "Validated checkpoint rebuilt into complete.patch (%d bytes)"
                % len(fixed.encode("utf-8", "surrogateescape"))
            )
        except OSError as e:  # noqa: BLE001
            project.poc_succeeded = False
            project.round_succeeded = False
            logger.error("Writing rebuilt complete.patch failed: %s" % e)
            return

        if cumulative and cum_base:
            # Chain validation must inspect the code produced by the rebuilt
            # artifact, not the stale cumulative branch that preceded repair.
            project.repo.git.reset("--hard")
            project.repo.git.checkout(cum_base)
            if not _try_apply(project, fixed):
                project.repo.git.reset("--hard")
                project.repo.git.checkout(data.target_release)
                project.poc_succeeded = False
                project.round_succeeded = False
                logger.error("Rebuilt complete patch no longer applies to clean base")
                return
            global_complete, global_result = evaluate_global_artifact(
                project, fixed, complete_path, ported_dir, hunks_by_idx,
                selected_hunks, accepted_need_not_ported,
                any_strict_requires_patch, cumulative,
            )
            project.repo.git.reset("--hard")
            project.repo.git.checkout(data.target_release)
            if not global_complete:
                project.poc_succeeded = False
                project.round_succeeded = False
                stale_path = complete_path + ".stale"
                try:
                    os.replace(complete_path, stale_path)
                except OSError:
                    pass
                logger.error(
                    "Rebuilt patch failed final global validation; stale complete.patch removed"
                )
                return
        for patch in project.succeeded_patches:
            logger.info(patch)
    else:
        logger.error(
            f"Failed backport the patch to the target release {data.target_release}"
        )
