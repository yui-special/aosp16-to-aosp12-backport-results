import os
import re
import json
import hashlib
import subprocess
import tempfile
import time
from types import SimpleNamespace
from typing import List, Tuple

import Levenshtein
from git import Repo
from git.exc import GitCommandError
from langchain_core.tools import tool

import tools.utils as utils
from tools.logger import logger
from tools.source_structure import SymbolIndex


class Project:
    def __init__(self, data: SimpleNamespace):
        self.project_url = data.project_url
        self.dir = data.project_dir
        self.repo = Repo(data.project_dir)

        if not data.error_message:
            self.err_msg = "no err_msg"
        else:
            self.err_msg = data.error_message

        self.new_patch_parent = data.new_patch_parent
        self.target_release = data.target_release
        self.succeeded_patches = []
        self.context_mismatch_times = 0
        self.round_succeeded = False
        self.all_hunks_applied_succeeded = False
        self.compile_succeeded = False
        self.testcase_succeeded = False
        self.poc_succeeded = False
        self.checkpoint_dir = None
        self.validated_patch_checkpoints = []
        self.full_patch_base = None
        self.validated_complete_patch = None
        self.symbol_map = {}
        self.source_index = SymbolIndex(self.dir)
        self.now_hunk = ""
        self.now_hunk_num = 0
        self.hunk_log_info = {}
        self.add_percent = 0
        self.last_context = []
        self.no_change_evidence = None
        self.final_artifact_check = None
        # [P0] 文件"搬移推断"的递归深度（只允许一层，避免 5^k 重试爆炸）
        self._move_depth = 0

    def persist_validated_patch(self, patch: str, ref: str):
        """Persist a successfully validated candidate immediately.

        The candidate is a full patch against full_patch_base. It is kept as
        a checkpoint so a later gateway disconnect cannot erase work already
        accepted by `validate`.
        """
        if not self.checkpoint_dir or not patch or "need not ported" in patch:
            return None
        if "diff --git" not in patch and "--- a/" not in patch:
            return None

        os.makedirs(self.checkpoint_dir, exist_ok=True)
        ref = self.full_patch_base or ref
        canonical = patch
        if self.full_patch_base:
            try:
                full = self.repo.git.diff(self.full_patch_base)
                if full and full.strip():
                    canonical = full
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not derive canonical full patch: %s" % e)
        self.validated_complete_patch = canonical
        digest = hashlib.sha256(
            canonical.encode("utf-8", "surrogateescape")
        ).hexdigest()[:16]
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        checkpoint = os.path.join(
            self.checkpoint_dir, "validated-%s-%s.patch" % (stamp, digest)
        )
        blob = canonical if canonical.endswith("\n") else canonical + "\n"
        for path in (checkpoint, os.path.join(self.checkpoint_dir, "latest-validated.patch")):
            tmp = path + ".tmp.%d" % os.getpid()
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(blob)
            os.replace(tmp, path)
        self.validated_patch_checkpoints.append(checkpoint)
        try:
            with open(os.path.join(self.checkpoint_dir, "latest-validated.json"),
                      "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"ref": ref, "patch": checkpoint, "sha256": digest,
                           "canonical_full_patch": canonical != patch},
                          fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
        logger.info("Validated patch checkpoint saved: %s" % checkpoint)
        return checkpoint

    def _checkout(self, ref: str) -> None:
        self.repo.git.reset("--hard")
        try:
            self.repo.git.checkout(ref)
        except Exception:
            # 模型偶尔会把 ref 写错（实测：把 target 的前 32 位和 parent 的后 8 位拼在一起）
            fallback = getattr(self, "target_release", None)
            if fallback and fallback != ref:
                logger.warning(f"checkout({ref}) failed; falling back to {fallback}")
                self.repo.git.checkout(fallback)
            else:
                raise

    def _get_patch(self, ref: str) -> str:
        try:
            return self.repo.git.show(f"{ref}^..{ref}")
        except:
            return "Error commit id, please check if the commit id is correct."

    def _prepare(self, ref: str) -> None:
        """Build a Tree-sitter index from this revision without changing the checkout."""
        self.symbol_map[ref] = self.source_index.prepare(ref)

    def _viewcode(self, ref: str, path: str, startline: int, endline: int) -> str:
        """
        View a file from a specific ref of the target repository. Lines between startline and endline are shown.

        Args:
            ref (str): The specific ref of the target repository.
            path (str): The path of the file to view.
            startline (int): The starting line number to display.
            endline (int): The ending line number to display.

        Returns:
            str: The content of the file between the specified startline and endline.
                 If the file doesn't exist in the commit, a message indicating that is returned.
        """
        try:
            file = self.repo.tree(ref) / path
        except:
            return "This file doesn't exist in this commit."
        content = file.data_stream.read().decode("utf-8", errors="ignore")
        lines = content.split("\n")
        ret = []
        if not lines:
            return "This file is empty.\n"
        if startline > endline:
            startline, endline = endline, startline
        startline = max(1, startline)
        if startline > len(lines):
            ret.append(
                f"This file only has {len(lines)} lines. Showing full file.\n"
            )
            startline = 1
            endline = len(lines)
        elif endline > len(lines):
            endline = len(lines)
            ret.append(
                f"This file only has {len(lines)} lines. Here are lines {startline} through {endline}.\n"
            )
        else:
            ret.append(f"Here are lines {startline} through {endline}.\n")
        for i in range(startline - 1, endline):
            ret.append(lines[i])
        return (
            "\n".join(ret)
            + "\nBased on the previous information, think carefully do you see the target code? You may want to keep checking if you don't.\n"
        )

    def _locate_symbol(self, ref: str, symbol: str) -> List[Tuple[str, int]] | None:
        """
        Locate a symbol in a specific ref of the target repository.

        Args:
            ref (str): The reference of the target repository.
            symbol (str): The symbol to locate.

        Returns:
            List[Tuple[str, int]] | None: File path and code lines.
        """
        return self.source_index.locate(ref, symbol)

    def _locate_similar_symbol(
        self, ref: str, symbol: str
    ) -> List[Tuple[str, int]] | None:
        """
        Locate the most similar symbol with llm need in a specific ref of the target repository.

        Args:
            ref (str): The reference of the target repository.
            symbol (str): The symbol to locates.

        Returns:
            List[Tuple[str, int]] : File path and code lines for the most similar symbol.
        """
        self._prepare(ref)
        symbols = self.symbol_map[ref]
        most_similar = None
        smallest_distance = float("inf")

        for symbol_i in symbols.keys():
            # 计算 Levenshtein 距离
            distance = Levenshtein.distance(symbol, symbol_i)
            if distance < smallest_distance:
                smallest_distance = distance
                most_similar = symbol_i

        return symbols.get(most_similar), most_similar

    def _git_history(self) -> str:
        """
        XXX: TBD

        Args:
            XXX

        Returns:
            XXX(str):
        """
        if self.now_hunk != "completed":
            merge_base = self.repo.merge_base(
                self.target_release, self.new_patch_parent
            )
            start_commit = merge_base[0].hexsha if merge_base else None
            hunk = self.now_hunk
            filepath = re.findall(r"--- a/(.*)", hunk)[0]
            chunks = re.findall(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@(.*)", hunk)[0]
            start_line = chunks[0]
            end_line = int(chunks[0]) + int(chunks[1]) - 1
            try:
                log_message = self.repo.git.log(
                    "--oneline",
                    f"-L {start_line},{end_line}:{filepath}",
                    f"{start_commit}..{self.new_patch_parent}",
                )
            except GitCommandError as e:
                # [histguard] 原版在这里没有任何容错：只要 (a) 两个版本没有共同祖先
                # （merge_base 为空 → range 变成 None..sha），或 (b) hunk 里的路径在目标
                # 版本历史里不存在，git log -L 就会 fatal，异常一路抛出去终止整个运行。
                # 这里仅把失败转成一条说明返回给模型（不改任何判定逻辑），
                # 让跨仓库/跨版本输入也能继续跑。
                return (
                    f"No history available for {filepath!r} "
                    f"(range {start_commit}..{self.new_patch_parent}): "
                    f"{str(e.stderr).strip()[:200]}\n"
                    "The file may not exist in the target version, or the two versions share "
                    "no history. Use locate_symbol / viewcode to inspect the target code instead."
                )
            # save each hunk related refs
            if self.now_hunk_num not in self.hunk_log_info and log_message:
                last_context = list(utils.split_patch(log_message, False))[-1]
                (
                    _,
                    context_line_num,
                    self.last_context,
                    add_line_num,
                ) = utils.extract_context(last_context.split("\n")[3:])
                self.add_percent = add_line_num / (add_line_num + context_line_num)

                self.hunk_log_info[self.now_hunk_num] = []
                patch_list = log_message.split("\n")
                for idx, line in enumerate(patch_list):
                    if line.startswith("diff --git"):
                        sha_num = patch_list[idx - 2].split(" ")[0]
                        self.hunk_log_info[self.now_hunk_num].append(sha_num)

            ret = log_message[len(log_message) - 5001 : -1]
            ret += "\nYou need to do the following analysis based on the information in the last commit:\n"
            ret += "Analyze the code logic of the context of the patch to be ported in this commit step by step.\n"
            ret += "If code logic already existed before this commit, the patch context can be assumed to remain in a similar location. Use `locate` and `viewcode` to check your results.\n"
            ret += "If code logic were added in this commit, then you need to `git_show` for further details.\n"
            return ret

        else:
            # XXX TBD
            # JUST return each hunk related refs
            pass

    def _git_show(self) -> str:
        """
        Show commit message for a specific ref when LLM need.

        Args:
            ref (str): The reference of the target repository.

        Returns:
            message(str): The commit message of ref
        """
        try:
            # XXX maybe too much context will confuse LLM, how could we refine it.
            ref_line = self.hunk_log_info[self.now_hunk_num][-1]
            ref = ref_line.split(" ")[0].strip()
            log = self.repo.git.show(f"{ref}")
            pps = utils.split_patch(log, False)
            dist = float("inf")
            last_context_len = len(self.last_context)
            best_context = []
            file_path = ""
            file_no = 0

            for idx, pp in enumerate(pps):
                try:
                    file_path_i = re.findall(r"--- a/(.*)", pp)[0]
                    chunks = re.findall(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@(.*)", pp)[0]
                    contexts, _, _, _ = utils.extract_context(pp.split("\n")[3:])
                    if (int(chunks[1]) - int(chunks[3])) < last_context_len:
                        continue
                    lineno, dist_i = utils.find_most_similar_block(
                        self.last_context, contexts, last_context_len, False
                    )
                    if dist_i < dist:
                        best_context = contexts[
                            lineno - 1 : lineno - 1 + last_context_len
                        ]
                        dist = dist_i
                        file_path = file_path_i
                        file_no = int(chunks[0]) + lineno - 1
                except:
                    continue

            ret = ""
            stat = self.repo.git.show("--stat", f"{ref}")
            ret += stat[0 : min(len(stat), 3000)]
            ret += "\n"
            if self.add_percent < 0.6:
                ret += f"[IMPORTANT] The relevant code shown by `git_history` is not fully `+` lines.\n"
                ret += f"[IMPORTANT] This means that the code in question was not added or migrated in this commit.\n"
                ret += f"[IMPORTANT] Please think step by step and check the abstract below carefully. If error exists in abstract, please ignore the info below.\n"
            elif best_context:
                ret += f"Because the commit's code change maybe too long, so I generate the abstract of the code change to show you how code changed in this commit.\n"
                ret += f"Commit shows that the patch code in old version maybe in the file {file_path} around line number {file_no} to {file_no + last_context_len}. The code is below\n"
                code_snippets = "\n".join(best_context)
                ret += f"{code_snippets}"
                ret += f"\nYou can call `viewcode` and `locate_symbol` to find the relevant code based on this information step by step."
            else:
                ret += f"This commit shows that there is a high probability that this code is new, so the corresponding code segment cannot be found in the old version.\n"
                ret += f"You can call `viewcode` and `locate_symbol` to further check the results step by step. For newly introduced code, we consider that this hunk `need not ported`.\n"
            return ret
        except:
            return "Something error, maybe you don't use git_history before or git_history is empty."

    def _apply_error_handling(self, ref: str, revised_patch: str) -> Tuple[str, str]:
        """
        Generate feedback to llm when an error patch is applied.
        When a file is not found, it is looked for in the five most similar files.

        Args:
            ref (str): The reference of the target repository.
            revised_patch (str): The patch to be applied.

        Returns:
            Tuple[str, str]: Bug patch similar code block information and difference between patch context and original code context.

        """
        path = re.findall(r"--- a/(.*)", revised_patch)[0]
        revised_patch_line = revised_patch.split("\n")[3:]
        contexts, num_context, _, _ = utils.extract_context(revised_patch_line)
        lineno = -1
        lines = []
        min_distance = float("inf")

        try:
            file = self.repo.tree(ref) / path
            content = file.data_stream.read().decode("utf-8", errors="ignore")
            lines = content.split("\n")
            lineno, dist = utils.find_most_similar_block(
                contexts, lines, num_context, False
            )
        except:
            similar_files = utils.find_most_similar_files(path.split("/")[-1], self.dir)
            for similar_file in similar_files:
                file = self.repo.tree(ref) / similar_file
                content = file.data_stream.read().decode("utf-8", errors="ignore")
                similar_lines = content.split("\n")
                current_line, current_dist = utils.find_most_similar_block(
                    "\n".join(contexts), similar_lines, num_context, False
                )

                if current_dist < min_distance:
                    min_distance = current_dist
                    lineno = current_line
                    path = similar_file
                    lines = similar_lines

        startline = max(lineno - 1, 0)
        endline = min(lineno + num_context, len(lines))
        block = "Here are lines {} through {} of file {} for commit {}.\n".format(
            startline, endline, path, ref
        )
        block += "```code snippet\n"
        for i in range(startline, endline):
            block = block + lines[i] + "\n"
        block += "```\n"

        differ = "```context diff\n"
        contexts = contexts[: min(len(lines), len(contexts))]
        j = 0
        for i, context in enumerate(revised_patch_line):
            if context.startswith(" ") or context.startswith("-"):
                idx = lineno - 1 + j
                if idx < 0 or idx >= len(lines):
                    # [P0] 补丁上下文比目标文件还长：不越界，如实说明"目标里没有这一行"
                    differ += f"On the line {i + 4} of your patch.\n"
                    differ += f"          Your patch:{context[1:]}\n"
                    differ += "          Original source code:(no such line in the target file)\n"
                    j += 1
                    continue
                if context[1:] != lines[idx]:
                    differ += f"On the line {i + 4} of your patch.\n"
                    differ += f"          Your patch:{context[1:]}\n"
                    differ += f"Original source code:{lines[idx]}\n"
                j += 1

        if differ == "```context diff\n":
            differ = "Here it shows that there is no difference between your context and the original code, the reason for the failure is that you didn't keep at least three lines of source code at the beginning and end of the patch, please follow this to fix it.\n"
        else:
            differ += "```\nPlease eliminate these diffs step by step. Be sure to eliminate these diffs the next time you generate a patch!\n"
        return block, differ

    def _apply_file_move_handling(self, ref: str, old_patch: str) -> str:
        """
        If a patch cannot apply for "No such file", try to find the symbol and apply the patch to the correct file.

        Args:
            ref (str): The reference string.
            old_patch (str): The patch that raises "No such file" when apply.

        Returns:
            str: If the file is found, return the current file path. Else, return all possible file paths.
        """
        ret = ""
        file_paths = []
        missing_file_path = re.findall(r"--- a/(.*)", old_patch)[0]

        # [P0] 只允许一层"重命名推断"；再往里递归就是 5^k 级别重试爆炸
        # （实测：一个组刷了 69,966 次 "No such file"、61 MB 日志、最后 RecursionError）
        if getattr(self, "_move_depth", 0) >= 1:
            logger.debug(f"Skip recursive file-move handling for {missing_file_path}")
            return (
                "TARGET_FILE_MISSING: `%s` does not exist at %s and no relocation was "
                "confirmed. Do NOT port this hunk literally. If an equivalent function/class "
                "exists in the target, rewrite the patch against that location; otherwise "
                "report `need not ported`.\n" % (missing_file_path, ref)
            )

        # locate file by git diff
        diff_args = [
            "--diff-filter=R",
            "--name-status",
            "--follow",
            self.target_release,
            self.new_patch_parent,
            "--",
            missing_file_path,
        ]
        file_diff = self.repo.git.diff(diff_args)
        if file_diff:
            file_path = file_diff.split("\t")[1]
            logger.debug(
                f"We have found the patch's file path is {file_path} at target release by git diff."
            )
            file_paths.append(file_path)

        # locate target file by symbol or utils.find_most_similar_files
        if not file_paths:
            try:
                # XXX: find symbol: the word before the first '{' or '('
                # @@ -135,7 +135,6 @@ struct ksmbd_transport_ops {
                # @@ -416,13 +416,7 @@ static void stop_sessions(void)
                at_line = old_patch.split("\n")[2]
                symbol_name = re.findall(r"\b\w+(?=\s*[{\(])", at_line)[0]
                symbol_locations = self._locate_symbol(ref, symbol_name)
                if not symbol_locations:
                    logger.debug(
                        f"No {missing_file_path} and no {symbol_name} in the repo."
                    )
                    file_paths = utils.find_most_similar_files(
                        missing_file_path.split("/")[-1], self.dir
                    )
                else:
                    logger.debug(f"Find {symbol_name} in {symbol_locations}.")
                    file_paths = [item[0] for item in symbol_locations]
            except:
                logger.debug("Can not find a symbol in given patch.")
                file_paths = utils.find_most_similar_files(
                    missing_file_path.split("/")[-1], self.dir
                )

        # try to apply patch to the target files
        self._move_depth = getattr(self, "_move_depth", 0) + 1
        try:
            for file_path in file_paths:
                new_patch = old_patch.replace(missing_file_path, file_path)
                logger.debug(f"Try to apply patch to {file_path}.")
                apply_ret = self._apply_hunk(ref, new_patch, False)
                if "successfully" in apply_ret:
                    logger.debug(f"{missing_file_path} has been moved to {file_path}.")
                    return f"{missing_file_path} has been moved to {file_path}. Please use --- a/{file_path} in your patch.\n"
                else:
                    ret += apply_ret
        finally:
            self._move_depth -= 1

        # patch can not apply directly
        logger.debug(f"Patch can not be applied to {file_paths}.")
        return (
            "TARGET_FILE_MISSING: `%s` does not exist at %s. Candidate paths tried: %s\n%s"
            % (missing_file_path, ref, file_paths, ret)
        )

    def _apply_hunk(self, ref: str, patch: str, revise_context: bool = False) -> str:
        """
        Apply a hunk to a specific ref of the target repository.

        Args:
            ref (str): The reference of the target repository.
            patch (str): The patch to be applied.

        Returns:
            str: A string indicating the result of the patch application.

        Raises:
            Exception: If the patch fails to apply.

        """
        ret = ""
        self._checkout(ref)
        self.repo.git.reset("--hard")
        if revise_context:
            logger.debug("original patch:\n" + patch)
        revised_patch, fixed = utils.revise_patch(patch, self.dir, revise_context)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write(revised_patch)
        logger.debug("revised patch:\n" + revised_patch)
        logger.debug(f"Applying patch {f.name}")
        try:
            self.repo.git.apply([f.name], v=True)
            ret += "Patch applied successfully\n"
            self.succeeded_patches.append(revised_patch)
            self.round_succeeded = True
        except Exception as e:
            if "No such file" in e.stderr:
                logger.debug(f"File not found")
                find_ret = self._apply_file_move_handling(ref, revised_patch)
                ret += find_ret
            elif "corrupt patch" in e.stderr:
                ret = "Unexpected corrupt patch, Please carefully check your answer, especially in your call tools arguments.\n"
                # raise Exception("Unexpected corrupt patch")
            else:
                logger.debug(f"Context mismatch")
                ret += "This patch does not apply because of CONTEXT MISMATCH. Context are patch lines that already exist in the file, that is, lines starting with ` ` and `-`. You should modify the error patch according to the context of older version.\n"
                block, differ = self._apply_error_handling(ref, revised_patch)
                ret += block
                ret += "Besides, here is detailed info about how the context differs between the patch and the old version.\n"
                ret += differ

        # 累积模式：成功打上的结果要留在工作树里，供 commit_group 直接提交；
        # 失败时 git apply 是原子的（不会留下半成品），照旧还原。
        if not (getattr(self, "keep_tree_after_apply", False) and self.round_succeeded):
            self.repo.git.reset("--hard")
        return ret

    def _compile_patch(
        self, ref: str, complete_patch: str, revise_context: bool = False
    ) -> str:
        """
        If all hunks could be applied successfully, compiles the patched source code after applying the joined patch.

        Args:
            ref (str): The reference to checkout before applying the patch.
            complete_patch (str): The complete patch to be applied.

        Returns:
            str: A message indicating the result of the compilation process.

        Raises:
            subprocess.TimeoutExpired: If the compilation process times out.

        """
        # Full candidates always replace the previous attempt on the original
        # base. Do not use _checkout's fallback or rewrite source context.
        base = self.full_patch_base or ref
        self.repo.git.reset("--hard")
        try:
            self.repo.git.checkout(base)
            if self.repo.git.rev_parse("HEAD") != self.repo.git.rev_parse(base):
                raise ValueError("checkout did not select the validation base")
        except Exception as exc:
            return "VALIDATION_BASE_ERROR: cannot check out %s: %s\n" % (base, exc)
        ret = ""
        blob = complete_patch if complete_patch.endswith("\n") else complete_patch + "\n"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         errors="surrogateescape", delete=False) as handle:
            handle.write(blob)
            patch_path = handle.name
        try:
            # --recount fixes hunk line counts only; --index includes new files
            # in the canonical checkpoint and makes reset undo failed attempts.
            self.repo.git.apply("--check", "--index", "--recount", patch_path)
            self.repo.git.apply("--index", "--recount", patch_path)
        except Exception as exc:
            self.repo.git.reset("--hard")
            return (
                "APPLY_FAILED on original base %s. Submit the complete revised patch "
                "against this base, not a fragment against previous changes. "
                "Context is not rewritten automatically. Inspect this ref with viewcode.\n%s\n"
                % (base, getattr(exc, "stderr", str(exc)))
            )
        finally:
            os.unlink(patch_path)

        if self.final_artifact_check is not None:
            failure = self.final_artifact_check(complete_patch)
            if failure:
                return failure + '\nSubmit a complete revised patch addressing these obligations.\n'

        # compile the patch
        logger.debug("Start compile the patched source code")
        if not os.path.exists(os.path.join(self.dir, "build.sh")):
            logger.debug("No build.sh file found.")
            ret += "The patched source code could be COMPILED successfully! I really thank you for your great efforts.\n"
            self.compile_succeeded = True
            return ret

        # build_process = subprocess.Popen(
        #     ["/bin/bash", "build.sh"],
        #     stdin=subprocess.DEVNULL,
        #     stdout=subprocess.PIPE,
        #     stderr=subprocess.PIPE,
        #     cwd=self.dir,
        #     text=True,
        # )
        docker_command = [
            "docker",
            "run",
            "-v",
            f"{self.dir}:{self.dir}",
            "--rm",
            "build-kernel-ubuntu-16.04",
            "/bin/bash",
            "-c",
            f"cd {self.dir}; bash build.sh",
        ]
        # [asan-runner] 编译改为宿主机执行（原实现依赖缺失的 docker 镜像）
        build_process = subprocess.Popen(
            ["/bin/bash", "build.sh"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.dir,
            text=True,
        )
        try:
            # stdout 和 stderr 都要：实测出现过 build.sh 失败但 stdout 为空、
            # 错误信息全在 stderr 的情况，模型收到空错误只能瞎猜。
            compile_stdout, compile_stderr = build_process.communicate(timeout=60 * 60)
            compile_result = compile_stdout
        except subprocess.TimeoutExpired:
            build_process.kill()
            ret += f"The compilation process of the patched source code is timeout. "
            self.repo.git.reset("--hard")
            logger.warning(
                "Timeout in project compilation. Please check patch manually!"
            )
            for patch in self.succeeded_patches:
                logger.info(patch)
            exit(0)
            return ret

        if build_process.returncode != 0:
            logger.info(f"Compilation                       FAILED")
            error_lines = "\n".join(
                [
                    line
                    for line in compile_result.splitlines()
                    if "error:" in line.lower()
                ]
            )
            # [asan-runner] 提取不到 error: 时回退到输出尾部，避免空错误信息
            if not error_lines.strip():
                error_lines = "\n".join(compile_result.splitlines()[-15:])
            if not error_lines.strip():
                error_lines = "[build.sh stderr]\n" + "\n".join(
                    (compile_stderr or "").splitlines()[-15:]
                )
            logger.debug(error_lines)
            ret += "The source code could not be COMPILED successfully after applying the patch. "
            ret += "Next I'll give you the error message during compiling, and you should modify the error patch. "
            ret += f"Here is the error message:\n{error_lines}\n"
            ret += "Please revise the patch with above error message. "
            ret += "Or use tools `locate_symbol` and `viewcode` to re-check patch-related code snippet. "
            ret += "Please DO NOT send the same patch to me, repeated patches will harm the lives of others.\n"
            self.repo.git.reset("--hard")
        else:
            logger.info(f"Compilation                       PASS")
            ret += "The patched source code could be COMPILED successfully! I really thank you for your great efforts.\n"
            self.compile_succeeded = True
        # self.repo.git.reset("--hard")
        return ret

    def _run_testcase(self) -> str:
        """
        Runs the testcase after compiling a patch.

        Returns:
            str: A message indicating the result of the testcase process.
        """
        ret = ""
        logger.debug("Run testcase after compile")

        if not os.path.exists(os.path.join(self.dir, "test.sh")):
            logger.debug("No test.sh file found, considered as test passed.")
            self.testcase_succeeded = True
            ret += "The patched source code could pass TESTCASE! I really thank you for your great efforts.\n"
            return ret
        testcase_process = subprocess.Popen(
            ["/bin/bash", "test.sh"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.dir,
            text=True,
        )

        try:
            # communicate() 返回 (stdout, stderr)。原版写成 `_, x = ...` 等于只拿 stderr，
            # 而 javac/脚本的输出几乎都在 stdout，模型因此只能收到空错误信息。
            testcase_stdout, testcase_stderr = testcase_process.communicate(timeout=60 * 30)
            testcase_result = testcase_stdout or ""
            if (testcase_stderr or "").strip():
                testcase_result += "\n[stderr]\n" + testcase_stderr
        except subprocess.TimeoutExpired:
            testcase_process.kill()
            ret += "The TESTCASE process of the patched source code is timeout. "
            return ret

        if testcase_process.returncode != 0:
            logger.info(f"Testsuite                         FAILED")
            logger.debug(f"{testcase_result}")
            ret = "The patched program could not pass the testcase. "
            ret += "Next I'll give you the error message during running the testcase, and you should modify the previous error patch according to this section. "
            ret += f"Here is the error message:\n{testcase_result}\n"
            ret += "Please revise the patch with above error message. "
            ret += "Or use tools `locate_symbol` and `viewcode` to re-check patch-related code snippet. "
            ret += "Please DO NOT send the same patch to me, repeated patches will harm the lives of others.\n"
            self.compile_succeeded = False
        else:
            logger.info(f"Testsuite                         PASS")
            ret += "The patched source code could pass TESTCASE! I really thank you for your great efforts.\n"
            self.testcase_succeeded = True
        return ret

    def _run_poc(self, complete_patch) -> str:
        """
        Runs the Proof of Concept (PoC) after running the testcase.

        Returns:
            str: A message indicating the result of the PoC process.
        """
        ret = ""
        logger.debug("Run PoC after compile and run testcase")

        if not os.path.exists(os.path.join(self.dir, "poc.sh")):
            logger.debug("No poc.sh file found, considered as PoC passed.")
            self.poc_succeeded = True
            self.succeeded_patches.clear()
            self.succeeded_patches.append(complete_patch)
            ret += "Existing PoC could NOT TRIGGER the bug, which means your patch successfully fix the bug! I really thank you for your great efforts.\n"
            return ret
        poc_process = subprocess.Popen(
            ["/bin/bash", "poc.sh"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.dir,
            text=True,
        )

        try:
            # 同上：原来只读 stderr，而 PoC 的触发输出通常走 stdout，
            # 结果 `err_msg in poc_result` 永远不成立 -> PoC 被"默认判过"。
            poc_stdout, poc_stderr = poc_process.communicate(timeout=60 * 10)
            poc_result = poc_stdout or ""
            if (poc_stderr or "").strip():
                poc_result += "\n[stderr]\n" + poc_stderr
        except subprocess.TimeoutExpired:
            poc_process.kill()
            ret += "The TESTCASE process of the patched source code is timeout. "
            return ret

        if self.err_msg in poc_result:
            logger.info(f"PoC test                          FAILED")
            logger.debug(f"returncode = {poc_process.returncode}")
            logger.debug(f"stderr: {poc_result}")
            ret += "Existing PoC could still trigger the bug, which means your patch fail to fix the bug. "
            ret += "Next I'll give you the error message during running the PoC, and you should modify the previous error patch according to this section. "
            ret += f"Here is the error message:\n{poc_result}\n"
            ret += "Please revise the patch with above error message. "
            ret += "Or use tools `locate_symbol` and `viewcode` to re-check patch-related code snippet. "
            ret += "Please DO NOT send the same patch to me, repeated patches will harm the lives of others.\n"
            self.compile_succeeded = False
            self.testcase_succeeded = False
        else:
            logger.info(f"PoC test                          PASS")
            ret += "Existing PoC could NOT TRIGGER the bug, which means your patch successfully fix the bug! I really thank you for your great efforts.\n"
            self.succeeded_patches.clear()
            self.succeeded_patches.append(complete_patch)
            self.poc_succeeded = True
        return ret

    def _validate(self, ref: str, patch: str, evidence: str = '') -> str:
        """
        Validates a patch by using the `_compile_patch`, `_run_testcase`, and `_run_poc` methods.

        Args:
            ref (str): The reference string.
            patch (str): The patch string.

        Returns:
            str: The validation result.

        """
        if self.all_hunks_applied_succeeded:
            # A new candidate must never inherit a previous candidate's success.
            self.compile_succeeded = False
            self.testcase_succeeded = False
            self.poc_succeeded = False
            ret = self._compile_patch(self.full_patch_base or ref, patch)
            if self.compile_succeeded and not self.testcase_succeeded:
                ret += self._run_testcase()
            if (
                self.compile_succeeded
                and self.testcase_succeeded
                and not self.poc_succeeded
            ):
                ret += self._run_poc(patch)
            return ret
        else:
            if patch.strip() == "need not ported":
                if not evidence.strip():
                    self.round_succeeded = False
                    return ('NO_CHANGE_EVIDENCE_REQUIRED: explain each member using concrete target '
                            'files, symbols, and behavior. Distinguish already satisfied by earlier '
                            'groups from genuinely inapplicable. A missing upstream name is insufficient.\n')
                self.no_change_evidence = {'reason': evidence, 'ref': self.repo.git.rev_parse(ref),
                                           'status': 'pending_final_semantic_review'}
                self.round_succeeded = True
                return ('NO_CHANGE_RECORDED: explanation saved for independent final semantic review. '
                        'No patch was applied and coverage has not been accepted. Finish this group '
                        'with the per-hunk explanation; compilation and tests have not run.\n')

            ret = self._apply_hunk(
                ref, patch, True if self.context_mismatch_times >= 2 else False
            )
            if "CONTEXT MISMATCH" in ret:
                self.context_mismatch_times += 1
            return ret

    def get_tools(self):
        return (
            creat_viewcode_tool(self),
            creat_locate_symbol_tool(self),
            create_validate_tool(self),
            create_git_history_tool(self),
            create_git_show_tool(self),
        )


def creat_locate_symbol_tool(project: Project):
    @tool
    def locate_symbol(ref: str, symbol: str) -> str:
        """
        Locate a symbol in a specific ref of the target repository.
        """
        res = project._locate_symbol(ref, symbol)
        if res is not None:
            return "\n".join([f"{file}:{line}" for file, line in res])
        else:
            res, most_similar = project._locate_similar_symbol(ref, symbol)
            ret = (f"No parsed definition of {symbol} was found at this ref. "
                   "Unsupported syntax/languages may be missing from the index; "
                   "this is not proof that the implementation is absent.\n")
            if not res:
                return ret + "No similar parsed definition was found. Inspect the source paths directly.\n"
            ret += f"But here is a symbol similar to it. It's `{most_similar}`.\n"
            ret += f"The file where this symbol is located is: \n"
            ret += "\n".join([f"{file}:{line}" for file, line in res])
            ret += f"\nPlease be careful to check that this symbol indicates the same thing as the previous symbol.\n"
            return ret

    return locate_symbol


def creat_viewcode_tool(project: Project):
    @tool
    def viewcode(ref: str, path: str, startline: int, endline: int) -> str:
        """
        View a file from a specific ref of the target repository. Lines between startline and endline are shown.
        """
        return project._viewcode(ref, path, startline, endline)

    return viewcode


def create_validate_tool(project: Project):
    @tool
    def validate(ref: str, patch: str, evidence: str = '') -> str:
        """
        Validate a patch on a target ref. For "need not ported", evidence must explain
        each hunk using target files/symbols; the decision remains pending final review.
        """
        ret = project._validate(ref, patch, evidence)
        if project.poc_succeeded:
            project.persist_validated_patch(patch, ref)
        return ret

    return validate


def create_git_history_tool(project: Project):
    @tool
    def git_history() -> str:
        """
        get history for lines which relate to patch hunk.
        """
        return project._git_history()

    return git_history


def create_git_show_tool(project: Project):
    @tool
    def git_show() -> str:
        """
        show change log for a specific ref
        """
        return project._git_show()

    return git_show
