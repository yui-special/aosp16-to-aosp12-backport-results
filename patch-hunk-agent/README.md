# AOSP16 -> AOSP12 Backport Agent

This directory contains the grouped backport agent used for round24 on
2026-10-28. It originated from the 2026-10-21 server snapshot under `/data/ncwang`;
the existing `data_ncwang/` source layout is preserved.

The round24 runtime uses external launchers and configuration under `/data/lyh/aosp`.
Results are archived in `../round24-results-20261028/`. Build/test scripts, credentials,
target repositories and runtime artifacts remain outside this source directory.

## What is included

`data_ncwang/` mirrors the relevant paths below `/data/ncwang`:

- `summarize_and_group.py`: one LLM call per original hunk, producing a
  structured `summary`, symbol, kind and a language-neutral
  `semantic_contract` (goal, obligations, call chain and search terms).
- `triage.py`: decides whether each hunk needs to be ported.
- `map_semantics.py`: maps each hunk contract to existing target paths and
  symbols. It classifies the result as direct, renamed, moved, split,
  unresolved or absent and mechanically validates every returned path/symbol.
- `backport_patch/cases/CVE-2025-48649/scripts/v6_group.py`: language
  detection, Tree-sitter source evidence and semantic grouping.
- `backport_patch/cases/CVE-2025-48649/scripts/build_v6_export.py`: creates
  the per-group patches and the instructions sent to PortGPT.
- `backport_patch/cases/run_case_generic.sh`: the complete case runner.
- `chain_check.py` and `make_verdict.py`: post-run checks and verdicts.
- `backport_patch/tools/patch-backporting/`: the current PortGPT source that
  was modified on the server, including grouped-session, coverage and
  cumulative-commit behavior.

Runtime data, target repositories, generated patches, logs, virtual
environments, backups and secrets are intentionally excluded.

## Pipeline order

1. `v6_group.py --detect-only` detects source languages.
2. `summarize_and_group.py` produces a per-hunk semantic contract.
3. `v6_group.py --enclosing-only` extracts before/after source structure;
   `triage.py`, when enabled, marks each hunk as port/skip.
4. `v6_group.py` asks the grouping model for minimal cohesive semantic groups,
   then checks unique hunk coverage and prerequisite ordering.
5. `map_semantics.py` searches the target tree using contract terms, generic
   rename variants and surrounding code evidence, then maps each hunk to
   concrete existing target locations.
6. `build_v6_export.py` writes one merged patch per group containing direct /
   renamed / moved / split / unresolved locations, semantic obligations and
   explicit handling rules.
7. PortGPT opens one session per group, applies/validates the group result,
   checks coverage, and accumulates successful groups.
8. `chain_check.py` checks changed call signatures and `make_verdict.py`
   produces the final case verdict.

## Per-group comparison

`merged_hunks/ported/group-comparison.md` contains each group number, original
hunk IDs, and inline before/after patches. In cumulative mode the after patch is
the group's committed diff; whole-patch repairs are a later stage. Empty results
remain in place. Each completed group is also printed to the log.
`--only-group N` preserves the original group number. The generic runner copies
the report into the results directory.

Grouping has no numerical hunk cap. The model reviews each changed signature and
each local scope separately, then reviews cross-scope references and test behavior.
Each local review must account for every member, including hunks spanning multiple
methods. Imports and new fields attach to a new definition or the first concrete
use. Identical edits in identical complete source scopes share one unit, ignoring
only leading indentation in the edit. Changes belonging to one test scenario
stay together. Direct initialization copies, alternative constructor initializers,
changed values and their local assertions, and relocated initializers provide
explicit local bindings. A correction consisting solely of casts in existing
assignment expressions stays separate from another initialization or normalization;
the whole hunk is retained when it updates multiple control paths. Private
overloads that delegate within a family and introduce the same named parameter
share that contract. These policies use source evidence, not case IDs.

Confirmed bindings are bidirectional; prerequisites are directed. The planner
forms minimal strongly connected components, orders them by their prerequisites
with original hunk IDs as a tie-breaker, and removes redundant transitive edges.
An ordinary call chain therefore does not become one large group. Every selected
hunk appears exactly once. Invalid model decisions are retried with feedback,
then fail the stage if still invalid; no previous model response is reused.
Target mapping follows grouping. The porting prompt uses that mapping and target
code to combine obligations from multiple upstream hunks into one target edit when
they resolve to the same implementation, preserving coverage of every member.

