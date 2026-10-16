# Patch Hunk Dependency Grouping Agent

在 PortGPT 执行 Patch Backport **之前**，分析原始 Patch 中不同 Hunk 之间的依赖关系，
把必须协同移植的 Hunk 分到同一组，并输出机器可读的分组结果。

本 Agent 只做**依赖分析与分组**：它不生成、不修改、不套用任何 backport 代码，也不修改
被分析的仓库或 Patch。

实现依据：`Patch Hunk Dependency Grouping Agent 实现文档 v1.0`。

---

## 1. 仓库现状与复用优先检查

本仓库（`group_patch_llm/`）在本次实现前是一个**空的 git 仓库**（只有 `.git/`，`main`
分支无任何提交）。因此：

* 仓库内**没有**可复用的 Patch 解析、符号提取、代码检索或 LLM 调用模块；
* 也**没有** PortGPT 代码可以审查其接口；
* 因此按实现文档第 11 节的目录模板从零实现，并把复用落成两类显式接口：
  * `EvidenceExtractor` / `ContextProvider` 可与外部已有能力对接（把结果放进
    `existing_analysis`，或替换 `SourceAccess` 实现即可）；
  * `PortGPTAdapter` 是与下游移植系统对接的唯一入口，未修改任何下游逻辑。

如果后续把本目录放进 PortGPT 仓库，建议做法见第 9 节。

---

## 2. 目录结构（实际实现）

```text
group_patch_llm/
├── README.md                     # 本文件
├── pyproject.toml                # 打包 / pytest / ruff 配置
├── .env.example                  # 环境变量样例（不含任何真实密钥）
├── .gitignore
├── configs/
│   ├── default.yaml              # 阈值、上下文、分析、分组、校验参数
│   └── models.yaml               # 逻辑 provider：mock / mock-heuristic / openai
├── prompts/
│   ├── dependency_analysis.txt   # 实现文档 §7.3 的 System Prompt（原文）
│   └── evidence_extraction.txt   # 可选：LLM 版阶段 A 符号抽取 Prompt
├── src/patch_hunk_agent/
│   ├── __init__.py               # 版本号与异常类型
│   ├── __main__.py               # python -m patch_hunk_agent
│   ├── cli.py                    # group / inspect / validate / evaluate / demo / providers
│   ├── config.py                 # YAML + ${ENV} 展开 + 内置最小 YAML 解析器
│   ├── schemas.py                # 全部数据契约与校验函数
│   ├── patch_parser.py           # unified diff → 稳定 Hunk 清单
│   ├── context_provider.py       # 源/目标版本代码上下文（git / 本地目录 / 无访问）
│   ├── evidence_extractor.py     # 阶段 A 符号 + 阶段 B 候选依赖证据
│   ├── dependency_analyzer.py    # 阶段 C/D：LLM 依赖判定、修复重试、按需补证
│   ├── dependency_validator.py   # 依赖记录独立校验与降级
│   ├── dependency_graph.py       # 无向依赖图与连通分量
│   ├── grouping_engine.py        # 确定性分组 + 跨组强依赖修复
│   ├── partition_validator.py    # 分组完整性/强依赖/未解决项校验（§10.4 输出）
│   ├── portgpt_adapter.py        # 分组 → 下游移植任务 + 可追溯记录
│   ├── portgpt_spec.py           # PortGPT hunk_groups.json 桥接（对齐/导出/导入/校验）
│   ├── evaluator.py              # §16.2 指标
│   ├── pipeline.py               # 端到端编排（新增模块，见第 12 节）
│   ├── llm/
│   │   ├── base.py               # LLMClient / LLMResponse / Prompt 装载 / JSON 解析
│   │   ├── provider.py           # OpenAI 兼容 chat/completions（仅标准库 HTTP）
│   │   └── mock.py               # 脚本化 Mock + 离线启发式 Mock
│   └── utils/
│       ├── git_utils.py          # 只读 git 封装（拒绝非只读子命令）
│       ├── json_utils.py         # 确定性读写 + LLM 输出宽容解析
│       └── logging_utils.py      # 日志、运行记录(JSONL)、密钥脱敏
├── tests/
│   ├── conftest.py / helpers.py
│   ├── fixtures/                 # 5 个 diff + source/target 版本树
│   ├── test_patch_parser.py      test_evidence_extractor.py
│   ├── test_context_provider.py  test_git_utils.py
│   ├── test_dependency_analyzer.py test_dependency_validator.py
│   ├── test_dependency_graph.py  test_grouping_engine.py
│   ├── test_partition_validator.py test_end_to_end.py
│   ├── test_cli.py               test_config.py
│   ├── test_llm_provider.py      test_json_utils.py
│   ├── test_portgpt_adapter.py   test_evaluator.py
│   ├── test_portgpt_spec.py      # PortGPT 契约对齐、往返、CLI（含真实目录可选测试）
│   ├── test_regression_dataset.py  # §15.2 回归 harness（无真实样例时 skip）
│   └── regression/README.md      # 真实回归样例的登记格式
├── examples/
│   ├── input/sample.diff         # 4 个 Hunk、3 个文件、2 组强依赖的示例 Patch
│   ├── source/ target/           # 示例的源/目标版本代码
│   ├── output/                   # demo 输出目录
│   └── evaluation/               # 数据集/预测格式样例（非实验结果）
├── scripts/run_evaluation.py     # Baseline vs Treatment 指标对比
└── results/                      # CLI 默认输出目录
```

