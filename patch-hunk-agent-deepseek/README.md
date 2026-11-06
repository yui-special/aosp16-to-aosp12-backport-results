# AOSP16 -> AOSP12 Backport Agent

This directory is the maintained DeepSeek agent, based on commit `6779661` with
the local dependency, validation and final-repair improvements integrated.
Continue development here through incremental commits. The existing
`data_ncwang/` layout and the historical `../patch-hunk-agent/` snapshot are preserved.

The round24 runtime uses external launchers and configuration under `/data/lyh/aosp`.
Results are archived in `../round24-results-20261028/`. Build/test scripts, credentials,
target repositories and runtime artifacts remain outside this source directory.

## What is included

`data_ncwang/` mirrors the relevant paths below `/data/ncwang`:

- `summarize_and_group.py`: extracts fixed-revision syntax before asking an LLM
  for each hunk's structured `summary`, kind and language-neutral
  `semantic_contract` (goal, obligations, call chain and search terms).
  Symbols and parameter changes come from the parser, not the model.
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
   `triage.py`, when enabled, makes initial port/skip decisions, then reviews
   prerequisites over the complete patch before filtering. Required support
   hunks are retained with their evidence in `triaged.json`.
4. `v6_group.py` asks the grouping model for minimal cohesive semantic groups,
   then checks unique hunk coverage and prerequisite ordering. It reuses the
   triage dependency review; legacy/no-triage inputs still run dependency review
   during grouping.
5. `map_semantics.py` searches the target tree using contract terms, generic
   rename variants and surrounding code evidence, then maps each hunk to
   concrete existing target locations. Exact old scopes with resolvable types
   are located mechanically even when the summary lists prerequisite claims;
   those claims remain explicitly unverified for porting and semantic review.
   Other mappings receive parsed candidate method signatures and ranges.
6. `build_v6_export.py` writes one merged patch per group containing direct /
   renamed / moved / split / unresolved locations, semantic obligations and
   explicit handling rules.
7. PortGPT opens one session per group, applies/validates the group result,
   checks coverage, and accumulates successful groups. Existing symbol-query
   results and mapped method source are shared only at the same immutable Git
   revision. Missing upstream test classes do not remove behavior-test obligations.
8. `chain_check.py` checks changed call signatures and `make_verdict.py`
   produces the final case verdict.

Candidate tree reconstruction first applies the original diff, falling back to
`--recount` only on failure: unconditional recounting misreads traditional multi-file
diffs. Recoverable candidate-inspection errors return diagnostics to the current
group after rollback. Semantic-review retries retain valid hunk decisions, including
when another hunk has an invalid resolution of a prior finding. The judge receives
per-hunk witness/resolution requirements and its remaining invalid records to correct.
Retries retain registered evidence cited by those invalid records, including cross-file
evidence; unknown IDs remain invalid. If scope expansion leaves the candidate, pending
obligations and evidence unchanged after an `unreviewed` response, review returns
`stop_reason: no_progress` with the unresolved decisions. An identical invalid response
at the same evidence state also stops. Neither condition accepts the patch; new evidence
and different corrections can continue, and transient provider failures remain retryable.
Target mapping retains validated hunk mappings and retries only unresolved members.
Source delivered in `KNOWN TARGET FACTS` is registered in the current session's existing
source/API read tracking; omitted lines and other revisions remain unread.

Summary, initial triage and target-mapping requests run independently with a
configurable worker count. Results are published in original hunk/group order.
Each stage keeps one `<output>.checkpoint.json` beside its existing output; only
validated results are reusable. Failures preserve completed items and stop further
dispatch once observed. In-flight successful requests are also retained. Resume
keys include input content, immutable source revisions, model/request options,
parser configuration and implementation hashes. Unresolved mappings are not cached.
Initial triage reuse does not bypass the subsequent whole-patch dependency review.

Configure these stages through `frontend` in the case YAML (see `src/example.yml`):
`workers: 3`, `resume: true`, plus per-stage `summary`, `triage` and `mapping`
overrides for `workers`, `resume`, `request_options` and `read_timeout`.
The runner passes this object as `FRONTEND_OPTIONS`; existing stage-specific request
option/timeout environment variables take precedence. Set `frontend.resume: false`
for fresh stability experiments. Parallel execution does not apply to cumulative
group porting, which still depends on preceding group commits.

