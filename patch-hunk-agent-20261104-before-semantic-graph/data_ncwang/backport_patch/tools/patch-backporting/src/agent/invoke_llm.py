import json
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
from langchain_core.messages import HumanMessage, SystemMessage
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
from tools.project import Project
from agent.coverage import CoverageReviewer
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
    _temp = float(os.environ.get("LLM_TEMPERATURE", "0.5"))
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
    (
        viewcode,
        locate_symbol,
        validate,
        git_history,
        git_show,
        resolve_mapping,
    ) = project.get_tools()
    tools = [viewcode, locate_symbol, validate, git_history, git_show, resolve_mapping]
    agent = create_tool_calling_agent(llm, tools, prompt)
    agent_executor = AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=debug_mode,
        max_iterations=max_iterations,
        return_intermediate_steps=True,
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


def quarantine_patch(path):
    """Move a final artifact off the success path while preserving evidence."""
    if path and os.path.exists(path):
        try:
            os.replace(path, path + ".stale")
        except OSError:
            pass


def promote_pending_patch(pending_path, final_path):
    """Promote only a nonempty, fully validated pending artifact."""
    if not pending_path or not os.path.isfile(pending_path):
        return False
    try:
        if os.path.getsize(pending_path) == 0:
            return False
        os.replace(pending_path, final_path)
        return True
    except OSError:
        return False


def final_candidate_sources(project, checkpoint_path):
    """Prefer the current validated candidate; checkpoints are resume fallback only."""
    if project.succeeded_patches:
        current = project.succeeded_patches[-1]
        if current and current.strip():
            return [current]
    if checkpoint_path and os.path.isfile(checkpoint_path):
        try:
            checkpoint = open(checkpoint_path, encoding="utf-8").read()
        except OSError:
            checkpoint = ""
        if checkpoint and checkpoint.strip():
            return [checkpoint]
    return []


def validate_direct_apply_safety(project, ref, patch):
    """Direct textual application must also satisfy symbol and guard checks."""
    symbol_report = project.post_apply_symbol_check(
        ref, patch, use_full_base=False
    )
    if symbol_report.get("status") not in {"PASS", "NOT_APPLICABLE"}:
        return symbol_report, False
    guard_report = project.post_apply_null_guard_check(patch)
    return guard_report, guard_report.get("status") in {
        "PASS", "NOT_APPLICABLE"
    }


def run_chain_check(project, candidate, base_ref, report_path):
    """Run the deterministic API-chain check against the candidate patch."""
    chain_script = os.environ.get("CHAIN_CHECK_SCRIPT", "/data/ncwang/chain_check.py")
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".patch",
        encoding="utf-8",
        errors="surrogateescape",
        delete=False,
    ) as artifact:
        artifact.write(candidate if candidate.endswith("\n") else candidate + "\n")
        patch_path = artifact.name
    try:
        env = os.environ.copy()
        env["CHAIN_CHECK_BASE_REF"] = base_ref
        proc = subprocess.run(
            [sys.executable, chain_script, project.dir, patch_path],
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
        )
        report = (proc.stdout or "") + (proc.stderr or "")
        with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(report)
        return proc.returncode, report
    finally:
        os.unlink(patch_path)


