# Historical regression dataset

The implementation document (section 15.2) requires regression cases built from
**real** patches and real source/target trees:

* CVE-49649: the cross-hunk variable definition around H11.
* CVE-49649: the function interface and caller coordination around H14/H15.
* H6 and H7: verify, with the relevant code retrieved, whether a dependency
  really requires coordinated porting.

Those patches are not part of this repository, so no expected grouping is
hard-coded here. Instead this directory is a harness: drop real cases in
`tests/regression/cases/*.json` and `tests/test_regression_dataset.py` will run
them through the full pipeline and compare the result with the reviewed
reference. Until then the test skips with an explicit message.

## Case format

```json
{
  "case_id": "cve-49649-h11",
  "description": "cross-hunk variable definition observed in CVE-49649",
  "patch_path": "tests/regression/cases/data/cve-49649.patch",
  "source_revision": "SOURCE_COMMIT",
  "target_revision": "TARGET_COMMIT",
  "source_tree": "tests/regression/cases/data/source",
  "target_tree": "tests/regression/cases/data/target",
  "hunk_scope": ["H11", "H12"],
  "reference_dependencies": [
    {
      "source_hunk": "H11",
      "target_hunk": "H12",
      "category": "DEFINITION_USE",
      "strength": "STRONG"
    }
  ],
  "reference_groups": [["H11", "H12"]],
  "thresholds": {"dependency_recall": 1.0, "over_grouping_rate": 0.0}
}
```

Notes:

* `patch_path`, `source_tree` and `target_tree` are resolved relative to the
  repository root.
* `hunk_scope` (optional) restricts scoring to the hunks under review, so a
  large patch can be recorded one issue at a time.
* `reference_dependencies` / `reference_groups` must come from human review of
  the real code; they are the ground truth and must not be filled in from the
  agent's own output.
* Set `PATCH_HUNK_AGENT_REGRESSION_PROVIDER=openai` (plus the usual
  `PATCH_HUNK_AGENT_*` variables) to score a real model instead of the offline
  heuristic stand-in.

`hunk_scope` and the reference entries are checked for schema validity even when
the referenced files are missing, so a malformed case fails loudly instead of
being skipped.