New summary responses must contain every contract field with its declared type.
Behavioral hunks also require nonempty goal, behavior, enclosing role and obligations.
Missing content triggers a retry of that hunk with schema feedback, then an explicit
stage failure; it cannot become a successful empty contract. New validated lists
are not truncated by the legacy normalizer. Target type lookup uses actual parsed
declarations, including nested classes, rather than same-named files. A parse gap
disables the direct-mapping shortcut; it does not establish target inapplicability.

Dependency closure does not use summary symbol names. Mechanical prerequisites
cover scoped parameters, unambiguous Java type imports used in types, calls or
annotations, new declarations and same-owner method definitions, lexically bound
old-to-new signature replacements and new-overload adaptations using an
unqualified/`this`/`Owner.this` receiver with an unchanged argument prefix,
and aconfig declaration cleanup after reference removal. Other receivers cannot
prove which same-named method or overload a call targets and remain model-reviewed.
Shadowed names, wildcard/static imports and unsupported syntax still
require behavioral review. Changed statements include unchanged receivers and
assertion context. The model reviews all original hunks only for additional
behavior/control-flow prerequisites and supplies quotes from both sides of each
new dependency; it cannot repeat, remove or reverse a locked mechanical edge.
Grouping reuses those locked signature edges and asks the model to classify only
the remaining call candidates, rather than running a second receiver-only rule.
A deterministic reduction removes a reviewed edge only when another path preserves
its reachability; mechanical and `retain_consumer` edges are never removed.
Quotes and hunk coverage are validated, but this is not a proof of semantic
equivalence. Reviewed dependencies are tied to the patch and source revisions;
they impose ordering without automatically merging an entire API chain. When an
initially skipped consumer is required to complete a selected provider's fix,
a proven overload adaptation or an evidenced model review marks the edge as
`retain_consumer`; only those explicit edges can promote a skipped consumer,
never a whole same-name/API component.
A pure deletion of one aconfig flag declaration is decided mechanically as independent
cleanup when another hunk removes the corresponding code reference; no triage model
call is made. Target occurrence counts are retained as evidence. Such cleanup cannot
become a prerequisite of the behavior fix. Other configuration changes remain subject
to semantic review rather than being skipped by filename alone.

Same-name skipped hunks carry `review_required` / `review_only_hunks` hints without
changing their port decision. Explicit `excluded_hunks` survive grouping, export
and loading, so fallback grouping cannot reinsert them. Port-required and excluded
sets must not overlap. Review-only hints are retained for inspection; they do not
represent a completed target-applicability review or enter the final selected-hunk
coverage gate. Concrete, evidenced prerequisites can still retain a skipped hunk.

The runner binds both the exported group specification and merged-input directory.
An empty input stays empty and does not restart the original hunks or establish
validation success. Explicit missing inputs fail instead of falling back. Group
specifications retain their execution order; indexed directories load only the
listed files in index order, including an empty index. Invalid indexes or missing
listed files fail. Re-export removes only obsolete generated input patches,
preserving custom files and ported results. `--list-groups` neither cleans the
target worktree nor writes merged inputs.

## Per-group comparison

`merged_hunks/ported/group-comparison.md` contains each group number, original
hunk IDs, and inline before/after patches. In cumulative mode the after patch is
the group's committed diff; whole-patch repairs are a later stage. Empty results
remain in place. Each completed group is also printed to the log.
`--only-group N` preserves the original group number. The generic runner copies
the report into the results directory.

`backporting.py -c CONFIG --repair-from COMPLETE.patch` skips group generation
and enters the same whole-patch review, repair and validation flow. The candidate
must be a complete patch against the configured target base. All selected upstream
hunks are still reviewed; old reviews and validation checkpoints are not imported.
Every full candidate is saved before checks as `merged_hunks/ported/latest-candidate.patch`,
with its base ref and SHA-256 in `latest-candidate.json`. It can be passed to `--repair-from`
with the same base/configuration. This checkpoint is explicitly unvalidated; acceptance
still comes from the final validation artifacts and `validated-checkpoints/`.
This mode enables cumulative validation automatically and cannot use `--only-group`.
The generic case runner supports the same mode through `repair_from` (a complete
candidate patch path) and `reuse_case` (an existing case directory containing
`config.yml` and exported `inputs`). It checks the upstream commit and target base,
copies only the exported group inputs, and skips the frontend stages. Results use
the runner's normal per-run directory; separate replay wrappers are unnecessary.