def sha256_text(text):
    import hashlib

    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def sha256_file(path):
    import hashlib

    if not os.path.isfile(path):
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path, value):
    tmp = path + ".tmp.%d" % os.getpid()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def restore_cumulative_checkpoints(project, data, selected, ported_dir):
    """Reuse the longest contiguous prefix of successfully processed groups."""
    state = getattr(data, "cumulative_resume_state", None)
    if not state:
        return {}
    if getattr(data, "hunk_group_index", None) not in (None, ""):
        return {}

    reusable = {}
    entries = state.get("groups", {})
    last_commit = project.repo.git.rev_parse(state["base"])
    for gpos, group in sorted(selected):
        entry = entries.get(str(gpos))
        if not entry or entry.get("input_sha256") != sha256_text(group["text"]):
            break
        status_path = os.path.join(
            ported_dir, "merged-hunk-%02d.status.json" % gpos
        )
        patch_path = os.path.join(ported_dir, "merged-hunk-%02d.patch" % gpos)
        try:
            with open(status_path, encoding="utf-8") as fh:
                status = json.load(fh)
            if not status.get("round_succeeded"):
                break
            commit = project.repo.git.rev_parse("--verify", entry["commit"])
            expected_patch = entry.get("patch_sha256", "")
            if expected_patch and sha256_file(patch_path) != expected_patch:
                break
        except Exception:  # noqa: BLE001
            break
        reusable[gpos] = (status, patch_path)
        last_commit = commit

    # Drop uncheckpointed commits and any later groups after the first gap.
    project.repo.git.reset("--hard", last_commit)
    state["groups"] = {
        str(gpos): entries[str(gpos)] for gpos in sorted(reusable)
    }
    state["head"] = last_commit
    write_json_atomic(data.cumulative_resume_path, state)
    if reusable:
        logger.info(
            "Resume checkpoint accepted %d group(s); branch reset to %s",
            len(reusable),
            last_commit[:12],
        )
    elif state:
        logger.warning("Resume checkpoint had no reusable group; restarting from base")
    return reusable


def save_cumulative_checkpoint(data, project, gpos, group, patch_path, cov):
    path = getattr(data, "cumulative_resume_path", "")
    if not path or not project.round_succeeded:
        return
    state = getattr(data, "cumulative_resume_state", None)
    if not state:
        state = {
            "schema": 1,
            "fingerprint": getattr(data, "cumulative_resume_fingerprint", ""),
            "base": project.repo.git.rev_parse(data.cum_base),
            "branch": data.target_release,
            "head": project.repo.git.rev_parse(data.target_release),
            "groups": {},
        }
        data.cumulative_resume_state = state
    state["head"] = project.repo.git.rev_parse(data.target_release)
    state.setdefault("groups", {})[str(gpos)] = {
        "gid": group.get("gid"),
        "member_hunks": group.get("hunks", []),
        "input_sha256": sha256_text(group["text"]),
        "patch_sha256": sha256_file(patch_path),
        "commit": state["head"],
        "round_succeeded": bool(project.round_succeeded),
        "complete": bool(cov.get("complete")),
    }
    write_json_atomic(path, state)
    logger.debug("Resume checkpoint saved after group %d at %s", gpos, state["head"][:12])


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
    capability_resolutions = (
        list(getattr(getattr(project, "mapping_resolver", None), "capabilities", []) or [])
    )
    global_cov = (
        reviewer.review(
            global_members,
            complete_patch,
            capability_resolutions=capability_resolutions,
        )
        if reviewer
        else heuristic_cov
    )
    if reviewer:
        accepted_need_not_ported = {m['idx'] for m in global_cov['members']
                                   if m.get('status') == 'not_applicable' and m['covered']}

    chain_script = os.environ.get("CHAIN_CHECK_SCRIPT", "/data/ncwang/chain_check.py")
    chain_path = os.path.join(ported_dir, "global-api-chain.txt")
    if cumulative:
        chain_env = os.environ.copy()
        if global_cov.get('candidate_tree'):
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
        chain_rc = 1
        chain_report = "API chain check requires cumulative mode.\n"
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
            status_data['missing_hunks'] = [i for i in members if not coverage_by_idx.get(i, False)]
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


