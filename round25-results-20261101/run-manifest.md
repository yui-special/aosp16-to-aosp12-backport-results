# Run manifest

- Deployed source: `/data/ncwang/backport_patch/patch-hunk-agent-classifier-mapping-20261101/data_ncwang`
- `src/agent/invoke_llm.py` SHA-256: `03ca73e821bdc86705499d18f1400e492d1271903cf03cc824241897326ac451`
- Server-key path: `/data/ncwang/backport_patch/.deepseek-api-key-20261101` (not printed)
- Naming: user `CVE-2025-0045` maps to existing server `CVE-2026-0045`.
- All sessions are finished; no launcher/backport process remains running.

| CVE | RUN_ID | Main PID | Result root | Case log | Launcher log |
|---|---|---:|---|---|---|
| CVE-2025-32348 | `final-candidate-CVE-2025-32348-20261101` | 3992775 | `/data/ncwang/backport_patch/final-candidate-CVE-2025-32348-20261101/results/CVE-2025-32348` | `/data/ncwang/backport_patch/final-candidate-CVE-2025-32348-20261101/logs/CVE-2025-32348.batch.log` | `/data/ncwang/backport_patch/final-candidate-32348-launcher-20261101.log` |
| CVE-2025-48649 | `six-latest-20261101-CVE-2025-48649` | 4050497 | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48649/results/CVE-2025-48649` | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48649/logs/CVE-2025-48649.batch.log` | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48649.launcher.log` |
| CVE-2025-48533 | `six-latest-20261101-CVE-2025-48533` | 4050536 | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48533/results/CVE-2025-48533` | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48533/logs/CVE-2025-48533.batch.log` | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48533.launcher.log` |
| CVE-2025-48550 | `six-latest-20261101-CVE-2025-48550` | 213004 | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48550/results/CVE-2025-48550` | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48550/logs/CVE-2025-48550.batch.log` | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48550.launcher.log` |
| CVE-2025-48595 | `six-latest-20261101-CVE-2025-48595` | 354801 | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48595/results/CVE-2025-48595` | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48595/logs/CVE-2025-48595.batch.log` | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48595.launcher.log` |
| CVE-2025-0045 (server: CVE-2026-0045) | `six-latest-20261101-CVE-2026-0045` | 493893 | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2026-0045/results/CVE-2026-0045` | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2026-0045/logs/CVE-2026-0045.batch.log` | `/data/ncwang/backport_patch/six-latest-20261101-CVE-2026-0045.launcher.log` |

All final patches also live at `<result root>/complete.patch`, with `status.txt`, `verdict.md`, `chain-report.txt`, and group-status JSON files in the same result directory.

