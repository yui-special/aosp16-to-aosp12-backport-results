# Evaluation inputs (format samples)

These files document the expected input format for `scripts/run_evaluation.py`.
They are **not** experimental results and must not be reported as such - replace
them with real runs against real patches.

## Dataset (`dataset.example.json`)

```json
{
  "cases": [
    {
      "patch_id": "patch_001",
      "hunks": ["H1", "H2"],
      "reference_dependencies": [
        {"source_hunk": "H1", "target_hunk": "H2", "category": "DEFINITION_USE", "strength": "STRONG"}
      ],
      "reference_groups": [["H1", "H2"]],
      "outcome": {
        "backport_success": true,
        "compile_pass": true,
        "test_pass": false,
        "tokens": 15234,
        "retries": 1,
        "runtime_s": 42.5
      }
    }
  ]
}
```

`reference_dependencies` and `reference_groups` must come from human review of
the real patch; `outcome` records what the backport run actually produced.

## Predictions (`predictions.example.json`)

```json
{
  "predictions": [
    {
      "patch_id": "patch_001",
      "analysis": {"dependencies": [{"source_hunk": "H1", "target_hunk": "H2",
                                     "category": "DEFINITION_USE", "strength": "STRONG"}]},
      "grouping": {"groups": [{"group_id": "G1", "hunk_ids": ["H1", "H2"]}]},
      "grouping_document": {"status": "complete"}
    }
  ]
}
```

The `grouping`/`analysis` blocks are the outputs of `patch-hunk-agent group`;
`outcome` can be added per prediction to score Backport results.