---

## 3. 处理流程

```text
Patch ─► PatchParser ─► Hunk 清单(H1..Hn)
                          │
        Source/Target ─► CodeContextProvider ─┐
                          │                  │
                          ▼                  ▼
                   EvidenceExtractor（阶段A符号 + 阶段B候选证据）
                          │
                          ▼
        DependencyAnalyzer（阶段C LLM 判定；阶段D 按需补证，重试有上限）
                          │
                          ▼
        DependencyValidator（结构 + 证据 + 目标版本状态，独立校验）
                          │
                          ▼
        DependencyGraph（仅 STRONG 成边）─► GroupingEngine（连通分量 + 条件提升）
                          │
                          ▼
        PartitionValidator（完整性 + 强依赖 + 未解决项）─► 分组 JSON
                          │
                          ▼
        PortGPTAdapter ─► 分组级移植任务 + 可追溯记录 ─► 评估
```

---

## 4. 模块与接口

| 模块 | 职责 | 主要接口 |
|---|---|---|
| `PatchParser` | 解析标准 unified diff，分配稳定 Hunk ID | `parse(text)`、`parse_document(text, patch_id)`、`parse_file(path)` |
| `CodeContextProvider` | 取源/目标版本代码上下文与符号证据 | `get_context(hunk, src_rev, tgt_rev)`、`get_contexts(...)`、`additional_evidence(request)` |
| `SourceAccess`（`LocalTreeAccess` / `GitRepoAccess` / `NullAccess`） | 只读代码访问抽象 | `available()`、`supports_revision(rev)`、`read_file(rev, path)`、`symbol_hits(rev, symbol, max_results)` |
| `EvidenceExtractor` | 阶段 A 符号定义/引用/签名变化；阶段 B 候选对与机器证据 | `extract_symbols(hunk)`、`enrich_hunks(hunks)`、`signature_changes(hunk)`、`extract(hunks, context)` |
| `DependencyAnalyzer` | 阶段 C/D：调用 LLM、校验、修复重试、按需补证 | `analyze(hunks, evidence, *, patch_id, source_revision, target_revision, contexts, existing_analysis)` |
| `LLMClient`（`OpenAICompatibleClient` / `MockLLMClient` / `HeuristicMockClient`） | 统一模型接口，可配置可 Mock | `complete(messages, ...)`、`analyze_dependencies(payload)` |
| `DependencyValidator` | 独立校验依赖记录（Schema、证据、目标状态、重复合并） | `validate(dependencies, hunk_ids, contexts=...)` |
| `DependencyGraph` | 无向图、连通分量、跨组边 | `components()`、`cross_group_edges(group_of)`、`to_dict()` |
| `GroupingEngine` | 确定性分组、条件依赖提升策略、显式 resolution | `group(hunk_ids, dependencies, resolutions=...)` |
| `PartitionValidator` | 完整性、强依赖同组、未解决项报告 | `validate(hunk_ids, groups, dependencies, ...)`（§10.4 文档） |
| `PortGPTAdapter` | 分组 → 下游任务、可追溯记录 | `build_tasks(...)`、`build_traceability_record(...)`、`append_traceability_record(...)` |
| `Evaluator` | §16.2 指标与数据集评估 | `dependency_metrics`、`grouping_metrics`、`backport_metrics`、`evaluate_dataset` |
| `HunkGroupingPipeline` | 端到端编排（新增） | `run(PipelineInputs)` |

### 4.1 关键数据结构

* **Hunk**：`hunk_id`、`file_path`/`target_file_path`、`old_start`/`old_count`/`new_start`/
  `new_count`、`header`、`section`、`diff_text`、`added_lines`/`removed_lines`/`context_lines`、
  `line_map`、`parse_warnings`、`symbols`、`evidence`。
* **依赖记录**：`dependency_id`、`source_hunk`、`target_hunk`、`category`、`strength`、
  `evidence[]`、`separation_risk`、`target_version_status`、`reasoning_summary`、
  `needs_more_evidence`。
* **分析结果**：`patch_id`、`status`、`hunk_ids`、`dependencies`、`unresolved_questions`、
  `metadata`（模型、Token、耗时、批次）、`rejected_records`、`call_records`。