Test or PoC failures preserve successful results from earlier validation stages;
they cannot mark a successful production build as failed. Test repair feedback
resolves JVM traceback lines against the immutable candidate tree and includes
the enclosing method and concrete calling test. Missing or ambiguous source and
truncated scopes are explicit. These source facts do not diagnose the failure or
justify weakening an assertion.
Repair sessions also receive a read-only Git tree for the current complete candidate.
All three source-query tools accept this tree, including files and methods absent
from the base. Traceback excerpts identify their own validation tree and list the
concrete call arguments plus preceding calls in source order; this does not prove
execution order or type binding. Repair submissions are local diffs against this
exact candidate tree. The program applies them in a private index and generates
the complete replacement patch against the original base. Stale tree IDs, context
relocation and apply conflicts are rejected atomically; prior changes and the
checkout are preserved. A rejected submission can be corrected within the same
session. The exported/replay patch remains a complete patch.

Compiler feedback retains diagnostic continuation lines (`symbol`, `location`,
`required`, `found`, and `reason`) and includes candidate source around resolvable
error locations. Explicit disk-full, missing-command, DNS and device-offline
failures stop code repair and retain the candidate as unvalidated; unknown failures
are not automatically classified as infrastructure failures.

Source-read budgets are checked before Git searches or parsing. `locate_symbol`
and `find_references` accept a literal file path that restricts the search itself.
Their `symbol` argument rejects source filenames and paths before Git lookup;
`Example.java` is not interpreted as the identifier `java`. Invalid input returns
`invalid_query` with `searched: false`, not an empty search result. Use a code symbol
such as `Example.method` and put a known file location in `path`.
A unique small definition includes its source. Source already supplied by failure
feedback or tools is tracked per session, immutable ref, path and line; subsequent
reads return only new complete lines and identify omitted/truncated ranges. A new
session starts a new ledger, so evidence from an earlier session is never hidden.
`locate_symbol` supplies declarations; `find_references` supplies calls and exact
identifier occurrences for constants, fields and local variables, with source lines
and enclosing ranges. It does not repeat the declaration inventory. Lexical matches
do not prove receiver or variable binding; unparsed text is reported separately.
Missing paths, parser gaps and scoped zero matches are distinct. Searches never
silently widen a requested path. Repair prompts require a concrete missing fact
for each query and trace assertions through helper arguments, defaults, shared mocks
and production branches. Test isolation must be repaired rather than hidden by
reordering scenarios or weakening their assertions. Unlimited mode remains available.

Semantic repair feedback separates `upstream_location` (the source obligation) from
`target_locations` (verified candidate excerpts). An absent upstream filename is not
an instruction to find or create a same-named target file. Findings without target
evidence explicitly keep their location unresolved.

Summary generation and final review read complete post-change upstream functions
through Tree-sitter, joining setup and assertion hunks in the same scope. Those
immutable source facts take precedence over a hunk-local interpretation. The final
judge receives these functions directly instead of trusting generated summaries.
Acceptance of a test scenario requires a `test_witnesses` entry linking setup,
exercised call and assertion to exact upstream and candidate source quotations.
Mixed hunks can explicitly mark upstream-only mechanical test scopes inapplicable,
with a quoted obligation, reason and target evidence; matching class names are not required.
The program verifies those anchors; cross-version behavioral equivalence remains
a semantic judgment, not something proved by an evidence ID or a matching test name.

