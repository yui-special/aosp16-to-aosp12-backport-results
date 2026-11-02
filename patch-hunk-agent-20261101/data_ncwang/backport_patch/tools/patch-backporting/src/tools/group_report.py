"""Compact, inline before/after patches for each group."""

import copy
import os
import re
import tempfile
from pathlib import Path


def _diff_block(text):
    fence = "`" * max(3, 1 + max((len(x) for x in re.findall(r"`+", text)), default=0))
    return "%sdiff\n%s\n%s" % (fence, text.rstrip("\n"), fence)


class GroupComparison:
    def __init__(self, ported_dir, selected, logger):
        self.path = Path(ported_dir) / "group-comparison.md"
        self.groups = {
            position: {"hunks": copy.deepcopy(group.get("hunks", [])),
                       "text": group.get("text", "")}
            for position, group in selected
        }
        self.outputs = {}
        self.logger = logger
        self._save()

    def record(self, position, patch, empty_note="未产出补丁"):
        self.outputs[position] = _diff_block(patch) if patch.strip() else empty_note
        self._save()
        self.logger.info("[GROUP COMPARISON]\n%s", self._render_group(position))

    def _render_group(self, position):
        group = self.groups[position]
        return "\n\n".join([
            "## Group %02d — hunks %s" % (position, group["hunks"]),
            "### 移植前", _diff_block(group["text"]),
            "### 移植后", self.outputs.get(position, "尚未完成"),
        ])

    def _save(self):
        report = "\n\n".join(self._render_group(i) for i in self.groups) + "\n"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", errors="surrogateescape", newline="\n",
                dir=self.path.parent, prefix=".group-comparison-", delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(report)
            os.replace(temporary, self.path)
        except OSError as exc:
            self.logger.warning("Failed to write group comparison: %s", exc)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