* **分组文档**：`patch_id`、`status`、`groups[]`（`group_id`/`hunk_ids`/`group_reason`/
  `dependency_ids`/`other_dependency_ids`/`external_dependency_ids`）、
  `cross_group_dependencies`、`unresolved_questions`、
  `validation{all_hunks_assigned_once, strong_dependencies_within_groups,
  unresolved_dependencies_reported}`、`metadata`、`errors`、`warnings`、`checks`。

枚举取值：

* `category` ∈ `DEFINITION_USE / INTERFACE_SIGNATURE / CALL_GRAPH / SHARED_STATE /
  CONTROL_FLOW / SEMANTIC_INVARIANT / CONTEXT_DEPENDENCY`
* `strength` ∈ `STRONG / CONDITIONAL / WEAK / NONE`
* `target_version_status` ∈ `present / absent / partially_present / unknown`
* `retrieval_status` ∈ `complete / partial / unavailable / failed`

---

## 5. 安装与运行

要求 Python ≥ 3.11（本机验证 3.12.14）。运行期**无第三方依赖**；`PyYAML` 可选
（未安装时使用内置受限 YAML 读取器，测试断言两者对随附配置的解析结果一致）。

```powershell
# 方式一：直接以源码目录运行（无需安装）
$env:PYTHONPATH = "src"
python -m patch_hunk_agent.cli --version

# 方式二：可编辑安装（提供 patch-hunk-agent 命令）
python -m pip install -e .
patch-hunk-agent --version

# 测试依赖
python -m pip install -e ".[dev]"     # pytest, PyYAML
```

### 5.1 命令行

```bash
# 分析与分组（实现文档 §14.1）
python -m patch_hunk_agent.cli group \
  --patch examples/input/sample.diff \
  --patch-id sample \
  --source-revision SRC --target-revision TGT \
  --source-tree examples/source --target-tree examples/target \
  --output results/sample.grouping.json

# 只读 git 仓库模式（源/目标版本是真实提交）
python -m patch_hunk_agent.cli group --patch fix.patch --repo /path/to/repo \
  --source-revision v1.0 --target-revision v0.9 \
  --provider openai --model gpt-4o-mini --output results/fix.grouping.json

# 查看 Hunk 清单 / 符号 / 检索状态 / 候选证据（不调用 LLM）
python -m patch_hunk_agent.cli inspect --patch tests/fixtures/simple_dependency.diff \
  --source-tree tests/fixtures/trees/source --target-tree tests/fixtures/trees/target

# 重新校验已有分组结果（不信任 LLM 输出，独立重算结构与图约束）
python -m patch_hunk_agent.cli validate --grouping results/sample.grouping.json \
  --analysis results/sample.analysis.json --output results/sample.validation.json

# 离线跑通全部内置样例（mock-heuristic，无网络）
python -m patch_hunk_agent.cli demo --output examples/output

# 列出可用 provider
python -m patch_hunk_agent.cli providers
```

常用参数：`--provider {mock|mock-heuristic|openai}`、`--model`、`--config`、
`--resolution D1=promote|keep|drop`（用人工/目标版本检查结论覆盖条件依赖）、
`--tasks-output`（交给 PortGPT 的任务 JSON）、`--analysis-output`、
`--record`（JSONL 运行记录）、`--no-context`（禁用代码检索）、`--print-json`。

退出码：`0` 成功；`1` 分组校验失败（`status=failed`）；`2` 参数 / 文件 / 配置错误。

### 5.2 通用驱动脚本（任何 patch 都能跑）

不绑定某个 CVE 或某台机器，patch 特定信息全部走参数：

```bash
python scripts/run_grouping.py --patch <任意.diff> [选项]

# 离线管线自检（无网络）
python scripts/run_grouping.py --patch tests/fixtures/simple_dependency.diff \
    --provider mock-heuristic --grouping-mode components --out results/_smoke

# 真实模型 + PortGPT 兼容产物（凭据来自 .env）
python scripts/run_grouping.py --patch /path/fix.patch \
    --portgpt-root /path/portgpt-src-modified \
    --portgpt-config /path/portgpt-src-modified/config.yml \
    --out results/<name>

# 同一输入跑 N 次看方差；有参照数据集时自动打分
python scripts/run_grouping.py --patch fix.patch --repeat 3 --dataset ref/dataset.json
```

每次运行都会写出 `manifest.json`：补丁路径与 sha256、提示词哈希、模型标识、上下文
（repo/源/目标版本）、每组组数/组大小/token/耗时、`execution_plan.order` 与
`constraint_satisfaction`、墙钟时间——用来解释分数变化、也用来复现。

`--portgpt-config` 会复用 PortGPT 自己的端点/模型/密钥（仅当环境变量未设置时）与其
`patch_dataset_dir`，所以产出的 `hunk_groups.json` 与 `merged_hunks/` 直接落在 PortGPT
自动识别的目录里；不给 `--portgpt-config` 时全部写到 `--out`。

### 5.3 配置与密钥

各文件各管什么：