A previously missing obligation needs an explicit `resolution` before acceptance.
The program rejects claimed code fixes whose cited source is unchanged and claimed
new evidence that was already supplied. Corrections of an earlier interpretation
must be explained explicitly and still satisfy the test-witness requirements.
Reviews use `coverage_temperature` (default `0.0`) independently of generation;
this reduces sampling variability without promising deterministic model judgments.
If a test compiler repair reveals a coverage gap, repair returns to the coverage
stage using the latest findings before any further full validation.

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
A call changed in the same hunk from the old signature to the new signature with
the same receiver and argument prefix is a mechanical binding and cannot be
rejected by the grouping model. Multi-hunk local units require connected source
bindings or a fixed test/private-overload contract; sharing a scope or making
parallel formatting changes is not sufficient.
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
When production-code and test-coverage findings coexist, final repair runs sequential
production then test-coverage sessions. Each session receives the latest complete
candidate and must submit a revised complete patch; a read-only session ends with
`REPAIR_NO_CANDIDATE`. A session whose own findings remain ends with
`REPAIR_STAGE_INCOMPLETE` and cannot advance to the next stage or final acceptance.
If semantic findings are resolved but the latest test build/runtime validation fails,
up to two consecutive `test_validation` sessions receive that concrete feedback and
the latest complete candidate before final acceptance.

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
   no-change claims. A deterministic builder gathers candidate structure, source
   ranges and symbol searches from an immutable Git tree. Exact post-change text
   matches are source-location evidence, not automatic acceptance: identical blocks
   can belong to different methods or guards. A separate tool-free judge classifies
   the obligations as satisfied, inapplicable, missing, or unreviewed when context is insufficient.
   The judge receives the deterministic evidence and source/search registry.
   Overlapping source ranges share one excerpt; search records are referenced by ID
   rather than repeated. Truncated scopes carry their full boundaries and omitted line
   ranges. An unreviewed decision citing such a scope triggers deterministic expansion
   within the review retry budget; unavailable context is not a code-repair finding.
   JSON wrappers are parsed directly. Explicit per-hunk prose verdicts are converted
   locally, without a formatter model call. Ambiguous decisions require another judge
   response; original explanations and evidence IDs are retained. String evidence IDs
   are normalized locally and must resolve exactly to a registered record. Search evidence
   may support missing or inapplicable decisions, never a satisfied decision.
   Decisions are validated per hunk: valid findings survive malformed neighboring rows,
   and retries request only unresolved hunks. Retry evidence is limited to their paths,
   search/source references, and related changed signatures/calls/helpers. The complete
   candidate diff remains available for cross-file context; unrelated source excerpts
   and structure records are omitted explicitly. Returned evidence IDs must belong to
   the registry actually supplied in that request.
   `missing` lists concrete gaps; `unreviewed` lists unavailable decisions. Both prevent
   acceptance, but only concrete findings are sent to code repair.
   Citations are checked against the immutable tree built with a private index,
   without changing the working tree. Structural checks, API inventories, evidence
   extraction and the judge use the applied candidate tree and a Git
   diff from the original base to that candidate tree, so accepted patch offsets and
   recounted hunk lengths cannot invalidate source coordinates. The submitted patch
   and canonical diff are retained together in the evidence audit record.
   Identifier overlap remains a diagnostic and
   cannot grant acceptance. Earlier groups may satisfy later obligations; upstream
   names do not have to survive an equivalent backport. Intermediate candidates
   complete semantic/API checks without build/test. One recorded `submit_candidate` ends the
   repair invocation, even if the model requested additional tools in that response.
   The controller selects subsequent repair stages and directly runs build/test/PoC
   after semantic checks pass; the repair model has no `validate` tool. Build/test
   failures return to repair with fresh diagnostics. Production and test stages
   receive separate retry budgets; regressions can return to production within a
   bounded category-switch budget. Failed candidate trees are not rebuilt solely
   because diff formatting changed. Review outages retain the candidate
   and do not suppress build/test or repair of concrete compilation, test, API-chain
   or semantic findings. An unavailable review alone cannot justify rewriting code
   or accepting the patch. Review responses, deterministic evidence records,
   normalization attempts and errors are saved by stage under `coverage-review/`,
   bound to the candidate tree and patch hash, with keys redacted.
   Evidence from another candidate tree is never reused. The same tree runs `chain_check.py`; results are
   written to `global-validation.json` and per-group status files receive
   `global_covered`, `global_api_chain_ok` and `global_validation_complete`.
   A case cannot be `OK` if this global gate fails; the generation process returns
   non-zero when final validation is incomplete. The post-run chain check also
   reconstructs the candidate from the original target commit and final patch,
   independently of the current checkout, and propagates failures to the runner.

