# AOSP16 -> AOSP12 Backport Agent (server snapshot)

This directory is a source snapshot of the code that was actually used by the
latest server-side grouped backport runs. It is not the older standalone
`patch-hunk-agent` package from GitHub.

Snapshot source:

- Server: `ncwang@172.28.8.69`
- Root: `/data/ncwang`
- Snapshot date: 2026-10-21
- Runner used: `backport_patch/cases/round5.sh` and
  `backport_patch/cases/round5_seq.sh`

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
  detection, ctags enclosing lookup and dependency grouping.
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
3. `v6_group.py` computes ctags enclosing functions and derives dependency
   edges; `triage.py`, when enabled, marks each hunk as port/skip.
4. Connected components of related hunks form one group.
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

## Status of the three requested mechanisms

1. **Per-hunk LLM summary and semantic contract: implemented.** The summary
   prompt is in `data_ncwang/summarize_and_group.py`.
2. **Grouping with hunk summary/contract context: implemented as a hybrid.** The
   summaries are consumed by `v6_group.py`, but the final grouping is
   deterministic: same symbol, same file+enclosing function, and introduced
   declarations form edges, then connected components become groups. There is
   no second free-form LLM grouping call in this exact runner.
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

## Configuration

The real server configuration was not copied because it contained a live API
key. Use the included `src/example.yml` and each dataset's `config.yml` to
recreate a local configuration. Never commit `.env`, `config.yml` containing a
real key, tokens or logs containing request headers.

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

## Deployment note

The scripts intentionally retain their server paths such as
`/data/ncwang/...`. To reproduce the recorded run, copy the contents of
`data_ncwang/` into `/data/ncwang/` on the target server, provide the dataset
repositories/patches/configuration, install PortGPT's requirements, and make
`ctags` available at `/data/ncwang/tools/ctags/bin/ctags`.

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