| 文件 | 管什么 | 是否放密钥 |
|---|---|---|
| `configs/default.yaml` | 阈值、上下文预算、批次大小、提升策略、校验策略、输出目录 | 否 |
| `configs/models.yaml` | 逻辑 provider 定义（`mock` / `mock-heuristic` / `openai`：`base_url`、`model`、`api_key_env`） | 可放（不推荐，见下） |
| `<repo>/.env`（从 `.env.example` 复制） | 本次运行/本机的 provider、端点、模型、**API Key** | **推荐放这里**（已 gitignore） |
| 单次运行的配置，如 `results/cve-2025-48649/config.run.yaml` | 只覆盖阈值/批次等，不含密钥 | 否 |
| PortGPT 自己的 `portgpt-src-*/config.yml` | PortGPT 的工程目录、提交号、Sanitizer、以及它自己的 `openai_key` | 那是 PortGPT 的事 |

`.env` **会被自动加载**（`config.load_env_file`：先看当前目录，再看仓库根目录；真实环境变量优先于文件）。
填这四行即可用真实模型：

```ini
PATCH_HUNK_AGENT_PROVIDER=openai
PATCH_HUNK_AGENT_BASE_URL=https://<你的兼容端点>/v1
PATCH_HUNK_AGENT_MODEL=gpt-4o
PATCH_HUNK_AGENT_API_KEY=<你的 key>
```

密钥取值优先级：`PATCH_HUNK_AGENT_API_KEY`（环境变量或 `.env`）→ `configs/models.yaml` 里的
`api_key` → `api_key_env` 指向的环境变量（默认 `OPENAI_API_KEY`）。

其它环境变量：`PATCH_HUNK_AGENT_BASE_URL`、`PATCH_HUNK_AGENT_MODEL`、`PATCH_HUNK_AGENT_CONFIG`、
`PATCH_HUNK_AGENT_LOG_LEVEL`；YAML 值支持 `${VAR}` 与 `${VAR:-default}`；配置文件与
`configs/models.yaml` 在缺省路径不存在时会回退到仓库根目录，所以在任意工作目录下运行都能加载。

**密钥不写入代码、不写入测试、也不写入运行记录**（记录写入前统一脱敏 `redact()`，有单元测试覆盖）；
`.env` 与 `results/*.json` 均在 `.gitignore` 中。

---

## 6. 测试

```powershell
$env:PYTHONPATH = "src"
python -m pytest -q
python -m ruff check src tests scripts
```

最近一次实际结果（Windows / Python 3.12.14，仓库根目录直接执行、无需设置 `PYTHONPATH`）：

```text
$ python -m pytest -q
199 passed, 2 skipped in 6.92s

$ python -m ruff check src tests scripts
All checks passed!
```

另外已手工验证：`group --repo <真实 git 仓库> --source-revision <commit>` 能从真实提交读取
上下文（`retrieval_status=complete`）、正确判定目标版本状态并输出分组 JSON。

2 个 skip 来自 §15.2 的真实回归样例（CVE-49649 H11、H14/H15、H6/H7）：本仓库没有这些真实
Patch 与版本代码，因此**不编造期望值**，harness 按 `tests/regression/README.md` 约定的格式
待录入，录入后自动纳入回归。

覆盖重点（对应 §15）：

* **解析**：稳定 ID、多文件、无计数头、`--- ` 与"以 `--` 开头的删除行"歧义、CRLF、
  `\ No newline at end of file`、非法头报错、计数不符告警但**不丢弃 Hunk**。
* **证据**：函数/宏/类型定义识别、`return x;` 不误判为定义、注释与字符串不参与扫描、
  签名变化（前后参数列表）、候选对机器信号、目标版本状态对信号强弱的影响。
* **检索**：只读、路径穿越拒绝、单次读取量上限、符号查询预算、无访问时
  `retrieval_status=unavailable` 且状态为 `unknown`（**不把检索失败当作符号不存在**）、
  真实临时 git 仓库上的 `read_file`/`git grep` 只读访问。
* **分析**：JSON 围栏/尾逗号/单引号/Python 字面量修复、非法 Hunk ID 与非法强度拒收并留痕、
  修复次数上限（`max_format_retries+1` 次后停止）、Token/耗时/原始响应记录。
* **校验**：STRONG 需可定位证据与分离风险、目标已满足→降级 CONDITIONAL、证据待补→降级并提问、
  重复依赖合并取弱、与检索证据矛盾时告警。
* **分组**：确定性（打乱输入顺序、多次运行结果一致）、孤立 Hunk 自成一组、
  不因同文件/相邻而合并、未知 Hunk ID 记录而非崩溃、强依赖跨组自动合并修复。
* **端到端**：Test A–E、每个 Hunk 恰好分配一次、输出 JSON 通过 Schema 校验、
  Patch 与源码树**未被修改**、任务 JSON 明确声明不是已验证的移植结果。