def _extract_candidate_patch(value):
    """Extract the first complete unified diff from an Agent/model response."""
    if isinstance(value, dict):
        for key in ("output", "candidate_patch", "patch", "text"):
            candidate = _extract_candidate_patch(value.get(key))
            if candidate:
                return candidate
        return ""
    text = getattr(value, "content", value)
    if isinstance(text, list):
        text = "\n".join(
            item if isinstance(item, str)
            else item.get("text", "") if isinstance(item, dict) else str(item)
            for item in text
        )
    text = str(text or "")
    fenced = re.findall(r"```(?:diff|patch)?\s*(.*?)```", text, re.S | re.I)
    for block in fenced:
        stripped = block.strip()
        if "diff --git " in stripped or stripped.startswith("--- a/"):
            return stripped + "\n"
    starts = [
        pos for pos in (
            text.find("diff --git "),
            text.find("\n--- a/"),
            text.find("--- a/"),
        )
        if pos >= 0
    ]
    if not starts:
        return ""
    candidate = text[min(starts):].strip()
    return candidate + "\n" if candidate else ""


def _compact_agent_trace(result, limit=7000):
    result = result if isinstance(result, dict) else {}
    lines = []
    for action, observation in (result.get("intermediate_steps") or [])[-10:]:
        tool_name = getattr(action, "tool", type(action).__name__)
        tool_input = getattr(action, "tool_input", None)
        lines.append(
            "%s(%s): %s" % (
                tool_name,
                json.dumps(tool_input, ensure_ascii=False)[:300],
                str(observation or "")[:600],
            )
        )
    return "\n".join(lines)[:limit]


