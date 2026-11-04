"""Deterministic checks for safety guards removed from the target code."""
import re


_NULL_COMPARE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_.$>-]*)\s*(?:==|!=)\s*(?:null|nullptr|NULL)\b"
    r"|\b(?:null|nullptr|NULL)\s*(?:==|!=)\s*"
    r"([A-Za-z_][A-Za-z0-9_.$>-]*)\b"
)


def _null_names(text):
    return {
        left or right
        for left, right in _NULL_COMPARE.findall(text)
        if left or right
    }


def _has_guard(name, text):
    escaped = re.escape(name)
    return any(re.search(pattern, text) for pattern in (
        r"\b%s\s*(?:==|!=)\s*(?:null|nullptr|NULL)\b" % escaped,
        r"\b(?:null|nullptr|NULL)\s*(?:==|!=)\s*%s\b" % escaped,
        r"\b(?:Objects\.)?requireNonNull\s*\(\s*%s\b" % escaped,
        r"\b(?:checkNotNull|assertNotNull)\s*\(\s*%s\b" % escaped,
    ))


def check_null_guard_preservation(patch):
    """Reject removal of a NULL guard while the protected value remains used."""
    lines = (patch or "").splitlines()
    removed = _null_names("\n".join(
        line[1:] for line in lines if line.startswith("-")
    ))
    if not removed:
        return {
            "status": "NOT_APPLICABLE",
            "removed_guards": [],
            "unprotected": [],
        }
    final_text = "\n".join(
        line[1:] for line in lines
        if line.startswith(("+", " ")) and not line.startswith("+++")
    )
    removed_guards, unprotected = [], []
    for name in sorted(removed):
        if not re.search(r"\b%s\b" % re.escape(name), final_text):
            continue
        removed_guards.append(name)
        if not _has_guard(name, final_text):
            unprotected.append(name)
    if unprotected:
        return {
            "status": "FAIL",
            "removed_guards": removed_guards,
            "unprotected": unprotected,
            "instruction": (
                "Preserve the target NULL/NULLPTR guard, or prove the value "
                "cannot be null before dereferencing it."
            ),
        }
    return {
        "status": "PASS" if removed_guards else "NOT_APPLICABLE",
        "removed_guards": removed_guards,
        "unprotected": [],
    }