---

## 7. 五个基准场景的实际结果（§15.1）

`python -m patch_hunk_agent.cli demo --output examples/output`（离线 `mock-heuristic`）：

| 场景 | 识别到的依赖 | 强度 | 目标版本状态 | 分组 | status |
|---|---|---|---|---|---|
| Test A `simple_dependency`（H1 定义 `clamp_value`，H2 调用） | H1→H2 `DEFINITION_USE` | STRONG | absent | `G1=[H1,H2]` | complete |
| Test B `signature_dependency`（H2 改 `process()` 签名，H1 调用点） | H2→H1 `INTERFACE_SIGNATURE` | STRONG | absent | `G1=[H1,H2]` | complete |
| Test C `independent_hunks`（同文件、互不相关） | 无 | — | — | `G1=[H1]`, `G2=[H2]` | complete |
| Test D `target_already_satisfies`（目标版本已有 `guard_limit`） | H1→H2 `DEFINITION_USE` | CONDITIONAL | present | `G1=[H1]`, `G2=[H2]` | complete |
| Test E `insufficient_evidence`（无源/目标代码） | H1→H2 `DEFINITION_USE` | CONDITIONAL | unknown | `G1=[H1]`, `G2=[H2]` | partial |

* Test D 的分组理由会显式写出"目标版本已满足该前置条件、因此未提升为分组边"，
  并在 `metadata.grouping.not_promoted` 记录原因。
* Test E 把待确认问题写入 `unresolved_questions`、标记受影响分组、`status=partial`，
  不伪造强依赖证据。
* 示例 Patch `examples/input/sample.diff`（4 Hunk / 3 文件）得到 `G1=[H1,H2]`（定义—使用）
  与 `G2=[H3,H4]`（函数签名 + 调用点），两组互不合并。

> 上表是**离线启发式 Mock** 的结果，用于证明流程与校验正确；它不是模型能力评测。
> 真实模型评测请使用第 10 节的流程。

---

## 8. 分组结果示例（节选）

```json
{
  "patch_id": "sample",
  "status": "complete",
  "groups": [
    {
      "group_id": "G1",
      "hunk_ids": ["H1", "H2"],
      "group_reason": "H1, H2 must be ported together because of verified STRONG dependency(ies): D1 (DEFINITION_USE H1->H2): ...",
      "dependency_ids": ["D1"],
      "other_dependency_ids": [],
      "external_dependency_ids": []
    }
  ],
  "cross_group_dependencies": [],
  "unresolved_questions": [],
  "validation": {
    "all_hunks_assigned_once": true,
    "strong_dependencies_within_groups": true,
    "unresolved_dependencies_reported": true
  }
}
```

---

## 9. 与 PortGPT 的集成（已实现并验证）

### 9.1 PortGPT 实际消费的契约

PortGPT（`portgpt-src-modified`）已经不是"一个 hunk 一次"，而是**一个 merged hunk 一次**。
它通过 `tools/utils.py` 里的 `group_patch()` / `dump_merged_hunks()` 消费一份分组规格文件：

```json
{
  "groups": [
    {"id": 1, "name": "…", "hunks": [0, 1, 6], "concern": "…", "why_together": "…"}
  ],
  "dependencies": [{"from_hunk": 3, "to_hunk": 6, "why": "…"}],
  "notes": "…", "_model": "…", "_patch": "…"
}
```

其中 `hunks` 是 **0 基下标**，由 PortGPT 自己的 `split_patch_indexed()` 按"文件顺序 + `@@` 顺序"
编号。取文件路径的顺序为：`config.yml` 的 `hunk_groups:` 配置 → 否则自动匹配
`<patch_dataset_dir>/*hunk*groups*.json`。

**这就是唯一会静默出错的地方**：如果本 Agent 的 `H<n>` 与 PortGPT 的 `idx` 错位，分组会把
错误的 hunk 合成一组，而且不会报错。所以桥接模块 `portgpt_spec.py` 不猜编号：

1. 直接运行 **PortGPT 自己的** `split_patch_indexed`（能 import 就 import，缺 Levenshtein /
   langchain 等依赖时从 `tools/utils.py` 里 AST 提取该函数执行，仍是它的代码）；
2. 与本 Agent 的 Hunk 清单逐项比对 **数量 / 顺序 / 文件路径 / `@@` 头 / 正文行数**；
3. 只有 `aligned = true` 时才按该映射导出 spec，并校验 spec 是"每个 hunk 恰好出现一次"的完整划分。

### 9.2 命令