Triage preserves the relevance decision separately from target mapping.
Generic target terms such as JUnit annotations cannot promote unrelated changes,
and absent upstream names cannot discard relevant fixes. Target lookup belongs
to the subsequent mapping stage. Original triage decisions are retained in JSON.

## Configuration

`unlimited_backend: true` enables an experiment without backend model iteration,
source-read count/character, stale-query, repair retry, category-switch or semantic
review retry budgets. The default is `false`. Effective limits are logged as
`BACKEND_LIMITS`; null means unlimited. Source deduplication, fixed-tree context
checks, no-progress detection and validation acceptance remain active. Compilation,
test and network request timeouts remain execution settings. Permanent provider
rejections (including authentication or insufficient balance) still stop review.

The real server configuration was not copied because it contained a live API
key. Use the included `src/example.yml` and each dataset's `config.yml` to
recreate a local configuration. Never commit `.env`, `config.yml` containing a
real key, tokens or logs containing request headers.

Generation configuration accepts `coverage_model` (defaults to the generation
model), `llm_timeout_seconds` (300), `llm_max_retries` (1),
`coverage_review_retries` (1), and `final_repair_iterations` (20).
`final_repair_retries` (2) bounds retries in each repair stage and retries after
failed full validation. `final_repair_category_switches` (2) bounds returns from
test repair to production repair. API context reads are tracked per inventory
record across pages and filters. Oversized records remain unread until their
complete source range is delivered at the inventory's fixed Git ref; truncated
or suppressed tool output does not satisfy required reads.
`coverage_review_iterations` remains accepted for configuration compatibility but
is unused now that evidence generation is deterministic. `coverage_retries` (0)
controls only the legacy per-group heuristic retry; the final evidence review always runs.

Relevant optional environment variables:

- `DEEPSEEK_API_KEY`: DeepSeek API key (preferred).
- `LLM_KEY`: legacy alias for `DEEPSEEK_API_KEY`.
- `LLM_BASE_URL`: OpenAI-compatible endpoint
  (default `https://api.deepseek.com/v1`).
- `DEEPSEEK_MODEL`: default model for every LLM stage
  (default `deepseek-flash`).
- `LLM_MODEL`, `COVERAGE_MODEL`: generation and final-review model overrides.
- `SUMMARY_MODEL`, `TRIAGE_MODEL`, `MAP_MODEL`: per-stage model overrides.
- `SUMMARY_MAX_FAILURES`: accepted upstream summary failure count (default 0),
  enforced by both summary and triage stages.
- `ENABLE_TRIAGE=1`: enable the optional semantic triage stage.
- `TRIAGE_REQUEST_OPTIONS`: JSON request parameters (default `{"temperature":0}`).
- `TRIAGE_READ_TIMEOUT`: per-hunk response read timeout in seconds (default 60).
- `TRIAGE_DEPENDENCY_READ_TIMEOUT`: complete-patch dependency review timeout;
  defaults to `GROUP_READ_TIMEOUT`, or 300 seconds if unset.
- `TRIAGE_DEPENDENCY_REQUEST_OPTIONS`: complete-patch request options; defaults
  to `TRIAGE_REQUEST_OPTIONS`.
- `TRIAGE_DEPENDENCY_RETRIES`: retries for an invalid dependency review (default 1).
  Unresolved per-hunk decisions and invalid reviews stop the stage; failures are
  never treated as skips. `TRIAGE_POST_ONLY=1` replays saved decisions and existing
  dependency reviews without model calls; it does not create a missing review.
- `SEMANTIC_MAP_OFFLINE=1`: run only candidate discovery for testing. Normal
  runs require an online semantic mapping and abort on API failure.
- `STOP_AFTER_EXPORT=1`: stop after the PortGPT input has been generated; used
  for pipeline smoke tests.
- `GROUP_MODEL`: semantic grouping model; defaults to `SUMMARY_MODEL`.
- `GROUP_RETRIES`: retries per invalid grouping subtask (default 1).
- `GROUP_ANALYSIS_WORKERS`: concurrent signature/local-scope reviews (default 3).
  This controls API concurrency, not group size. Without a triage dependency
  review, dependency review follows them.
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
- The checker accepts `--base <commit>` to reconstruct the candidate from the
  patch; this overrides inherited `CHAIN_CHECK_REF`. Legacy callers may use
  `CHAIN_CHECK_REF` or `CHAIN_CHECK_BASE_REF`. Missing/invalid references are errors,
  never a request to scan the mutable working tree.