Both grouping and `locate_symbol` use `src/tools/source_structure.py`. Install the
pinned Tree-sitter dependencies in `requirements.txt` / `pyproject.toml`. Symbol
lookup reads Git blobs and caches immutable revisions; it does not reset or switch
the working tree. Grouping maps old coordinates to the patch parent and new
coordinates to the patch commit, including insertions, deletions and renamed paths.
The model receives enclosing scopes, before/after signatures, changed calls,
assignments and source excerpts. Complete-file overload comparisons distinguish
replacement signatures from compatible additions. Comments in argument lists
are not counted as arguments. Parameter prerequisites are directed evidence;
matching names/arity and assignment references remain candidates requiring review,
not a resolved type graph. Old summaries and E1–E4 keyword edges are not supplied
as grouping evidence. New or changed test assertions require their actual behavior
implementation; adapting an old call alone does not require every later guard.
Confirmed relations and signature reviews are saved in group details, with their
source anchors in `grouping_anchors`. Source
evidence also accompanies group exports.

Java, C/C++, Kotlin, Python and Rust grammars are pinned Python dependencies. AIDL
uses its own separately installed grammar (for example AndroidIDEOfficial's
`tree-sitter-aidl`); configure its compiled library with:

```json
{"grammars": {"aidl": {"library": "/path/to/aidl.so", "symbol": "tree_sitter_aidl"}}}
```

Keep grammar binaries and environment-specific configuration outside this repository.
An unavailable installed grammar fails explicitly. Unconfigured languages, missing
files and syntax recovery are labelled in evidence; they never mean “no dependency”.
Tree-sitter syntax extraction does not replace compilation or type resolution.

Final whole-patch validation pins the original target commit. Every candidate is
a complete replacement patch, applied atomically on that clean base; only hunk
line counts may be recounted. Context is never force-rewritten in final validation,
and reconstruction never falls back to applying a candidate on the cumulative branch.

## Status of the three requested mechanisms

1. **Per-hunk LLM summary and semantic contract: implemented.** The summary
   prompt is in `data_ncwang/summarize_and_group.py`.
2. **Grouping with source evidence: implemented with a model.**
   Focused signature, local behavior and dependency reviews determine bindings
   and prerequisites. Reasons and prerequisite hunk IDs are saved
   in `groups.json` and supplied to each porting session.
3. **A group may mix ported and not-ported hunks: implemented.** The explicit
   instruction is generated in `build_v6_export.py` and repeated in PortGPT's
   `USER_PROMPT_HUNK_GROUP` in `src/agent/prompt.py`.
4. **Renamed/moved/split target mapping: implemented.** Existing upstream paths
   are not assumed to be directly portable. If an imported or referenced type
   is absent, the hunk becomes a mapping candidate. `split` requires at least
   two validated target locations.
5. **Infrastructure failures cannot become semantic skips: implemented.**
   Failed summaries, triage calls and mapping calls terminate the runner with a
   non-zero status. `UNRESOLVED` is explicitly forbidden from being treated as
   "need not ported" without concrete target-code evidence.
6. **Final-patch global validation: implemented.** After all group sessions,
   every selected hunk is reviewed against the final composed patch, including
   no-change claims. A separate model review classifies each obligation as
   satisfied, inapplicable, or missing, citing exact candidate source lines.
   Citations are checked against an immutable Git tree built with a private
   index, without changing the working tree. Identifier overlap remains a
   diagnostic and cannot grant acceptance. Earlier groups may satisfy later
   obligations; upstream names do not have to survive an equivalent backport.
   Missing obligations enter whole-patch repair. Each repaired candidate must
   pass this review before real compilation/testing; evidence from another
   candidate tree is never reused. The same tree runs `chain_check.py`; results are
   written to `global-validation.json` and per-group status files receive
   `global_covered`, `global_api_chain_ok` and `global_validation_complete`.
   A case cannot be `OK` if this global gate fails.

Triage preserves the relevance decision separately from target mapping.
Generic target terms such as JUnit annotations cannot promote unrelated changes,
and absent upstream names cannot discard relevant fixes. Target lookup belongs
to the subsequent mapping stage. Original triage decisions are retained in JSON.

## Configuration