```bash
# 分析与分组，并直接产出 PortGPT 能读的 spec（默认 results/<patch_id>.hunk_groups.json）
python -m patch_hunk_agent.cli group \
  --patch  /path/to/48649-AOSP16-fix.patch \
  --portgpt-root /path/to/portgpt-src-modified \
  --portgpt-spec-output /path/to/patch_dataset/hunk_groups.json \
  --merged-hunks-dir /path/to/patch_dataset/merged_hunks \
  --portgpt-strict \
  --output results/48649.grouping.json

# 只有分组 JSON 时，单独导出 / 校验 spec
python -m patch_hunk_agent.cli portgpt-spec --patch P --portgpt-root R \
  --grouping results/48649.grouping.json --output hunk_groups.json --strict

# 同时产出 merged hunk 补丁文件（PortGPT 可直接当 session 输入用）
python -m patch_hunk_agent.cli portgpt-spec --patch P --portgpt-root R \
  --grouping results/48649.grouping.json --output hunk_groups.json \
  --merged-hunks-dir /path/to/patch_dataset/merged_hunks --strict

# 校验一份已有的 spec（例如别人/别的模型产出的 hunk_groups.json）
python -m patch_hunk_agent.cli portgpt-spec --patch P --portgpt-root R \
  --check-spec hunk_groups.json --strict

# 把已有的 spec 反向导入本 Agent 的分组格式，便于用本 Agent 的校验器/评估器复查
python -m patch_hunk_agent.cli portgpt-spec --patch P --portgpt-root R \
  --from-spec hunk_groups.json --grouping-output results/imported.grouping.json
```

导出是**保措辞的**：从外部 spec 导入的分组会原样保留 `name` / `concern` / `why_together`，
只有本 Agent 自己分组时才生成这些文字。

### 9.2.1 直接产出 grouped / merged hunk 文件

`--merged-hunks-dir DIR` 会按 PortGPT 的布局写出"一组一个补丁文件"：

```text
DIR/
├── original/
│   ├── merged-hunk-00.patch   # 第 1 组的若干原始 hunk 拼接成的整体补丁
│   ├── merged-hunk-01.patch
│   └── index.json             # file/gid/name/hunks/concern/why/files
├── index.md                   # 人读总览
└── ported/                    # 预留：移植结果（由 PortGPT 或你写入）
```

组序与文件名编号按"组内最小 hunk 下标"排序、`files` 逐成员列出（同文件重复），都与
PortGPT 自己的 `group_patch` + `dump_merged_hunks` 一致。给定同一份分组，本 Agent 写出的
`merged-hunk-NN.patch`、`original/index.json`、`index.md` 与 PortGPT 自己写出的**逐字节相同**
（在 `48649-AOSP16-fix.patch` 上 9/9 文件 + 2 个索引文件全部一致，见 9.3 与
`tests/test_portgpt_spec.py::test_merged_hunk_files_are_byte_identical_to_portgpts_own_dump`）。

该目录可直接作为 PortGPT 的输入（`config.yml` 里的 `merged_hunks_dir:`，或放到
`<patch_dataset_dir>/merged_hunks/`）：PortGPT 的 `load_merged_hunk_files` 会按文件名顺序
逐个当作独立 session 的输入。这样等于让本 Agent 决定"分组 + 合并成补丁"，
PortGPT 只负责移植。

### 9.3 在真实 Patch 上验证过的结果

对 `Desktop/backport_patch/48649-AOSP16-fix.patch`（33 hunk / 19 文件）与
`portgpt-src-modified` 实际执行：

| 检查 | 结果 |
|---|---|
| 本 Agent `H1..H33` vs PortGPT `idx 0..32` | **对齐，0 处差异**（数量/顺序/文件/`@@` 头/正文行数） |
| 现有 `hunk_groups.json`（9 组，DeepSeek 产出）覆盖 | 9 组、33/33 hunk 恰好一次、无越界/重复 |
| 导入→导出往返 | `name`/`concern`/`why_together`/`hunks`/`dependencies` 全部无损 |
| PortGPT `group_patch()` 消费导出的 spec | 9 个 merged hunk，合并文本与原 hunk 文本**逐字节一致**，33 个 hunk 恰好覆盖一次 |
| `dump_merged_hunks()` | 正常写出 9 个 `merged-hunk-NN.patch` + `index.md` + `index.json` |
| 本 Agent 直接写 merged hunk 文件 vs PortGPT 自己写 | `merged-hunk-00..08.patch`、`original/index.json`、`index.md` **全部逐字节相同**（9/9 + 2/2） |

对应测试：`tests/test_portgpt_spec.py`（22 项；其中两项在检测到真实 PortGPT 目录时执行，
用 `PORTGPT_ROOT` / `PORTGPT_PATCH` 指定，未提供时 skip）。

### 9.4 分工与边界

* 本 Agent **不改动 PortGPT 的任何代码**，只产出它已有的输入格式；也不需要 PortGPT 的
  `Levenshtein`/`langchain` 依赖即可做对齐校验。
* 若要按分组逐个移植，把导出的 `hunk_groups.json` 放进 `patch_dataset_dir`
  （或在 `config.yml` 里写 `hunk_groups: <文件名>`），再跑 `python backporting.py -c config.yml`
  即可；`--list-groups` 可先打印分组而不调用 LLM。
