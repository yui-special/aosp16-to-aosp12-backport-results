"""Small, deterministic lookup over the precomputed semantic mapping.

The resolver deliberately returns only the bindings for the current group.  It
must not be used as a second oracle: callers still verify every returned target
path/symbol against the candidate tree.
"""
import json
import os
import re


RESOLVED_TYPES = {
    "direct",
    "renamed",
    "moved",
    "split",
    "merged_into",
    "behavior_equivalent",
    "create_method",
    "accessor_rewrite",
    "distributed_obligation",
}


def _clip(value, limit):
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _string_list(value, limit=6, chars=240):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [_clip(item, chars) for item in value if str(item).strip()][:limit]


class MappingResolver:
    def __init__(self, mapping_path="", capabilities_path=""):
        self.mapping_path = str(mapping_path or "")
        self.capabilities_path = str(capabilities_path or "")
        self.entries = {}
        self.capabilities = []
        self.error = ""
        self._load()

    @property
    def available(self):
        return bool(self.mapping_path and os.path.isfile(self.mapping_path))

    def _load(self):
        if self.mapping_path and os.path.isfile(self.mapping_path):
            try:
                with open(self.mapping_path, encoding="utf-8") as handle:
                    payload = json.load(handle)
                for group in payload.get("groups") or []:
                    for item in group.get("hunk_mappings") or []:
                        try:
                            idx = int(item.get("hunk"))
                        except (TypeError, ValueError):
                            continue
                        self.entries[idx] = item
                        for capability in item.get("capability_resolutions") or []:
                            if not isinstance(capability, dict):
                                continue
                            if str(capability.get("resolution", "")).upper() != "INLINE_TRUE":
                                continue
                            self.capabilities.append({
                                "provider": idx,
                                "capability": capability.get("capability"),
                                "resolution": "INLINE_TRUE",
                                "evidence": capability.get("evidence", ""),
                            })
            except (OSError, ValueError, TypeError) as exc:
                self.error = "mapping_load_failed: %s" % exc
        if self.capabilities_path and os.path.isfile(self.capabilities_path):
            try:
                with open(self.capabilities_path, encoding="utf-8") as handle:
                    payload = json.load(handle)
                if isinstance(payload, list):
                    self.capabilities = payload
            except (OSError, ValueError, TypeError) as exc:
                self.error = (self.error + "; " if self.error else "") + (
                    "capability_load_failed: %s" % exc
                )

    @staticmethod
    def _parse_ids(hunk_ids):
        if isinstance(hunk_ids, int):
            return [hunk_ids]
        values = re.findall(r"-?\d+", str(hunk_ids or ""))
        return sorted({int(value) for value in values if int(value) >= 0})

    @staticmethod
    def _row(idx, item):
        mapping_type = str(
            item.get("resolution") or item.get("mapping_type") or "unresolved"
        ).strip().lower()
        mappings = []
        for mapped in (item.get("mappings") or [])[:3]:
            mappings.append({
                "path": _clip(mapped.get("path"), 300),
                "symbol": _clip(mapped.get("symbol"), 200),
                "evidence": _clip(mapped.get("evidence"), 240),
                "covers": _string_list(mapped.get("covers"), limit=3, chars=180),
            })
        rewrites = []
        for row in (item.get("accessor_rewrites") or [])[:4]:
            if isinstance(row, dict):
                rewrites.append({
                    "source": _clip(row.get("source"), 200),
                    "target": _clip(row.get("target"), 240),
                })
        capabilities = []
        for row in (item.get("capability_resolutions") or [])[:4]:
            if isinstance(row, dict):
                capabilities.append({
                    "capability": _clip(row.get("capability"), 180),
                    "resolution": _clip(row.get("resolution"), 40),
                    "evidence": _clip(row.get("evidence"), 180),
                })
        return {
            "hunk": idx,
            "resolution": mapping_type,
            "targets": mappings,
            "reason": _clip(item.get("reason"), 320),
            "requires_prerequisite": _string_list(
                item.get("required_prerequisites"), limit=4
            ),
            "required_bindings": _string_list(item.get("required_bindings"), limit=4),
            "required_guards": _string_list(item.get("required_guards"), limit=4),
            "accessor_rewrites": rewrites,
            "capability_resolutions": capabilities,
        }

    def resolve(
        self,
        hunk_ids,
        source_symbol="",
        semantic_role="",
        required_binding="",
        ref="",
        limit=3,
    ):
        ids = self._parse_ids(hunk_ids)
        terms = [
            str(value).strip()
            for value in (source_symbol, semantic_role, required_binding)
            if str(value).strip()
        ]
        if ids:
            selected = [(idx, self.entries[idx]) for idx in ids if idx in self.entries]
        else:
            # A bounded term fallback handles an agent that does not know the original
            # hunk ID yet. It never returns the complete mapping.
            ranked = []
            for idx, item in self.entries.items():
                haystack = json.dumps(item, ensure_ascii=False)
                score = sum(1 for term in terms if re.search(re.escape(term), haystack))
                if score:
                    ranked.append((score, idx, item))
            selected = [(idx, item) for _score, idx, item in
                        sorted(ranked, key=lambda row: (-row[0], row[1]))[:limit]]
        rows = [self._row(idx, item) for idx, item in selected[:limit]]

        capabilities = []
        capability_haystack = " ".join(terms + [str(idx) for idx in ids])
        for item in self.capabilities:
            encoded = json.dumps(item, ensure_ascii=False)
            provider = str(item.get("provider", ""))
            if (
                not capability_haystack.strip()
                or any(re.search(re.escape(term), encoded) for term in terms)
                or (provider and re.search(r"(?<!\d)%s(?!\d)" % re.escape(provider),
                                          capability_haystack))
            ):
                capabilities.append({
                    key: item.get(key)
                    for key in (
                        "id", "provider", "consumer", "capability",
                        "resolution", "evidence",
                    )
                    if key in item
                })
        capabilities = capabilities[:3]

        resolutions = {row["resolution"] for row in rows}
        if resolutions & RESOLVED_TYPES:
            status = "RESOLVED"
        elif "not_applicable" in resolutions:
            status = "NOT_APPLICABLE_CANDIDATE"
        elif "requires_prerequisite" in resolutions:
            status = "REQUIRES_MAPPING_REVIEW"
        elif rows:
            status = "NEEDS_EVIDENCE"
        else:
            status = "UNMAPPED"
        result = {
            "status": status,
            "target_ref": ref,
            "hunks": rows,
            "capability_resolutions": capabilities,
            "instruction": (
                "Use these as compact binding candidates, then verify target code. "
                "Search exhaustion or UNMAPPED is not proof that no port is needed."
            ),
        }
        if self.error:
            result["error"] = _clip(self.error, 300)
        return result