The real server configuration was not copied because it contained a live API
key. Use the included `src/example.yml` and each dataset's `config.yml` to
recreate a local configuration. Never commit `.env`, `config.yml` containing a
real key, tokens or logs containing request headers.

Generation configuration accepts `coverage_model` (defaults to the generation
model), `llm_timeout_seconds` (300), `llm_max_retries` (1),
`coverage_review_iterations` (20), `coverage_review_retries` (1), and
`final_repair_iterations` (20). `coverage_retries` (0) controls only the legacy
per-group heuristic retry; the final evidence review always runs.

Relevant optional environment variables:

- `LLM_BASE_URL`: OpenAI-compatible endpoint (default GreatRouter).
- `SUMMARY_MODEL`, `TRIAGE_MODEL`, `MAP_MODEL`: per-stage model overrides.
- `SUMMARY_MAX_FAILURES`: accepted summary failure count (default is 10%,
  minimum 2); triage independently rejects any failed upstream summary.
- `ENABLE_TRIAGE=1`: enable the optional semantic triage stage.
- `SEMANTIC_MAP_OFFLINE=1`: run only candidate discovery for testing. Normal
  runs require an online semantic mapping and abort on API failure.
- `STOP_AFTER_EXPORT=1`: stop after the PortGPT input has been generated; used
  for pipeline smoke tests.
- `GROUP_MODEL`: semantic grouping model; defaults to `SUMMARY_MODEL`.
- `GROUP_RETRIES`: retries per invalid grouping subtask (default 1).
- `GROUP_ANALYSIS_WORKERS`: concurrent signature/local-scope reviews (default 3).
  This controls API concurrency, not group size. Dependency review follows them.
- `GROUP_REQUEST_OPTIONS`: JSON request parameters supported by the configured
  grouping provider, such as `{"thinking":{"type":"enabled"},"reasoning_effort":"high"}`.
  No provider-specific mode is assumed by default; model/messages/stream cannot be overridden.
- `GROUP_READ_TIMEOUT`: grouping response read timeout in seconds (default 60).
  Increase it for a provider's long reasoning responses; JSON repair uses the same settings.
- `SOURCE_STRUCTURE_CONFIG`: optional JSON configuration for grammar modules or shared
  libraries, extension overrides, `context_lines` (default 3) and
  `max_context_lines` (default 80). These limit context excerpts, not group size.
- `CHAIN_CHECK_SCRIPT`: path to the API-chain checker; the generic runner
  derives it relative to its project root.
- `CHAIN_CHECK_GLOBS`: comma-separated source patterns used by the chain check
  (defaults include Java, Kotlin, AIDL, C, C++, headers, Rust and Python).

## Deployment note

The Python stages load modules relative to this source snapshot. The generic
runner accepts `BACKPORT_ROOT`, `BACKPORT_PYTHON`, `BACKPORT_SCRIPTS`,
`BACKPORT_DATASET`, `BACKPORT_CONFIG`, `BACKPORT_CASE` and `OUT` from the external
launcher, so runtime paths need not be patched into the source. Dataset settings
come from YAML; `llm_model` and `llm_base_url` select the generation model and
endpoint. Build/test hooks and environment-specific configuration remain external.
Install the included requirements and configure any additional Tree-sitter grammars
through `SOURCE_STRUCTURE_CONFIG`. Existing runner defaults remain available.

## Generic mapping checks

The following checks were run against the recorded AOSP target refs:

- 48550: the absent A16 `FrameworkParsingPackageUtils` is no longer treated as
  a direct port; candidate discovery lists A12's
  `core/java/android/content/pm/parsing/ParsingPackageUtils.java`.
- 32348: candidate discovery resolves `ActivityStarter`,
  `BackgroundLaunchProcessController`, `WindowProcessController` and
  `VisibleActivityProcessTracker`; 13 hunks remain explicitly `unresolved`
  instead of silently becoming skips.
- 2026-0045: relocated BT paths resolve to `bta/jv/bta_jv_act.cc` and
  `stack/btm/btm_sec.cc`.
- 2026-46054: facility-dependent hunks remain unresolved when the required
  backing-file infrastructure is absent.
- 48533 with triage enabled: the two security-semantics hunks remain in two
  non-empty groups; the aconfig removal and newline-only hunks are skipped.
- Infrastructure failure: an unavailable mapping endpoint returns non-zero,
  removes stale output, and never emits a false successful mapping.
