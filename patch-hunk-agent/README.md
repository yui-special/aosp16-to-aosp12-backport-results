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
  structured `summary`, symbol, kind and parameter-change label.
- `triage.py`: decides whether each hunk needs to be ported.
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
2. `summarize_and_group.py` asks the LLM for one structured summary per hunk.
3. `v6_group.py` computes ctags enclosing functions and derives dependency
   edges; `triage.py` marks each hunk as port/skip.
4. Connected components of related hunks form one group.
5. `build_v6_export.py` writes one merged patch per group plus relocation and
   per-hunk analysis hints.
6. PortGPT opens one session per group, applies/validates the group result,
   checks coverage, and accumulates successful groups.
7. `chain_check.py` checks changed call signatures and `make_verdict.py`
   produces the final case verdict.

## Status of the three requested mechanisms

1. **Per-hunk LLM summary: implemented.** The summary prompt is in
   `data_ncwang/summarize_and_group.py`.
2. **Grouping with hunk summary context: implemented as a hybrid.** The
   summaries are consumed by `v6_group.py`, but the final grouping is
   deterministic: same symbol, same file+enclosing function, and introduced
   declarations form edges, then connected components become groups. There is
   no second free-form LLM grouping call in this exact runner.
3. **A group may mix ported and not-ported hunks: implemented.** The explicit
   instruction is generated in `build_v6_export.py` and repeated in PortGPT's
   `USER_PROMPT_HUNK_GROUP` in `src/agent/prompt.py`.

## Configuration

The real server configuration was not copied because it contained a live API
key. Use the included `src/example.yml` and each dataset's `config.yml` to
recreate a local configuration. Never commit `.env`, `config.yml` containing a
real key, tokens or logs containing request headers.

## Deployment note

The scripts intentionally retain their server paths such as
`/data/ncwang/...`. To reproduce the recorded run, copy the contents of
`data_ncwang/` into `/data/ncwang/` on the target server, provide the dataset
repositories/patches/configuration, install PortGPT's requirements, and make
`ctags` available at `/data/ncwang/tools/ctags/bin/ctags`.
