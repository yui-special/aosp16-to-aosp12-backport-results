import json
import os
import re
import traceback
from typing import Generator, List, Tuple

import Levenshtein

from tools.logger import logger

blacklist = [
    ".rst",
    ".yaml",
    ".yml",
    ".md",
    ".tcl",
    "CHANGES",
    "ANNOUNCE",
    "NEWS",
    ".pem",
    ".js",
    ".sha1",
    ".sha256",
    ".uuid",
    ".test",
    "manifest",
    ".xml",
    "_test.go",
    ".json",
    ".golden",
    ".txt",
    ".mdx",
]


def find_most_similar_files(target_filename: str, search_directory: str) -> List[str]:
    """
    Find the five file paths that are most similar to non-existent files.

    Args:
        target_filename (str): The target file's name which we want to find out.
        search_directory (str): Directory name which we need to find in.

    Returns:
        List[str]: List of the five most similar file.
    """
    top_n = 5
    similarity_list = []

    # Walk through all subdirectories and files in the search directory
    for root, dirs, files in os.walk(search_directory):
        for filename in files:
            # Calculate the Levenshtein distance between the target filename and the current filename
            distance = Levenshtein.distance(target_filename, filename)
            relative_path = os.path.relpath(
                os.path.join(root, filename), search_directory
            )
            similarity_list.append((distance, relative_path))

    # Sort the list by distance in ascending order and get the top N results
    similarity_list.sort(key=lambda x: x[0])
    top_similar_files = [
        relative_path for distance, relative_path in similarity_list[:top_n]
    ]

    return top_similar_files


def find_most_similar_block(
    pattern: List[str], main: List[str], p_len: int, dline_flag: bool = False
) -> Tuple[int, int]:
    """
    Finds the most similar block of lines in the main list compared to the pattern list using Levenshtein distance.

    Args:
        pattern (List[str]): The list of code lines to match.
        main (List[str]): The list of lines to search within.
        p_len (int): The length of the pattern.
        dline_flag (bool, optional): A flag indicating whether to ignore lines starting with '+' or '-'. Defaults to False.

    Returns:
        Tuple[int, int]: A tuple containing the starting index of the most similar block in the main list (1-based index)
                         and the minimum Levenshtein distance.
    """
    min_distance = float("inf")
    best_start_index = 1

    for i in range(len(main) - p_len + 1):
        distance = Levenshtein.distance(
            "\n".join(main[i : i + p_len]), "\n".join(pattern)
        )
        if distance < min_distance and not (
            dline_flag and (main[i].startswith("+") or main[i].startswith("-"))
        ):
            min_distance = distance
            best_start_index = i + 1

    # try to fix offset, align the pattern with the most similar block
    if not dline_flag:
        offset_flag = False
        offset = float("inf")
        lineno = best_start_index
        for i in range(p_len):
            if len(pattern[i].strip()) < 3:
                continue
            for j in range(-5, 6):
                try:
                    if pattern[i].strip() == main[lineno - 1 + j].strip():
                        offset_flag = True
                        if abs(j - i) < abs(offset):
                            offset = j - i
                except:
                    pass
            if offset_flag:
                best_start_index += offset
                break

    return best_start_index, min_distance


def extract_context(lines: list) -> Tuple[list, int, list, int]:
    """
    Process the input string by removing certain lines and returning the processed string and the count of processed lines.

    Args:
        input_string (str): The input string to be processed.

    Returns:
        tuple[str, int]: A tuple containing the processed string and the count of processed lines.
    """
    processed_lines = []
    add_lines = []
    for line in lines:
        if line.startswith(" "):
            processed_lines.append(line[1:])
        elif line.startswith("-"):
            processed_lines.append(line[1:])
        elif line.startswith("+"):
            add_lines.append(line[1:])

    processed_lines_count = len(processed_lines)

    return processed_lines, processed_lines_count, add_lines, len(add_lines)