* 本 Agent 另外提供的 `portgpt.group_task/v1`（`--tasks-output`）是**更详细**的任务包
  （含依赖证据、未解决项、`caveats`），供需要更多上下文的下游流程使用。
* 移植后用 `PortGPTAdapter.build_traceability_record(...)` +
  `append_traceability_record(path, record)` 记录 `patch_id / group_id / hunk_ids /
  PortGPT 输入 / 生成的 diff / 编译与测试结果 / 失败原因`，与原始分组决策一一对应。
* **安全提醒**：`portgpt-src-*/config.yml` 里目前有明文 `openai_key`（形如 `sk-gr-…`），
  建议改从环境变量读取；本 Agent 不读取也不复制该值。

### 9.5 后续可选增强

* 复用 PortGPT 已有的 Patch 解析/符号检索结果：把它们的依赖记录传入
  `DependencyAnalyzer.analyze(..., existing_analysis=[...])`；或实现 `SourceAccess`
  （`read_file` / `symbol_hits` / `supports_revision`）后复用 `CodeContextProvider`。
* 在 PortGPT 进程内直接调用 `HunkGroupingPipeline.run(...)`，把 `groups` 作为移植任务边界。

---

## 10. 真实 Backport 对照实验怎么做

### 10.1 两组设置

* **Baseline**：原 PortGPT，按原有输入方式移植整份 Patch。
* **Treatment**：先 `group` 得到分组，再按 `tasks.json` 的分组逐个移植
  （同组 Hunk 一次性给出）。

保持模型配置、输入代码版本、编译/测试环境与成功判定标准一致。

### 10.2 参考数据集格式

见 `examples/evaluation/README.md`：每个 case 需要人工审阅得到的
`reference_dependencies`（含 `category`/`strength`）、`reference_groups`，以及实际移植的
`outcome`（`backport_success`/`compile_pass`/`test_pass`/`tokens`/`retries`/`runtime_s`）。

### 10.3 运行与指标

```bash
python scripts/run_evaluation.py \
  --dataset   datasets/patches.json \
  --baseline  predictions/baseline.json \
  --treatment predictions/grouped.json \
  --output    results/evaluation.json
```

输出包含每组的 `dependency_precision/recall/f1`、`strong_miss_rate`、`over_grouping_rate`、
`under_grouping_rate`、`co_membership_recall`、`backport_success_rate`、`compile_pass_rate`、
`test_pass_rate`、`average_tokens/retries/runtime_s`，以及 Baseline↔Treatment 的 `delta` 对比。

### 10.4 消融实验

同一数据集上切换配置即可：`--no-context`（不用目标版本上下文）、关闭阶段 B 机器信号、
只保留 `DEFINITION_USE` + `INTERFACE_SIGNATURE`（`grouping.promoted_categories`）、
`grouping.promote_conditional: false`、用 `validate` 子命令对比"跳过分组校验"的差异；
不同模型通过 `--provider/--model` 切换。

> 这些实验**尚未执行**：本仓库没有真实 Patch 数据集与移植环境，因此不做无依据的结论。

---

## 11. 尚未实现 / 已知限制

| 项 | 现状 | 说明 |
|---|---|---|
| 真实 Patch 回归样例（CVE-49649 H11、H14/H15、H6/H7） | **未实现**（harness 就绪、测试 skip） | §15.2 要求基于真实 Patch 与源/目标代码；本仓库无此数据，不编造期望值 |
| 真实 Backport 对照实验与消融实验 | **未执行** | §16 需要真实移植环境；工具与指标计算已就绪 |
| 真实 LLM 效果验证 | **未执行** | 代码支持任意 OpenAI 兼容端点，但本轮未联网调用真实模型；仓库内所有断言基于 Mock |
| C/C++ AST（tree-sitter / clang）级符号分析 | 未实现 | 当前为词法级启发式（正则 + 括号/作用域启发），已在文档与输出中标注为"机器证据" |
| 目标版本代码漂移对齐 | 部分实现 | 优先按 `section` 函数名在目标文件定位，其次按行号窗口；不做 diff 位置重映射 |
| 语义依赖（`SEMANTIC_INVARIANT`）机器信号 | 依赖 LLM | 机器只提供"无信号也不丢弃"的候选对，语义判断交由 LLM，且必须有证据才能 STRONG |
| 多语言支持 | 以 C 系为主 | Python/JS/Java 关键字已覆盖，但定义识别规则仍偏 C 风格 |
| 大规模仓库检索性能 | 有限 | 本地目录检索按扩展名遍历并带缓存；超大仓库建议注入已有检索能力（§9.2） |
| 人工审阅界面 | 未实现 | 目前通过 `--resolution` 与 JSON 输出人工干预 |

---

## 12. 与实现文档的差异及原因