def finalize_and_validate_candidate(
    llm, project, ref, group, original_patch, agent_result
):
    """Force one no-tool finalization and mechanically validate its diff."""
    candidate = _extract_candidate_patch(agent_result)
    source = "agent_output"
    validation_result = ""
    if candidate:
        project.round_succeeded = False
        validation_result = project._validate(
            ref,
            candidate,
            "complete candidate produced by agent output after session ended",
        )
        if project.round_succeeded:
            return True, validation_result

    if llm is not None and hasattr(llm, "invoke"):
        prompt = """
You are the final patch finalizer. You have no tools and must not explain your reasoning.
Using the upstream group, compact tool evidence and target constraints below, return ONE
complete unified diff against the stated validation ref. Do not omit callers or signature
changes. If no safe complete patch can be produced, return exactly NO_PATCH.

Validation ref: %s

Group:
%s

Upstream group patch:
%s

Tool evidence:
%s

Previous final output validation:
%s
""" % (
            ref,
            describe_group(group)[:4000],
            original_patch[:14000],
            _compact_agent_trace(agent_result),
            validation_result[:2500],
        )
        try:
            response = llm.invoke([
                SystemMessage(content="Return only a unified diff or NO_PATCH."),
                HumanMessage(content=prompt),
            ])
            candidate = _extract_candidate_patch(response)
            source = "finalizer_model"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Finalizer model failed: %s: %s", type(exc).__name__, exc)
    if not candidate:
        return False, "FINALIZER_NO_CANDIDATE"
    project.round_succeeded = False
    ret = project._validate(
        ref,
        candidate,
        "complete candidate produced by %s after agent session ended" % source,
    )
    return bool(project.round_succeeded), ret


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

    project.checkpoint_dir = os.path.join(ported_dir, "validated-checkpoints")
    os.makedirs(project.checkpoint_dir, exist_ok=True)

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
    group_comparison = GroupComparison(ported_dir, selected, logger)

    # 组内成员 hunk 的原文，用来做「这一组是否被完整移植」的机械校验
    _, all_hunks = split_patch_indexed(patch, True)
    hunks_by_idx = {h["idx"]: h for h in all_hunks}
    # 覆盖校验是启发式：实测两次"失败"都是误报（标识符被改名），而每次重试要一整轮
    # session（≈20 分钟）。所以默认只记录、不重试；需要时可 --coverage-retries 1 打开。
    coverage_retries = int(getattr(data, "coverage_retries", 0) or 0)

    # [C] 跨组共享变更账本：后面的组能看到前面各组「实际产出了什么」。
    # 各组会话独立；累积模式中的源码已包含前面组的提交。
    ledger = []
    ledger_limit = int(getattr(data, "ledger_chars", 6000) or 6000)

    all_groups_complete = True
    selected_hunks = set()
    accepted_need_not_ported = set()
    any_strict_requires_patch = False
    reusable_checkpoints = restore_cumulative_checkpoints(
        project, data, selected, ported_dir
    )
    for gpos, group in selected:
        pp = group["text"]
        members = group["hunks"] or ["?"]
        group_tag = "merged hunk %d (original hunks %s, group %s)" % (
            gpos,
            members,
            group["gid"],
        )
        reusable = reusable_checkpoints.get(gpos)
        if reusable is not None:
            previous_status, previous_patch_path = reusable
            selected_hunks.update(int(i) for i in group.get("hunks", []))
            if group.get("strict_requires_patch", False):
                any_strict_requires_patch = True
            if previous_status.get("need_not_ported"):
                accepted_need_not_ported.update(
                    int(i) for i in group.get("hunks", [])
                )
            if not previous_status.get("complete"):
                all_groups_complete = False
            previous_patch = ""
            if os.path.isfile(previous_patch_path):
                with open(
                    previous_patch_path,
                    encoding="utf-8",
                    errors="surrogateescape",
                ) as fh:
                    previous_patch = fh.read()
            if previous_patch:
                ledger.extend([previous_patch])
            group_comparison.record(
                gpos, previous_patch, "恢复：复用已完成检查点"
            )
            logger.info(
                "%s resume checkpoint reused; no new model session",
                group_tag,
            )
            continue
        member_hunks = [hunks_by_idx[i] for i in group["hunks"] if i in hunks_by_idx]
        status_path = os.path.join(ported_dir, "merged-hunk-%02d.status.json" % gpos)
        project.round_succeeded = False
        project.context_mismatch_times = 0
        project.no_change_evidence = None
        reset_search_budget = getattr(project, "reset_search_budget", None)
        if reset_search_budget:
            reset_search_budget()
        finalizer_used = False
        n_before = len(project.succeeded_patches)
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
            return list(project.succeeded_patches[n_before:])

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
                        "symbol_check": getattr(project, "last_symbol_check", None),
                        "finalizer_used": finalizer_used,
                        "requires_final_semantic_review": True,
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
            applied_note = (
                "Earlier groups are already committed to your target branch. Inspect and reuse "
                "their existing definitions; do not add them again. The ledger can include "
                "superseded candidate revisions; the target tree is authoritative.\n"
                if cumulative else
                "Earlier groups are not applied to this clean checkout; their patches will "
                "be composed at the end. Keep signatures and renamed symbols consistent.\n"
            )
            text = (
                "\n[PREVIOUS CHANGES — shared change ledger]\n" + applied_note +
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
            direct_candidate = "\n".join(produced_patches()) or pp
            safety_report, safety_ok = validate_direct_apply_safety(
                project, data.target_release, direct_candidate
            )
            if safety_ok:
                directly_applied = True
                logger.debug(f"{group_tag} can be applied without any conflicts")
            else:
                del project.succeeded_patches[n_before:]
                project.round_succeeded = False
                project.repo.git.reset("--hard")
                ret = "DIRECT_APPLY_SAFETY_CHECK_FAILED: %s\n%s" % (
                    json.dumps(safety_report, ensure_ascii=False),
                    ret,
                )
                logger.warning(
                    "%s applied textually but failed target safety validation: %s",
                    group_tag,
                    safety_report.get("missing")
                    or safety_report.get("unprotected")
                    or safety_report.get("error"),
                )
        if not project.round_succeeded:
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
                "group_desc": describe_group(group) + ledger_note(),
            }
            dump_portgpt_summary(ported_dir, gpos, "initial", payload)
            agent_result = None
            try:
                agent_result = executor.invoke(payload, {"callbacks": [log_handler]})
            except Exception as e:  # noqa: BLE001  -- [P0] 会话异常不许弄死整个运行
                logger.error(
                    "%s 会话抛异常: %s: %s" % (group_tag, type(e).__name__, e)
                )
                project.round_succeeded = False
            if not project.round_succeeded:
                finalizer_used = True
                finalized, finalizer_ret = finalize_and_validate_candidate(
                    llm, project, data.target_release, group, pp, agent_result
                )
                if finalized:
                    logger.info(
                        "%s finalized and validated from %s",
                        group_tag,
                        finalizer_ret[:160],
                    )
            if not project.round_succeeded:
                logger.debug(
                    f"Failed to backport the {group_tag} \n----------------------------------\n{pp}\n----------------------------------\n"
                )
                logger.error(f"Reach max_iterations for {group_tag}")
                save_ported()
                # [P0] 一组失败不再终止整个运行：记录状态后继续下一组
                save_status(check_coverage(), 0)
                all_groups_complete = False
                ledger.extend(produced_patches())
                if cumulative:
                    commit_group(project, produced_patches(), gpos, group)
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
                "group_desc": describe_group(group) + ledger_note() + note,
            }
            dump_portgpt_summary(
                ported_dir, gpos, "retry-%02d" % attempts, payload
            )
            agent_result = None
            try:
                agent_result = executor.invoke(payload, {"callbacks": [log_handler]})
            except Exception as e:  # noqa: BLE001  -- [P0] 重试会话异常也不许弄死整个运行
                logger.error(
                    "%s 重试会话抛异常: %s: %s" % (group_tag, type(e).__name__, e)
                )
                project.round_succeeded = False
            if not project.round_succeeded:
                finalizer_used = True
                finalized, finalizer_ret = finalize_and_validate_candidate(
                    llm, project, data.target_release, group, pp, agent_result
                )
                if finalized:
                    logger.info(
                        "%s retry finalized and validated from %s",
                        group_tag,
                        finalizer_ret[:160],
                    )
            if not project.round_succeeded:
                # 重试没成，回滚到上一次的产出，别把已经拿到的部分丢掉
                project.succeeded_patches.extend(previous)
                cov = check_coverage()
                break
            cov = check_coverage()

        if not cov["complete"] and strict_requires_patch and not produced_patches() and not project.no_change_evidence:
            project.round_succeeded = False
        save_ported()
        save_status(cov, attempts)
        if cov.get("need_not_ported"):
            accepted_need_not_ported.update(int(i) for i in group.get("hunks", []))
        if not cov["complete"]:
            all_groups_complete = False
        # 本组产出记入账本，供后面的组参考
        ledger.extend(produced_patches())
        commit_ok = True
        if cumulative:
            commit_ok = commit_group(project, produced_patches(), gpos, group)
            if not commit_ok:
                project.round_succeeded = False
                all_groups_complete = False
                save_status(check_coverage(), attempts)
            elif os.environ.get("PG_COMPILE_PER_GROUP") == "1":
                compile_group(project, data, gpos,
                              os.path.join(ported_dir, "compile-per-group.log"))
            if commit_ok:
                save_cumulative_checkpoint(
                    data,
                    project,
                    gpos,
                    group,
                    os.path.join(ported_dir, "merged-hunk-%02d.patch" % gpos),
                    cov,
                )
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

    project.all_hunks_applied_succeeded = all_groups_complete
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
    pending_complete_path = complete_path + ".pending"
    quarantine_patch(complete_path)
    try:
        # 用 surrogateescape 原样写回字节：AOSP 里有少数文件不是严格 UTF-8
        blob = complete_patch if complete_patch.endswith("\n") else complete_patch + "\n"
        with open(pending_complete_path, "wb") as fh:
            fh.write(blob.encode("utf-8", "surrogateescape"))
        logger.info(
            "Pending complete patch (%d bytes) written to %s"
            % (len(blob.encode("utf-8", "surrogateescape")), pending_complete_path)
        )
    except OSError as e:  # noqa: BLE001
        logger.warning("写完整 patch 失败: %s" % e)

    # ---- Global validation over the FINAL composed patch --------------------
    # This runs once before final repair and again after the validated artifact
    # is rebuilt, so stale pre-repair statuses cannot report a false success.
    validation_base = project.repo.git.rev_parse(cum_base or data.target_release)
    review_llm = llm
    if getattr(data, 'coverage_model', None) and data.coverage_model != getattr(data, 'llm_model', None):
        review_data = copy(data)
        review_data.llm_model = data.coverage_model
        review_llm = build_llm(review_data)
    project.coverage_reviewer = CoverageReviewer(
        review_llm, project.dir, validation_base,
        max_iterations=getattr(data, 'coverage_review_iterations', 20),
        retries=getattr(data, 'coverage_review_retries', 1))

    def final_artifact_check(candidate):
        # Chain validation is deterministic and must run first. If it fails, the
        # repair Agent gets an actionable finding even when semantic review is
        # temporarily unavailable.
        chain_rc, chain_report = run_chain_check(
            project,
            candidate,
            validation_base,
            os.path.join(ported_dir, "global-api-chain.txt"),
        )
        if chain_rc != 0:
            return "API_CHAIN_VALIDATION_FAILED:\n%s" % chain_report

        # The same semantic check protects every full-patch repair before
        # compilation.
        with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', encoding='utf-8',
                                         errors='surrogateescape') as artifact:
            artifact.write(candidate if candidate.endswith('\n') else candidate + '\n')
            artifact.flush()
            complete, report = evaluate_global_artifact(
                project, candidate, artifact.name, ported_dir, hunks_by_idx,
                selected_hunks, accepted_need_not_ported, any_strict_requires_patch, cumulative)
        if report['coverage']['method'] == 'semantic_review_failed':
            raise RuntimeError('FINAL_REVIEW_UNAVAILABLE: ' + str(report['coverage'].get('error')))
        if complete:
            return ''
        return 'SEMANTIC_VALIDATION_FAILED: ' + json.dumps({
            'missing': report['coverage']['missing'],
            'members': [m for m in report['coverage']['members'] if not m['covered']],
            'review_error': report['coverage'].get('error'),
            'api_chain': report['api_chain_report'],
        }, ensure_ascii=False)

    initial_chain_rc, initial_chain_report = run_chain_check(
        project,
        complete_patch,
        validation_base,
        os.path.join(ported_dir, "global-api-chain.txt"),
    )
    if initial_chain_rc != 0:
        all_groups_complete = False
        global_result = {
            "complete": False,
            "patch_bytes": len(complete_patch.encode("utf-8", "surrogateescape")),
            "selected_hunks": sorted(selected_hunks),
            "accepted_need_not_ported_hunks": sorted(accepted_need_not_ported),
            "coverage_hunks": sorted(selected_hunks),
            "coverage": {
                "method": "api_chain_failed",
                "complete": False,
                "missing": sorted(selected_hunks),
                "members": [],
                "error": None,
            },
            "heuristic_coverage": None,
            "api_chain_rc": initial_chain_rc,
            "api_chain_report": initial_chain_report,
        }
        logger.error(
            "Global API chain validation failed before semantic review; "
            "entering targeted final repair."
        )
    else:
        all_groups_complete, global_result = evaluate_global_artifact(
            project, complete_patch, pending_complete_path, ported_dir, hunks_by_idx,
            selected_hunks, accepted_need_not_ported, any_strict_requires_patch,
            cumulative,
        )

    if (global_result['coverage']['method'] == 'semantic_review_failed'
            and global_result.get('api_chain_rc') == 0):
        logger.error('Backport INCOMPLETE: final review could not finish. Candidate retained; '
                     'review/API errors are not evidence that code must be rewritten.')
        return

    if global_result['coverage']['method'] == 'semantic_review_failed':
        logger.warning(
            "Final semantic review was unavailable, but API-chain check found an "
            "actionable defect. Entering targeted final repair."
        )

    if not complete_patch.strip():
        logger.error('Backport INCOMPLETE: no nonempty candidate to validate or repair.')
        return
    # Full-patch mode is distinct from semantic acceptance. The callback rejects
    # missing obligations before any build, including candidates from final repair.
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
    if project.poc_succeeded:
        project.persist_validated_patch(complete_patch, validation_base)
        write_patch_atomic(pending_complete_path, complete_patch)
        if not promote_pending_patch(pending_complete_path, complete_path):
            project.poc_succeeded = False
            project.round_succeeded = False
            logger.error("Validated candidate could not be promoted to complete.patch")
            return
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
    viewcode, locate_symbol, validate, _, _, resolve_mapping = project.get_tools()
    tools = [viewcode, locate_symbol, validate, resolve_mapping]
    agent = create_tool_calling_agent(llm, tools, prompt)
    agent_executor = AgentExecutor(
        agent=agent, tools=tools, verbose=True,
        max_iterations=getattr(data, 'final_repair_iterations', 20)
    )
    try:
        agent_executor.invoke(
            {
                "project_url": data.project_url,
                "new_patch_parent": data.new_patch_parent,
                "target_release": validation_base,
                "new_patch": patch,
                "complete_patch": complete_patch,
                "compile_ret": validate_ret,
            },
            {"callbacks": [log_handler]},
        )
    except Exception as e:  # noqa: BLE001
        if project.poc_succeeded:
            logger.warning(
                "Final repair session ended with %s after a validated patch was saved; "
                "using the validated checkpoint." % type(e).__name__
            )
        else:
            logger.error("Final repair session failed: %s: %s" % (type(e).__name__, e))
            project.round_succeeded = False
    if project.poc_succeeded:
        logger.info(
            f"Successfully backport the patch to the target release {data.target_release}"
        )
        # Final validation accepts full patches against validation_base only.
        # Rebuild on that same base; never reinterpret a failed full patch as
        # a fragment to stack on the cumulative branch.
        latest_checkpoint = os.path.join(
            project.checkpoint_dir, "latest-validated.patch"
        )
        candidates = final_candidate_sources(project, latest_checkpoint)

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
            quarantine_patch(complete_path)
            logger.error(
                "Validated checkpoint could not be rebuilt into a complete patch; "
                "removed the stale complete.patch so it cannot be reported as OK."
            )
            return

        try:
            write_patch_atomic(pending_complete_path, fixed)
            logger.info(
                "Validated checkpoint rebuilt as pending complete patch (%d bytes)"
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
                project, fixed, pending_complete_path, ported_dir, hunks_by_idx,
                selected_hunks, accepted_need_not_ported,
                any_strict_requires_patch, cumulative,
            )
            project.repo.git.reset("--hard")
            project.repo.git.checkout(data.target_release)
            if not global_complete:
                project.poc_succeeded = False
                project.round_succeeded = False
                quarantine_patch(complete_path)
                logger.error(
                    "Rebuilt patch failed final global validation; stale complete.patch removed"
                )
                return
        if not promote_pending_patch(pending_complete_path, complete_path):
            project.poc_succeeded = False
            project.round_succeeded = False
            logger.error("Rebuilt patch passed checks but could not be promoted")
            return
        write_patch_atomic(
            os.path.join(project.checkpoint_dir, "final-complete.patch"), fixed
        )
        project.succeeded_patches.clear()
        project.succeeded_patches.append(fixed)
        for patch in project.succeeded_patches:
            logger.info(patch)
    else:
        quarantine_patch(complete_path)
        logger.error(
            f"Failed backport the patch to the target release {data.target_release}"
        )