- `CHAIN_CHECK_GLOBS`: comma-separated source patterns used by the chain check
  (defaults include Java, Kotlin, AIDL, C, C++, headers, Rust and Python).

## Deployment note

Deterministic source facts are implemented in `src/tools/source_structure.py` and
`src/tools/source_checks.py` under `data_ncwang/backport_patch/tools/patch-backporting/`.
Each group receives `merged-hunk-NN.api-inventory.json`. A whole-patch signature
catalog expands the group's target methods through calls to related changed APIs,
including target-only callers before porting starts. Mapped target names seed this
search too. Records include fixed commits, files, lines, receivers and arguments.
After a candidate changes signatures, validation returns a fresh inventory combining
the applied candidate's files with untouched callers at its exact base. The
`find_references` tool also provides inventories on demand. `locate_symbol` accepts
`Class.method`, `Class::method` and `Class#method`; other owners remain explicit
candidates instead of fuzzy name substitutions.
These are syntax candidates, not a type-resolved call graph; renamed APIs still
require semantic mapping. Parse gaps remain explicit. Enclosing functions seed a
whole-repository inventory only when they are changed APIs or explicit target
mappings, so a generic anonymous `run` method is not expanded across the tree.

Initial group prompts include the inventory manifest, compact signature changes,
a required-read checklist and complete small per-symbol inventories within
`context_inline_chars` (default 6000; 0 disables inline delivery). Rendering a prompt
does not complete reads: the executor records inline delivery when invoking the
session. No partial symbol inventory is marked complete. Remaining records use the
`query_api_context` tool can read only the current group's artifact; it accepts
no artifact or filesystem path and provides bounded, paginated definitions and
references filtered by symbol, exact path/scope and argument count. Before
editing, the model must query every non-inlined symbol with `record_type=all`;
`validate` rejects missing required reads. It must then inspect every concrete
edit site with `viewcode` at the current cumulative ref and recheck candidate
API facts after signature changes. Required-read completion is saved in each
group status. YAML `context_query_limit` (default 10, maximum 20) and
`context_query_chars` (default 24000, minimum 2000) bound each response without
cutting a JSON record in half.

`src/tools/porting_context.py` keeps one actual committed diff per cumulative group.
Later groups receive an index of all prior changes plus complete relevant hunks,
selected by symbols and paths. YAML `ledger_chars` (default 6000, minimum 0) limits
diff details; omitted hunks retain their refs, paths and ranges. Exported prompts
use compact syntax facts and one semantic contract per hunk; full evidence stays
in `groups.json`.

YAML `query_stale_limit` (default 4, minimum 1) controls consecutive reads with no
new facts. Repeated results are suppressed at the threshold with a request to
validate a candidate or identify missing information; new ranges/revisions remain
readable. This is a convergence hint, not a forced acceptance or skip. Group status
files record queries and actual executor exit reasons, distinguishing explicit
limits, exceptions and returns without a validated candidate.

Mapping accepts parsed declarations and carries their real signatures/ranges into
the porting input. Patch validation rejects newly introduced, exact Java duplicate
method/parameter declarations and reports changed if/else structure as review facts.
It does not reject control-flow restructuring merely because its syntax differs.
Missing distinctive identifiers mean unconfirmed local coverage, never automatic
success. The generic runner passes `--repo` and `--commit` to summary generation;
grouping reuses matching syntax evidence instead of extracting it again.

The Python stages load modules relative to this source snapshot. The generic
runner accepts `BACKPORT_ROOT`, `BACKPORT_PYTHON`, `BACKPORT_SCRIPTS`,
`BACKPORT_DATASET`, `BACKPORT_CONFIG`, `BACKPORT_CASE` and `OUT` from the external
launcher, so runtime paths need not be patched into the source. Dataset settings
come from YAML. Explicit stage environment variables override the corresponding
YAML settings; DeepSeek defaults apply only when no value is configured. Generation
and review models remain independently configurable. API keys are inherited from
the environment or read from the input configuration, then omitted from the generated
case configuration. The target revision is pinned before pipeline stages run.
Build/test hooks and environment-specific configuration remain external.
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
