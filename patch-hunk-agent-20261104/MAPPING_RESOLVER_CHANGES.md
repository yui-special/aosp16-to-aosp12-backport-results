# Mapping fallback and adaptive search fuse

## Behavior

1. PortGPT can call `resolve_mapping(ref, hunk_ids, source_symbol, semantic_role,
   required_binding)`.
2. The tool returns at most three bindings for the requested hunks. It never
   returns the complete mapping file.
3. Repeated failure for one symbol fuses only that symbol. Excessive search
   failures fuse the group.
4. After group-level search fusion, `need not ported` requires one mapping
   lookup when mapping is available.
5. `UNMAPPED`, `NEEDS_EVIDENCE`, `REQUIRES_MAPPING_REVIEW`, and query-budget
   exhaustion cannot be converted into `need not ported`.
6. Search and mapping budgets reset at the start of every group.

## Mapping resolutions

The mapping stage now accepts:

```text
direct
renamed
moved
split
merged_into
behavior_equivalent
create_method
accessor_rewrite
distributed_obligation
inline_true
requires_prerequisite
unresolved
absent
```

`create_method` may name a new symbol in an existing target file.
`accessor_rewrite` carries compact source-to-target expressions.
`inline_true` carries an explicit capability resolution.

## Prompt size control

- Group prompt receives only the current group's mapping hints.
- Source-structure evidence is compacted to scopes, calls, writes, declarations
  and signature changes; full context arrays are omitted.
- Mapping/source evidence lines are clipped and candidate lists are capped.
- The on-demand resolver limits output to three hunk bindings, four requirements
  and four capability rows.

## Capability consistency

`config-capability-resolutions.json` is passed to the global semantic review,
so `INLINE_TRUE` remains effective after all groups are composed.

For fixed-read-only config capabilities, `INLINE_TRUE` now has deterministic
precedence over a mapping's `requires_prerequisite` result. It removes only
flag/aconfig declaration obligations; consumer code obligations remain.

## Mechanical mapping and final validation

- Every applicable mapping that targets an existing source file must include
  that source path as its main target.
- Incomplete mapping output is sent through one bounded remap attempt. If it
  remains incomplete, it is marked `unresolved` instead of being accepted.
- Agent sessions retain intermediate steps.
- When an Agent ends without a successful `validate`, the system extracts a
  complete diff from its final output and programmatically calls `validate`.
- If no diff is available, one no-tool finalizer call receives the bounded
  group patch and compact tool evidence and must return a complete diff or
  `NO_PATCH`.
- Finalizer candidates retain the same mechanical/API-chain validation gates.

## Inactive relations

Relations involving triage-excluded hunks remain in `group_details` as
`dropped_relations` and are shown to PortGPT as explicit obligations instead of
being silently discarded.

## Deterministic test/debug triage

- A new `hunk_classifier.py` stage runs before summarization.
- It classifies hunks as production source, test source, build, config,
  resource, metadata, generated or unknown.
- Test/debug classifications bypass both summary LLM calls and triage LLM calls.
- Test-file hunks never enter production patch generation by default.
- Behavior assertions become `TEST_EQUIVALENCE_REQUIRED`.
- Test infrastructure becomes `test_infrastructure_only`.
- Fixture/signature/expected-string changes become optional test obligations.
- New-file hunks whose `file` field is `(new file)` resolve their actual path
  from the `+++ b/...` header before classification.
- Test obligations are written to
  `merged_hunks/ported/test-obligations.json` and do not block production Patch
  coverage.

On 32348, the classifier routes 11 hunks to the semantic pipeline, 2 debug
hunks to `SKIP_PRODUCTION`, and all 25 test hunks to test-only actions. This
avoids 25 summarize calls, 25 triage calls, mapping calls and production
grouping for upstream test code.
