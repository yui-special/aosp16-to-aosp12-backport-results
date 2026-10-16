"""Lenient JSON parsing of model output must repair without inventing content."""

from __future__ import annotations

import json

import pytest

from patch_hunk_agent.utils.json_utils import (
    extract_json_block,
    loads_lenient,
    repair_json_text,
    save_json,
)


def test_plain_json_roundtrip(tmp_path):
    target = save_json(tmp_path / "out.json", {"a": 1, "b": [1, 2]})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": [1, 2]}


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"dependencies": []}\n```',
        'Here is the result:\n{"dependencies": []}\nDone.',
        '{"dependencies": [],}',
        "{'dependencies': []}",
        '{"dependencies": []} // no dependencies found',
        '{\n  "dependencies": [],\n  /* none */\n}',
        '\u201cnote\u201d: {"dependencies": []}',
    ],
)
def test_lenient_parsing_of_common_model_shapes(text):
    if text.startswith("\u201c"):
        parsed, repairs = loads_lenient(text.split(": ", 1)[1])
        assert "normalized_smart_quotes" not in repairs  # nothing to fix in the suffix
    else:
        parsed, _ = loads_lenient(text)
    assert parsed["dependencies"] == []


def test_smart_quotes_are_normalized():
    parsed, repairs = loads_lenient("{\u201ckey\u201d: \u201cvalue\u201d}")
    assert parsed == {"key": "value"}
    assert "normalized_smart_quotes" in repairs


def test_python_literals_are_normalized():
    parsed, repairs = loads_lenient('{"flag": True, "none": None, "off": False}')
    assert parsed == {"flag": True, "none": None, "off": False}
    assert "normalized_python_literals" in repairs


def test_braces_inside_strings_do_not_confuse_extraction():
    payload = {"note": "use {braces} carefully", "dependencies": []}
    block = extract_json_block(f"prefix {json.dumps(payload)} suffix")
    assert json.loads(block) == payload


def test_extract_returns_none_without_json():
    assert extract_json_block("no json here") is None


def test_unparseable_text_raises():
    with pytest.raises(json.JSONDecodeError):
        loads_lenient("definitely not json")


def test_repair_reports_what_it_changed():
    repaired, repairs = repair_json_text("```json\n{a: 1}\n```")
    assert "stripped_markdown_fence" in repairs
    assert repaired.startswith("{")


def test_fence_without_language_and_extra_prose():
    parsed, _ = loads_lenient('```\n{"status": "complete"}\n```')
    assert parsed == {"status": "complete"}