def revise_patch(
    patch: str, project_path: str, revise_context: bool = False
) -> Tuple[str, bool]:
    """fix mistakes in generated patch.
    1. wrong line numbers.
    2. wrong format: not startswith ` `, `-` or `+`
    3. wrong context lines: a) wrong indention. b) wrong lines.

    Args:
        patch (str): patch to be revised.
        project_path (str): CVE project source code in local.
        revise_context (bool, optional): True means force to revise all context lines. Defaults to False.

    Returns:
        Tuple[str, bool]: revised patch and fix flag.
    """

    def revise_hunk(lines: list[str], target_file_lines: list[str]) -> tuple[str, bool]:
        """fix lines from "@@" to the end"""
        fixed = False
        if len(lines[-1]) == 0 or "\ No newline at end of file" in lines[-1]:
            lines = lines[:-1]

        # fix corrupt patch
        tmp_lines = []
        for line in lines[1:]:
            if line.startswith("+") or line.startswith("-") or line.startswith(" "):
                tmp_lines.append(line)
            else:
                tmp_lines.append(" " + line)

        # fix mismatched lines
        # force_flag: force to revise all mismatched lines, otherwise fix indentation only
        # TODO: if the distance is close, it should be revised
        # XXX: if the distance is far, it should not be revised
        contexts, num_context, _, _ = extract_context(tmp_lines)
        lineno, _ = find_most_similar_block(
            contexts, target_file_lines, num_context, False
        )
        i = 0
        revised_lines = []
        for line in tmp_lines:
            if line.startswith(" ") or line.startswith("-"):
                sign = line[0]
                new_line = target_file_lines[lineno - 1 + i]
                if revise_context:
                    revised_lines.append(" " + new_line.strip("\n"))
                elif re.sub(r"\s+", "", line[1:]) == re.sub(r"\s+", "", new_line):
                    revised_lines.append(sign + new_line.strip("\n"))
                else:
                    revised_lines.append(line)
                i += 1
            else:
                revised_lines.append(line.replace("'s ", "->"))

        if revise_context:
            logger.debug("force to revise all context lines")
            last_line = 0
            for line in tmp_lines:
                if not line.startswith("-"):
                    continue
                dline = []
                dline.append(line[1:])
                dlineno, dist = find_most_similar_block(
                    dline, revised_lines[last_line:], 1, True
                )
                dlineno = dlineno + last_line
                last_line = dlineno
                revised_lines[dlineno - 1] = "-" + revised_lines[dlineno - 1][1:]

            if not revised_lines[-1].startswith(" "):
                revised_lines.append(
                    " " + target_file_lines[lineno - 1 + i].strip("\n")
                )

        # fix wrong line number
        orignal_line_number = sum(
            1 for line in revised_lines if not line.startswith("+")
        )
        patched_line_number = sum(
            1 for line in revised_lines if not line.startswith("-")
        )
        chunks = re.findall(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@(.*)", lines[0])[0]
        if chunks[0] != chunks[2]:
            fixed = True
        header = f"@@ -{chunks[0]},{orignal_line_number} +{chunks[2]},{patched_line_number} @@{chunks[4]}\n"

        return header + "\n".join(revised_lines), fixed

    def revise_block(lines: list[str]) -> tuple[list[str], bool]:
        """fix "--- a/" and "+++ b/", and call revise_hunk."""
        try:
            file_path_a = re.findall(r"--- a/(.*)", lines[0])[0]
            fixed_file_path_a = os.path.normpath(file_path_a)
        except:
            file_path_a = fixed_file_path_a = lines[0]

        try:
            file_path_b = re.findall(r"\+\+\+ b/(.*)", lines[1])[0]
            fixed_file_path_b = os.path.normpath(file_path_b)
        except:
            file_path_b = fixed_file_path_b = lines[1]

        block_fixed = (
            file_path_a != fixed_file_path_a or file_path_b != fixed_file_path_b
        )
        assert (
            (file_path_a == file_path_b and fixed_file_path_a == fixed_file_path_b)
            or fixed_file_path_a == "--- /dev/null"
            or fixed_file_path_b == "--- /dev/null"
        )

        fixed_lines = [
            f"--- a/{fixed_file_path_a}".replace("a/--- ", ""),
            f"+++ b/{fixed_file_path_b}".replace("b/--- ", ""),
        ]
        try:
            with open(os.path.join(project_path, file_path_a), "rb") as f:
                content = f.read().decode("utf-8", errors="ignore")
                file_content = [line.rstrip("\n") for line in content.splitlines()]
        except:
            # do not revise patch if file changed, handle changed file in `_apply_hunk`
            return lines, False

        last_line = -1
        for line_no in range(2, len(lines)):
            if lines[line_no].startswith("@@"):
                if last_line != -1:
                    hunk_lines, hunk_fixed = revise_hunk(
                        lines[last_line:line_no], file_content
                    )
                    fixed_lines.append(hunk_lines)
                    block_fixed = block_fixed or hunk_fixed
                last_line = line_no
        if last_line != -1:
            hunk_lines, hunk_fixed = revise_hunk(lines[last_line:], file_content)
            fixed_lines.append(hunk_lines)
            block_fixed = block_fixed or hunk_fixed

        return fixed_lines, block_fixed

    try:
        lines = patch.splitlines()
        fixed_lines = []

        last_line = -1
        fixed = False
        for line_no in range(len(lines)):
            if lines[line_no].startswith("--- a/") or lines[line_no].startswith(
                "--- /dev/null"
            ):
                if last_line != -1:
                    block_lines, block_fixed = revise_block(lines[last_line:line_no])
                    fixed_lines += block_lines
                    fixed = fixed or block_fixed
                last_line = line_no
        if last_line != -1:
            block_lines, block_fixed = revise_block(lines[last_line:])
            fixed_lines += block_lines
            fixed = fixed or block_fixed

        return "\n".join(fixed_lines) + "\n", fixed
    except Exception as e:
        logger.debug("Failed to revise patch")
        logger.debug(e)
        logger.warning("".join(traceback.TracebackException.from_exception(e).format()))
        return patch, False


def split_patch(patch: str, flag_commit: bool) -> Generator[str, None, None]:
    """
    Split a patch into individual blocks.

    Args:
        patch (str): The patch to be split.
        flag_commit (bool): Whether the patch exists commit message.

    Yields:
        str: Each individual block of the patch.

    Returns:
        None
    """

    def split_block(lines: list[str]):
        file_path_line_a = lines[0]
        file_path_line_b = lines[1]
        last_line = -1
        for line_no in range(2, len(lines)):
            if lines[line_no].startswith("@@"):
                if last_line != -1:
                    content = (
                        file_path_line_a
                        + "\n"
                        + file_path_line_b
                        + "\n"
                        + "\n".join(lines[last_line:line_no])
                    )
                    yield content
                last_line = line_no
        if last_line != -1:
            content = (
                file_path_line_a
                + "\n"
                + file_path_line_b
                + "\n"
                + "\n".join(lines[last_line:])
            )
            yield content

    try:
        lines = patch.splitlines()
        message = ""
        last_line = -1
        for line_no in range(len(lines)):
            if lines[line_no].startswith("--- a/"):
                if last_line >= 0:
                    if flag_commit:
                        for x in split_block(lines[last_line : line_no - 2]):
                            yield message + x
                    else:
                        for x in split_block(lines[last_line:line_no]):
                            yield message + x
                if last_line == -1 and flag_commit:
                    message = "\n".join(lines[: max(line_no - 2, 0)])
                if any(
                    lines[line_no].endswith(blacklist_item)
                    for blacklist_item in blacklist
                ):
                    last_line = -2
                else:
                    last_line = line_no
            if lines[line_no].startswith("--- /dev/null"):
                if last_line >= 0:
                    if flag_commit:
                        for x in split_block(lines[last_line : line_no - 3]):
                            yield message + x
                    else:
                        for x in split_block(lines[last_line:line_no]):
                            yield message + x
                if last_line == -1 and flag_commit:
                    message = "\n".join(lines[: max(line_no - 3, 0)])
                if any(
                    lines[line_no + 1].endswith(blacklist_item)
                    for blacklist_item in blacklist
                ):
                    last_line = -2
                else:
                    last_line = line_no
        if last_line >= 0:
            for x in split_block(lines[last_line:]):
                yield message + x

    except Exception as e:
        logger.debug("Failed to split patch")
        logger.debug(e)
        logger.warning("".join(traceback.TracebackException.from_exception(e).format()))
        return None


def split_patch_indexed(patch: str, flag_commit: bool = True):
    """
    把 patch 拆成「按文件顺序、hunk 顺序」编号的 hunk 列表（不做黑名单过滤）。

    与 split_patch 的区别：这里给每个 hunk 一个稳定的下标，便于按编号把若干
    互相依赖的 hunk 归并成一组（merged hunk）。

    Args:
        patch (str): 完整 patch 文本。
        flag_commit (bool): patch 里是否带 commit message。

    Returns:
        tuple[list[str], list[dict]]: (message, hunks)
            message: 提交信息（与 split_patch 的取法一致，去掉紧邻的
                     `diff --git` / `index` 两行）
            hunks:   [{"idx": 0, "file": "a/b.java",
                       "text": "--- a/...\n+++ b/...\n@@ ..."}, ...]
    """
    lines = patch.splitlines()
    first = None
    for i, line in enumerate(lines):
        if line.startswith("--- a/") or line.startswith("--- /dev/null"):
            first = i
            break
    if first is None:
        return "", []

    message = "\n".join(lines[: max(first - 2, 0)]) if flag_commit else ""

    hunk_bodies = []
    head = []
    cur = None

    def flush():
        nonlocal cur
        if cur is not None:
            hunk_bodies.append(cur)
            cur = None

    for line in lines[first:]:
        if line.startswith("--- a/") or line.startswith("--- /dev/null"):
            flush()
            head = [line]
            continue
        if line.startswith("+++ "):
            head.append(line)
            continue
        if line.startswith("@@"):
            flush()
            cur = {"head": list(head), "body": [line]}
            continue
        if cur is not None:
            if line[:1] in (" ", "+", "-", "\\"):
                # 合法的 hunk 正文行
                cur["body"].append(line)
            else:
                # hunk 之后到下一个文件块之间的非正文行（diff --git / index /
                # mode / similarity 等）属于下一个文件，不能算进当前 hunk，
                # 否则 revise_patch 会把它们当成上下文行。
                flush()
            continue
    flush()

    hunks = []
    for idx, h in enumerate(hunk_bodies):
        file_path = "(new file)"
        for line in h["head"]:
            if line.startswith("--- a/"):
                file_path = line[len("--- a/") :]
        hunks.append(
            {
                "idx": idx,
                "file": file_path,
                "text": "\n".join(h["head"] + h["body"]),
            }
        )
    return message, hunks


def load_hunk_groups(path: str):
    """读取 hunk 分组文件（JSON）。

    文件格式见 48649-groups.json：
        {"groups": [{"id": 1, "name": "...", "hunks": [0, 1, 6],
                     "concern": "...", "why_together": "..."}, ...],
         "dependencies": [...], "notes": "..."}
    """
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_merged_hunks(merged, out_dir: str):
    """
    把 merged hunk 落到磁盘，便于查看/复现。

    产出结构:
        <out_dir>/index.md                      —— 分组总览
        <out_dir>/original/merged-hunk-NN.patch —— 该组「上游原始」hunk 合并后的 patch
        <out_dir>/original/index.json           —— 机读索引（放在 patch 旁边，供 load_merged_hunk_files 用）
        <out_dir>/ported/                       —— 预留给移植后的结果（由调用方写入）

    Args:
        merged (list[dict]): group_patch() 的返回值。
        out_dir (str): 输出目录。

    Returns:
        tuple[str, str]: (original_dir, ported_dir)
    """
    orig_dir = os.path.join(out_dir, "original")
    ported_dir = os.path.join(out_dir, "ported")
    os.makedirs(orig_dir, exist_ok=True)
    os.makedirs(ported_dir, exist_ok=True)

    head = [
        "# merged hunk 列表",
        "",
        "每个 merged hunk = 一组互相依赖的原始 hunk，作为一个整体、在一个独立 session 里移植。",
        "",
        "| # | group id | 原始 hunk 编号 | 文件数 | 组名 |",
        "|---|---|---|---|---|",
    ]
    body = []
    index = []
    for i, g in enumerate(merged):
        fn = "merged-hunk-%02d.patch" % i
        with open(os.path.join(orig_dir, fn), "w", encoding="utf-8", newline="\n") as f:
            f.write(g["text"])
        index.append(
            {
                "file": fn,
                "gid": g["gid"],
                "name": g["name"],
                "hunks": g["hunks"],
                "concern": g.get("concern", ""),
                "why": g.get("why", ""),
                "files": g["files"],
                "hints": g.get("hints", ""),
                "mapping_status": g.get("mapping_status", []),
                "strict_requires_patch": bool(g.get("strict_requires_patch", False)),
            }
        )
        head.append(
            "| %d | `%s` | %s | %d | %s |"
            % (
                i,
                g["gid"],
                ", ".join(str(x) for x in g["hunks"]),
                len(g["files"]),
                g["name"],
            )
        )
        body.append("")
        body.append("## merged hunk %d（group %s）：%s" % (i, g["gid"], g["name"]))
        body.append("")
        body.append("- 原始 hunk 编号：%s" % ", ".join(str(x) for x in g["hunks"]))
        body.append("- 涉及文件：")
        for fp in g["files"]:
            body.append("  - `%s`" % fp)
        if g.get("concern"):
            body.append("- 关注点：%s" % g["concern"])
        if g.get("why"):
            body.append("- 为什么必须一起改：%s" % g["why"])
        body.append("- 文件：`original/%s`" % fn)

    with open(os.path.join(out_dir, "index.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(head + body) + "\n")
    with open(
        os.path.join(orig_dir, "index.json"), "w", encoding="utf-8", newline="\n"
    ) as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    logger.info(
        "Merged hunks dumped to %s (%d merged hunk(s))" % (orig_dir, len(merged))
    )
    return orig_dir, ported_dir


def load_merged_hunk_files(dir_path: str, index_name: str = "index.json"):
    """
    直接把「已经合并好的」merged hunk 当作输入读进来（不再现场从 patch 合并）。

    目录里应有若干 `*.patch`（每个文件 = 一组 merged hunk，按文件名排序依次处理），
    可选一个 `index.json`（dump_merged_hunks 写出的索引，带 gid/name/concern/why）。
    这样只要替换/改写这些 patch 文件，就等于改了 PortGPT 的输入。

    Args:
        dir_path (str): merged hunk 所在的目录（通常是 <patch_dataset_dir>/merged_hunks/original）。
        index_name (str): 索引文件名，缺省 index.json。

    Returns:
        list[dict]: 与 group_patch() 的 merged 结构一致，额外带 source_file 字段。
    """
    if not dir_path or not os.path.isdir(dir_path):
        return []

    files = sorted(f for f in os.listdir(dir_path) if f.endswith(".patch"))

    meta = {}
    idx_path = os.path.join(dir_path, index_name)
    if os.path.isfile(idx_path):
        try:
            with open(idx_path, "r", encoding="utf-8") as f:
                for item in json.load(f):
                    meta[item.get("file", "")] = item
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 %s 失败，忽略其元信息: %s" % (idx_path, e))

    merged = []
    for i, fn in enumerate(files):
        with open(os.path.join(dir_path, fn), "r", encoding="utf-8") as f:
            text = f.read()
        m = meta.get(fn, {})
        patch_files = [
            line[len("--- a/") :]
            for line in text.split("\n")
            if line.startswith("--- a/")
        ]
        merged.append(
            {
                "gid": m.get("gid", "file-%d" % i),
                "name": m.get("name", fn),
                "hunks": m.get("hunks", []),
                "concern": m.get("concern", ""),
                "why": m.get("why", ""),
                "files": m.get("files", patch_files),
                "hints": m.get("hints", ""),
                "mapping_status": m.get("mapping_status", []),
                "strict_requires_patch": bool(m.get("strict_requires_patch", False)),
                "text": text,
                "source_file": os.path.join(dir_path, fn),
            }
        )
    logger.info("Loaded %d pre-merged hunk file(s) from %s" % (len(merged), dir_path))
    return merged


def select_merged_hunks(merged, only_index=None):
    """
    挑出这次要处理哪些 merged hunk。

    - only_index 为 None / "" / 负数 -> 全部（默认，一次进程跑完所有组）；
    - only_index >= 0            -> 只跑该下标的那个组（「一次进程 = 一组」）。

    返回 [(原始下标, group), ...]。下标始终是它在 merged 里的原位置，
    这样装了 --only-group 之后 ported/merged-hunk-NN.patch 的编号也不会错位。
    """
    if not merged:
        return []
    if only_index is None or only_index == "":
        return list(enumerate(merged))
    try:
        i = int(only_index)
    except (TypeError, ValueError):
        logger.error("--only-group 需要一个整数，收到 %r，改为跑全部" % (only_index,))
        return list(enumerate(merged))
    if i < 0:
        return list(enumerate(merged))
    if i >= len(merged):
        logger.error(
            "--only-group %d 越界：这次只有 %d 个 merged hunk（下标 0..%d）"
            % (i, len(merged), len(merged) - 1)
        )
        return []
    return [(i, merged[i])]


# ---------------------------------------------------------------------------
# 「这一组是不是真的被完整移植了」的机械校验
#
# 背景：分组只解决了「一次给一组」，但没有保证「模型产出的补丁覆盖了整组」。
# 模型完全可能只写了 H3 的内容、漏掉 H6，而 round_succeeded 仍然是 True。
# 这里用一个不依赖 LLM 的检查：把每个成员 hunk「新增的、且不是被删掉的」标识符
# 抽出来，看产出的补丁里有没有出现。
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]{3,}")

# 语言关键字 / 常见类型名，没有区分度，不能用来判定"这个 hunk 被移植了"
_IDENT_STOPWORDS = {
    "boolean", "return", "void", "static", "final", "public", "private",
    "protected", "class", "interface", "import", "package", "this", "super",
    "extends", "implements", "throws", "override", "String", "Object",
    "Integer", "Boolean", "Long", "Exception", "Integer", "NonNull", "Nullable",
    "UserIdInt", "android", "java", "lang", "util", "value", "null", "true",
    "false", "that", "with", "from", "when", "else", "case", "break",
}


def hunk_added_identifiers(hunk_text: str):
    """取一个 hunk「新增的、且未在删除行里出现」的标识符集合。"""
    added, removed = set(), set()
    for line in hunk_text.split("\n"):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added |= set(_IDENT_RE.findall(line[1:]))
        elif line.startswith("-"):
            removed |= set(_IDENT_RE.findall(line[1:]))
    return (added - removed) - _IDENT_STOPWORDS


def hunk_coverage_report(member_hunks, produced_patch_text: str, need_not_ported: bool = False,
                         strict_requires_patch: bool = False, directly_applied: bool = False):
    """
    检查「这一组的产出」是否覆盖了组内每个成员 hunk。

    Args:
        member_hunks: [{"idx": 原始 hunk 下标, "file": 路径, "text": hunk 原文}, ...]
        produced_patch_text: 这一组实际产出的补丁文本（可能为空）
        need_not_ported: 模型显式声明「这组不需要移植」

    Returns:
        {"complete": bool, "members": [{"idx","file","covered","matched","reason"}],
         "missing": [idx...], "need_not_ported": bool}

    判定规则（宁可漏报也不误报）：
      - strict_requires_patch + 空产出  -> 全部未覆盖（不允许静默 need_not_ported）；
      - need_not_ported=True            -> 全部视为覆盖（模型显式声明了）；
      - 该 hunk 没新增任何标识符          -> 视为覆盖（纯格式/注释类改动，无法判定）；
      - 该 hunk 新增的标识符有出现在产出里 -> 覆盖；
      - 其余                             -> 未覆盖（产出里找不到这个 hunk 的痕迹）。
    """
    produced = set(_IDENT_RE.findall(produced_patch_text or "")) - _IDENT_STOPWORDS

    # 逐行相似度兜底：标识符改名（如 applicationInfo -> appInfo）时，
    # 用"新增行的 token 重合度"判断这一处是否被移植，避免误报。
    produced_added = [
        l[1:] for l in (produced_patch_text or "").split("\n")
        if l.startswith("+") and not l.startswith("+++") and len(l.strip()) > 6
    ]
    produced_line_tokens = [
        {t.lower() for t in _IDENT_RE.findall(l)} - _IDENT_STOPWORDS for l in produced_added
    ]

    def line_similarity_covered(hunk_text: str) -> bool:
        for line in hunk_text.split("\n"):
            if not line.startswith("+") or line.startswith("+++"):
                continue
            if len(line.strip()) <= 6:
                continue
            toks = {t.lower() for t in _IDENT_RE.findall(line[1:])} - _IDENT_STOPWORDS
            if not toks:
                continue
            for other in produced_line_tokens:
                if other and len(toks & other) / len(toks) >= 0.6:
                    return True
        return False

    # 兄弟成员共同新增的标识符没有区分度，判定时一律排除
    added_sets = {h["idx"]: hunk_added_identifiers(h["text"]) for h in member_hunks}
    shared = set()
    if len(added_sets) > 1:
        seen = {}
        for s in added_sets.values():
            for t in s:
                seen[t] = seen.get(t, 0) + 1
        shared = {t for t, c in seen.items() if c > 1}

    members = []
    for h in member_hunks:
        distinctive = added_sets[h["idx"]] - shared
        hit = sorted(distinctive & produced)
        if directly_applied:
            covered, reason = True, "directly_applied"
        elif strict_requires_patch and not produced_patch_text.strip():
            covered, reason = False, "strict_mapping_requires_patch"
        elif need_not_ported:
            covered, reason = True, "need_not_ported"
        elif not distinctive:
            covered, reason = True, "no_distinctive_identifier"
        elif hit:
            covered, reason = True, "identifier_found"
        elif line_similarity_covered(h["text"]):
            covered, reason = True, "line_similarity"
        else:
            covered, reason = False, "no_trace_in_output"
        members.append(
            {
                "idx": h["idx"],
                "file": h["file"],
                "covered": covered,
                "matched": hit[0] if hit else "",
                "reason": reason,
            }
        )
    missing = [m["idx"] for m in members if not m["covered"]]
    return {
        "complete": not missing,
        "members": members,
        "missing": missing,
        "need_not_ported": need_not_ported,
        "strict_requires_patch": strict_requires_patch,
        "directly_applied": directly_applied,
    }


def uncovered_hunks(hunks, groups_spec):
    """分组规格没覆盖到的 hunk 下标列表（空 = 完整划分）。"""
    covered = set()
    if groups_spec:
        for g in groups_spec.get("groups", []):
            covered |= {int(i) for i in g.get("hunks", [])}
    return [h["idx"] for h in hunks if h["idx"] not in covered]


def group_patch(patch: str, groups_spec=None, flag_commit: bool = True):
    """
    按分组规格把 patch 的 hunk 合并成若干「merged hunk」。

    互相依赖的 hunk（例如：hunk a 引入/修改了一个接口方法或参数，hunk b 使用了它）
    会被合进同一个 merged hunk，后续作为一个整体移植，避免只改一半导致编译不过或语义不对。

    Args:
        patch (str): 完整 patch 文本。
        groups_spec (dict | None): 分组规格；为 None 时退化成「每个 hunk 单独一组」，
                                   即原有的逐个 hunk 行为。
        flag_commit (bool): patch 里是否带 commit message。

    Returns:
        tuple[str, list[dict]]: (message, merged)
            merged: [{"gid":..., "name":..., "hunks":[原始 hunk 编号...],
                      "concern":..., "why":..., "files":[...], "text": 合并后的 patch 文本}, ...]
                    按组内最小 hunk 编号排序，保证输出顺序稳定。
    """
    message, hunks = split_patch_indexed(patch, flag_commit)
    if not hunks:
        return message, []

    by_idx = {h["idx"]: h for h in hunks}
    assign = {}
    specs = []

    if groups_spec:
        for g in groups_spec.get("groups", []):
            members = [int(i) for i in g.get("hunks", [])]
            members = [i for i in members if i in by_idx]
            if not members:
                continue
            specs.append(
                {
                    "gid": g.get("id", len(specs) + 1),
                    "name": g.get("name", ""),
                    "members": members,
                    "concern": g.get("concern", ""),
                    "why": g.get("why_together", ""),
                }
            )
            for i in members:
                assign[i] = len(specs) - 1
            logger.info(
                "group %s: %d hunks %s -> %s"
                % (
                    g.get("id", len(specs)),
                    len(members),
                    members,
                    g.get("name", ""),
                )
            )

    # 没被分组覆盖到的 hunk：各自单独成组（保持原来的逐 hunk 行为）
    uncovered = [h["idx"] for h in hunks if h["idx"] not in assign]
    if uncovered and groups_spec:
        logger.warning(
            "分组规格没有覆盖全部 hunk！有 %d 个 hunk 不在任何组里，"
            "会被当成独立组逐个移植：%s（如果这不是你想要的，检查分组文件的完整性）"
            % (len(uncovered), uncovered)
        )
    for h in hunks:
        if h["idx"] not in assign:
            specs.append(
                {
                    "gid": "solo-%d" % h["idx"],
                    "name": "single hunk %d" % h["idx"],
                    "members": [h["idx"]],
                    "concern": "",
                    "why": "",
                }
            )
            assign[h["idx"]] = len(specs) - 1

    merged = []
    for gi in sorted(range(len(specs)), key=lambda k: min(specs[k]["members"])):
        s = specs[gi]
        members = sorted(s["members"])
        text = "\n".join(by_idx[i]["text"] for i in members) + "\n"
        merged.append(
            {
                "gid": s["gid"],
                "name": s["name"],
                "hunks": members,
                "concern": s["concern"],
                "why": s["why"],
                "files": [by_idx[i]["file"] for i in members],
                "text": text,
            }
        )
    return message, merged