1. **目录布局**：文档 §11 模板的外层是 `patch-hunk-agent/`；本仓库该目录即仓库根，
   因此 `pyproject.toml`/`configs`/`tests` 直接放根目录，包放 `src/patch_hunk_agent/`。
2. **新增 `pipeline.py`**：文档未列该模块。端到端编排若塞进 `cli.py` 会难以独立测试，
   故独立成模块，`cli.py` 只负责参数与输出。
3. **Hunk 边界按计数驱动解析**：以 `@@` 声明的行数切分，而不是"下一行像表头就结束"。
   原因：删除行 `-- legacy_call();` 渲染为 `--- legacy_call();`，与文件头完全同形；
   只有计数驱动才能正确切分（有专门测试）。
4. **`analyze_dependencies` 返回 dict**：保持文档签名，返回体同时携带
   `parsed`/`raw_text`/`usage`/`error`/`repairs`，便于记录 Token、原始响应与失败原因。
5. **新增 `machine_hint`（仅用作提示与离线 Mock）**：候选对带
   `NONE/WEAK_HINT/CONDITIONAL_HINT/STRONG_HINT`，用于批次排序、给 LLM 的输入提示与离线
   `mock-heuristic`；**不参与最终判定**。最终强度只来自 LLM 输出 + 独立校验器。
6. **CONDITIONAL 提升为 STRONG 的策略**：文档 §9.2 第 6 步只给原则，这里落成可配置规则：
   类别在 `promoted_categories` 内、`target_version_status=absent`、证据可定位、
   `needs_more_evidence=false`；或调用方显式 `--resolution D#=promote`。
   未提升时写入 `not_promoted` 并给出原因。
7. **STRONG 的必要条件**：必须有可定位证据与分离风险描述，且目标版本状态不是 `present`，
   否则降级为 CONDITIONAL/WEAK 并留痕（§6.1/§18.1 的落地）。
8. **`over_grouping_rate` 定义**：不必要合并对数 / 预测合并对数（与 `under_grouping_rate` 对称）；
   文档 §16.2 未给出公式。
9. **`status` 聚合规则**：分组文档 `status` 取 PartitionValidator 结果；当分析状态非 `complete`
   时下调为 `partial`；`failed` 仅用于结构错误或强依赖跨组未修复。
10. **内置最小 YAML 读取器**：为保证零依赖可运行；测试断言其解析结果与 PyYAML 一致。
11. **阶段 A 双路径**：默认走确定性启发式（`evidence.mode=heuristic`）；`evidence_extraction.txt`
    与 LLM 路径保留以备扩展。当前 CLI 与测试使用启发式路径，不把未验证的 LLM 抽取当作事实。
12. **无信号候选对也提交给 LLM**（`include_no_signal_pairs: true`），避免机器规则漏掉纯语义依赖；
    但这会增加调用量，可通过配置或 `max_candidate_pairs` 预算关闭/限制。

---

## 13. MVP 验收清单（§19）

- [x] 能解析标准 unified diff（多文件、新增/删除文件、重命名/复制、CRLF、无末行换行）
- [x] 每个 Hunk 都有稳定且唯一的 ID（`H1..Hn`，按文档顺序，与分组无关地保持不变）
- [x] 能识别和表示多种跨 Hunk 依赖（7 类 category 全支持，机器信号覆盖 6 类）
- [x] 每条依赖都有类型、强度和证据字段（证据含符号/文件/行号，可定位）
- [x] 能将强依赖 Hunk 放入同一组（连通分量；跨组强依赖自动合并修复）
- [x] 能保留无关 Hunk 的独立分组（同文件、相邻也不合并）
- [x] 能检查目标版本是否已经满足相关前置条件（含签名参数比对）
- [x] 能报告无法确认的依赖（`unresolved_questions` + `status=partial`）
- [x] 能检测分组中的强依赖跨组问题（`cross_group_dependencies` + `status=failed`）
- [x] 所有 Hunk 均被分配且只分配一次（PartitionValidator 独立校验）
- [x] 相同依赖图能生成确定性的分组结果（顺序无关、多次运行一致）
- [x] 提供可运行的命令行接口和单元测试（`group/inspect/validate/evaluate/demo/providers`）
- [x] 支持 Mock LLM 测试（脚本化 Mock + 离线启发式 Mock）
- [x] 不会直接修改原始 Patch 或目标仓库（只读访问 + 测试断言内容不变）
- [x] 能输出机器可读的 JSON（分组、分析、任务、校验报告、运行记录）
- [x] 为后续 PortGPT 集成保留明确的接口（`PortGPTAdapter` + `existing_analysis` + `SourceAccess`）
- [x] 记录运行时间、Token、错误和分组结果（JSONL 运行记录 + 分析 `metadata`）

以下文档目标**不**由本实现证明，需要真实移植实验（见第 10 节）：
能否提高 Backport 成功率、编译通过率与测试通过率，以及各组件（符号证据、目标版本上下文、
分组校验）的实际增益。
