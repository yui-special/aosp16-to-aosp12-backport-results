# 交接说明：Patch Hunk Dependency Grouping Agent

> 这份文档写给**没有参与过前面讨论**的读者（另一个对话/另一位工程师）。
> 读完它应当可以：理解这个 agent 做什么、怎么跑起来、输出是什么、哪些结论已验证、
> 哪些还没验证、以及下一步怎么做真实对照实验。

---

## 1. 它是什么，不做什么

**做什么**：在 PortGPT 执行 Patch Backport **之前**，分析原始 Patch 里 Hunk 之间的依赖关系，
把它们组织成若干**工作包（work package）**，并给出可执行的跨包协调信息，最后以 JSON +
PortGPT 可直接消费的补丁文件交付。

**不做什么**（这是硬边界）：

- 不生成、不修改、不套用任何 backport 代码；
- 不修改 PortGPT 的任何代码、不修改被分析的仓库；
- 不做编译/测试/PoC 判定（那是 PortGPT 与评估模块的事）。

原始需求来自一份实现文档（*Patch Hunk Dependency Grouping Agent 实现文档 v1.0*，
下称"实现文档"）。本实现遵循它的大部分设计，但对**分组算法**做了有意的偏离，原因见第 5 节。

---

## 2. 目录结构与关键文件

```text
patch-hunk-agent/
├── README.md                  # 原项目 README（含 CLI、配置、PortGPT 集成、评估方法）
├── HANDOFF.md                 # 本文件
├── pyproject.toml             # 打包 / pytest / ruff 配置
├── .env.example               # 环境变量模板（真实密钥放 .env，不要提交）
├── configs/
│   ├── default.yaml           # 阈值、上下文预算、分组策略、校验策略
│   └── models.yaml            # 逻辑 provider 定义（mock / mock-heuristic / openai）
├── prompts/
│   ├── dependency_analysis.txt   # 逐对依赖判定的 System Prompt
│   ├── evidence_extraction.txt   # 可选：LLM 版阶段 A 符号抽取
│   └── global_partition.txt      # 全局工作包划分的 System Prompt（关键）
├── src/patch_hunk_agent/
│   ├── patch_parser.py           # unified diff → 稳定 Hunk 清单（H1..Hn，计数驱动切分）
│   ├── context_provider.py       # 源/目标版本代码上下文（git / 本地目录 / 无访问）
│   ├── evidence_extractor.py     # 阶段A符号 + 阶段B候选依赖机器证据
│   ├── dependency_analyzer.py    # 阶段C/D：LLM 逐对判依赖、修复重试、按需补证
│   ├── dependency_validator.py   # 依赖记录独立校验与降级
│   ├── grouping_engine.py        # components 模式：强依赖连通分量（实现文档原算法）
│   ├── global_grouper.py         # global 模式：模型全局划分 + 图校验 + 有上限修复（默认）
│   ├── dependency_graph.py       # 无向图与连通分量
│   ├── partition_validator.py    # 分组完整性/强依赖/未解决项校验
│   ├── portgpt_spec.py           # PortGPT 规格 + merged hunk 文件的读写与对齐核验
│   ├── portgpt_adapter.py        # 更详细的任务包 + 可追溯记录
│   ├── evaluator.py              # 指标（分组代理指标 + 回移产出指标）
│   ├── pipeline.py               # 端到端编排（唯一把各阶段串起来的地方）
│   ├── cli.py                    # 命令行：group / inspect / validate / evaluate / portgpt-spec / demo / providers
│   ├── config.py                 # YAML + ${ENV} + .env 加载 + provider 解析
│   ├── schemas.py                # 全部数据契约与校验
│   └── llm/                      # 统一模型接口：base / provider(OpenAI 兼容) / mock(脚本化+离线启发式)
├── scripts/
│   ├── run_grouping.py           # 【通用驱动】任意 patch → 分组 + PortGPT 产物 + manifest
│   ├── portgpt_compare.py        # 【对照实验】baseline vs treatment 的执行与判定
│   ├── run_evaluation.py         # 数据集评分对比
│   └── score_grouping.py         # 用一份 result.json 对一个数据集打分
├── tests/                        # 19 个测试文件（251 项断言，全部可通过）
└── examples/                     # 示例 patch 与源/目标代码
```

注意：**`results/` 与 `.env` 没有随本次交接复制**（前者是本机跑出的产物，后者含密钥）。

---

## 3. 快速开始

### 3.1 安装

运行期**无第三方依赖**（`PyYAML` 可选：没有它时用内置的受限 YAML 读取器，已用测试证明两者解析一致）。

```powershell
cd <这个文件夹>
$env:PYTHONPATH = "src"                     # 或以源码方式直接运行
python -m pytest -q                          # 251 passed（需要 pytest）
python -m pip install -e ".[dev]"            # 或正式安装（提供 patch-hunk-agent 命令）
```

### 3.2 离线自检（不需要网络与密钥）

```powershell
python scripts/run_grouping.py --patch tests\fixtures\simple_dependency.diff `
  --provider mock-heuristic --grouping-mode components --out results\_smoke
python -m patch_hunk_agent.cli demo --output examples\output   # 跑通 5 个内置场景
```

`demo` 会输出实现文档 §15.1 的五个基准场景（定义—使用、函数签名、无关 Hunk、目标版本已具备、
证据不足）的实际结果。

### 3.3 真实模型（需要密钥）

```powershell
# 1) 在仓库根创建 .env（可从 .env.example 复制）
#    PATCH_HUNK_AGENT_PROVIDER=openai          或任意名字（配合下面的端点即可）
#    PATCH_HUNK_AGENT_BASE_URL=<兼容 /chat/completions 的端点>
#    PATCH_HUNK_AGENT_MODEL=<模型名>
#    PATCH_HUNK_AGENT_API_KEY=<你的 key>
#    PATCH_HUNK_AGENT_REASONING_EFFORT=none    ← 推理型模型必加，见第 4.3 节

# 2) 跑任意补丁
$env:PATCH_HUNK_AGENT_REASONING_EFFORT = "none"
python scripts/run_grouping.py --patch <任意.diff> --out results\<name>
```

`.env` 会被自动加载（先看当前目录，再看仓库根），真实环境变量优先于文件。

---

## 4. 配置与密钥（三个必须知道的点）

### 4.1 各文件分工

| 文件 | 管什么 | 是否放密钥 |
|---|---|---|
| `configs/default.yaml` | 阈值、上下文预算、分批大小、分组策略、校验策略 | 否 |
| `configs/models.yaml` | 逻辑 provider 定义（`base_url`/`model`/`api_key_env`） | 可以但不推荐（会进 git） |
| `<repo>/.env` | 本机 provider / 端点 / 模型 / **API Key** | **推荐**（已 gitignore） |
| PortGPT 自己的 `config.yml` | 工程目录、提交号、Sanitizer、它自己的 key | 那是 PortGPT 的事 |

密钥优先级：`PATCH_HUNK_AGENT_API_KEY`（环境变量或 `.env`）→ `models.yaml` 的 `api_key`
→ `api_key_env` 指向的环境变量（默认 `OPENAI_API_KEY`）。
运行记录写入前统一脱敏，测试断言仓库内配置文件不含密钥。

### 4.2 provider 名字是"逻辑名"

`PATCH_HUNK_AGENT_PROVIDER` 只匹配 `configs/models.yaml` 里的键（`mock` / `mock-heuristic` / `openai`）。
写别的名字也可以，但**必须同时给出 `PATCH_HUNK_AGENT_BASE_URL`**（否则会明确报错，不会静默降级）。

### 4.3 推理型模型会返回空响应（踩过的坑）

有些模型（如 `deepseek-flash`）会把整个输出预算花在隐藏思考上，`content` 恒为空：

```
max_tokens=8192  finish=length content=0 reasoning_len=35173 reasoning_tokens=8192
max_tokens=16384 finish=length content=0 reasoning_len=62259 reasoning_tokens=16384
```

**加 max_tokens 无效**。已验证有效的开关：

| 参数 | 效果 |
|---|---|
| `reasoning_effort: "none"` | ✅ `finish=stop`，正常返回 JSON |
| `thinking: {"type": "disabled"}` | ✅ 同上 |
| `reasoning_effort:"minimal"` / `enable_thinking` / `chat_template_kwargs` / 提示词里要求"别思考" | ❌ 无效 |

已内置：`configs/models.yaml` 的 `openai` provider 默认带 `extra_body.reasoning_effort: none`；
任何 provider 都可用 `PATCH_HUNK_AGENT_REASONING_EFFORT=none` 覆盖。
运行记录会带 `finish_reason`/`usage`，并在"预算耗尽且无输出"时给出提示。

---

## 5. 核心概念与设计决定（不理解这些会误用）

### 5.1 三层模型（最重要）

实现文档把"哪些 Hunk 必须协同修改"直接等同于"哪些 Hunk 应放在同一个工作包"。
实测证明这个等式不成立，因此本实现把它拆成三层：

| 层 | 回答的问题 | 谁来做 | 输出 |
|---|---|---|---|
| ① 耦合约束 | 只移植 A 不移植 B，会不会破坏正确性？ | LLM 逐对判定 + 目标版本核对 | 依赖记录（含证据、强度、目标版本状态） |
| ② 工作包 | 如何按职责组织，使每个包能被独立移植与验证？ | **模型一次看全整份补丁来划分** | `groups[]` + 每包理由 + 独立验证方式 |
| ③ 跨包协调 | 包之间按什么顺序、共享什么上下文、哪些必须联合验证？ | 确定性程序（拓扑序 + 锚点重合 + 延迟约束） | `execution_plan` |

三者是**三个决策契约**，但不等于必须三次模型调用——调用粒度由实验决定（见 5.2）。

### 5.2 两种分组模式

| `grouping.mode` | 含义 | 何时用 |
|---|---|---|
| `global`（默认） | 模型一次看完整个补丁产出工作包；图算法只做**校验与有上限修复** | 真实使用 |
| `components` | 实现文档 §9.2 原算法：只把 STRONG 边连成无向图取连通分量 | 离线自检、实现文档 Test A–E、作为兜底 |

**为什么改默认**：同一模型、同一补丁，闭包算法把 33 个 Hunk 合成一个 22-Hunk 巨组
（过度分组 0.810），而"一次看全整份补丁直接划分"得到 0.073。所以分组这个**全局决策**交给模型，
图只负责"强依赖不得跨组"的约束校验。

### 5.3 有上限的约束修复 + deferred 约束（关键机制）

若某条强约束的端点被分到不同包，**只合并直接相连的那两个包**；如果合并会让包超过
`grouping.max_group_size`，则**拒绝合并**，把这条约束记为 `deferred_constraints` 并生成
未解决问题与联合验证要求。这样一条长调用链不会再把整个补丁"闭包"成一个包。

### 5.4 结论强度与降级规则

- `STRONG` 的必要条件：有**可定位**证据 + 有分离风险描述 + 目标版本状态不是 `present`；
  否则自动降级为 `CONDITIONAL`/`WEAK` 并留下痕迹。
- `CONDITIONAL` 只有在"目标确实缺该前置条件 + 证据可定位 + 证据已齐"时才提升为分组边。
- 目标版本状态：`present / absent / partially_present / unknown`；**没有目标版本代码时一律
  `unknown`**（此时全部依赖停在 CONDITIONAL，分组会自然偏保守）。

### 5.5 证据驱动

不因文件相同、函数名相同、文本相似、位置相邻就认定依赖；每条依赖都要给出可定位证据；
证据不足就标"待确认"而不是硬判。机器侧给的只是**候选对与信号**（`machine_hint`），
不参与最终强度判定。

---

## 6. 输出契约（跑完会得到什么）

`scripts/run_grouping.py --out <DIR>` 会写出：

| 文件 | 内容 |
|---|---|
| `grouping.json` | 分组文档（实现文档 §10.4）：`groups[]`（含 `hunk_ids`/`group_reason`/`dependency_ids`/`external_dependency_ids`）、`cross_group_dependencies`、`unresolved_questions`、`validation{all_hunks_assigned_once, strong_dependencies_within_groups, unresolved_dependencies_reported}`、`errors`/`warnings`/`checks` |
| `analysis.json` | 依赖分析：每条依赖含 `category`/`strength`/`evidence`/`separation_risk`/`target_version_status`/`reasoning_summary`/`needs_more_evidence`；另含 `rejected_records`（被拒的非法记录）与 `call_records`（每次模型调用） |
| `result.json` | 完整结果：还包含 `candidates`（机器候选对）、`contexts`（检索状态与符号证据）、`run`（事件、token、耗时） |
| `manifest.json` | 可复现信息：补丁 sha256、提示词哈希、模型标识、上下文（repo/源/目标版本）、每组 token/耗时、`execution_order`、`constraint_satisfaction` |
| `hunk_groups.json` + `merged_hunks/` | 给 PortGPT 的规格与补丁文件（见第 7 节），仅在传 `--portgpt-root` 时产出 |
| `evaluation.json` | 传 `--dataset` 时的评分（含 `--repeat N` 的均值/极值） |

`grouping.json` 的 `metadata.grouping` 里还有两个关键块：

- `execution_plan`：`order`（包间执行顺序）、`shared_target_regions`（多包改到同一锚点）、
  `joint_verification`（必须一起编译/测试的包组）、`directives`（每包的可执行指令）、
  `anchor_evidence: source_side_proxy`（**没有目标代码时的锚点是源侧代理，不是真实位置**）；
- `constraint_satisfaction`：`total / dissolved_by_target_version / active / enforced_in_package /
  deferred_reported / violated / satisfaction_rate`。

---

## 7. 与 PortGPT 的接口（不改它一行代码）

PortGPT（`portgpt-src-modified`）已经是"一个 merged hunk 一次"移植。它有**两个**输入接口：

1. `<patch_dataset_dir>/hunk_groups.json`（或 `config.yml` 的 `hunk_groups:`）→ 它自己现场合并；
2. `<patch_dataset_dir>/merged_hunks/original/*.patch` → **每个文件当一次 session 输入**（优先级更高）。

规格格式（`hunks` 是 **0 基下标**，由 PortGPT 自己的 `split_patch_indexed()` 编号）：

```json
{"groups": [{"id": 1, "name": "…", "hunks": [0, 1, 6], "concern": "…", "why_together": "…"}],
 "dependencies": [{"from_hunk": 3, "to_hunk": 6, "why": "…"}], "notes": "…"}
```

**唯一会静默出错的地方是下标错位**（把错误的 Hunk 合成一组且不报错）。因此本实现在导出前会：

1. 直接运行 **PortGPT 自己的** `split_patch_indexed`（能 import 就 import；缺
   `Levenshtein/langchain` 时从 `tools/utils.py` 里 AST 提取该函数执行，仍是它的代码）；
2. 与本 agent 的 `H1..Hn` 逐项比对**数量/顺序/文件路径/`@@` 头/正文行数**；
3. 只有对齐才导出，并校验规格是"每个 Hunk 恰好一次"的完整划分。

已验证（33 Hunk 的真实补丁）：**对齐 0 差异**；`merged-hunk-*.patch` 与 `index.json`/`index.md`
与 PortGPT 自己 `dump_merged_hunks` 的输出**逐字节一致**；协调指令会追加进 `why` 字段——
那是 PortGPT `describe_group()` 渲染进移植 prompt 的通道，所以**不改它代码也能改变其行为**。
（有协调内容时才在 `index.json` 增加 `coordination` 键；否则保持逐字节一致。）

---

## 8. 实测现状与读数（补丁：CVE-2025-48649，33 Hunk / 19 文件）

### 8.1 分组与人工 5 组参照的一致性（代理指标）

参照 = 人工分组 `M000–M004`（5 组，5–8 Hunk/组，来自 `CVE-2025-48649-groups.md`）。
指标定义（**参照物是人工分组，仅用于事后诊断，绝不作为智能体的输入**）：

- 先人工 5 组（大小 7/8/7/6/5）展开成"同组 Hunk 对"，共 **95 对**（21+28+21+15+10）；
- `co_membership_recall` = |预测同组对 ∩ 这 95 对| / 95 ——"人工认为该在一起的配对，被保住了多少"；
- `over_grouping_rate` = |预测同组对 \ 这 95 对| / |预测同组对| ——"我们合并的配对里，人工没合的比例"。

换算成绝对值便于阅读：0.600 = 保住 57/95；0.695 = 保住 66/95；0.537 = 保住 51/95。
**这两个指标衡量的是"与一份人工工作包划分的相似度"，不是回移成功率**；人工那份划分自身
也声明尚未经过 PortGPT 对照实验验证。因此它们只用于调优与诊断，最终结论必须来自产出对比。

| 方案 | 组数 | 召回 | 过度分组 | 最大组 | Token |
|---|---:|---:|---:|---:|---:|
| 旧 components（闭包） | 12 | 0.463 | 0.810 | 22 | 208k+12k |
| global（上限 8 / 证据 60） | 9 | 0.411 | 0.480 | 8 | 208k+12k |
| global（上限 6 / 证据 20） | 9 | 0.400 | 0.333 | 6 | 118k+10k |
| **global + 目标版本代码** | 9 | **0.474** | **0.237** | 6 | 198k+11k |
| 对照：一次划分实验（两次临时调用） | 9 | 0.537 | 0.073 | 6 | ~4k |
| 基线：既有的 9 组（早期由 DeepSeek 产出） | 9 | 0.600 | 0.095 | 6 | — |

**必须一起读的限定条件**（否则会误判）：**n = 1 个补丁、单模型（deepseek-flash 经中转）、
每配置单次运行、人工参照本身尚未通过实验验证、且各轮的证据量不同**（"过度分组下降"里有一部分
来自"喂给模型的证据更少"：活跃强约束从 36 条降到 6–10 条）。因此这些数字是**方向性观察**，
不是统计结论。

### 8.2 已经站得住的三件事

1. **巨组问题解决**：最大包 22 → 6，与人工参照同量级；
2. **目标版本感知确实生效**：检索状态 `unavailable` → `partial`；
   符号状态变成实测（例如 `restorePregrantedPermissions: absent`、`clearApplicationUserData: present`）；
   约束满足率 0.6 → 0.667，且**0 条强约束被违反**；
3. **协调信息是可执行的**，不只是备注：包间顺序 → 直接决定 PortGPT 读取的文件编号顺序；
   指令 → 进入它的移植 prompt；联合验证要求 → 记入 `index.json` 供失败归因。

### 8.3 还没站住的事

- **仍未跨过基线**（0.474/0.237 vs 基线 0.600/0.095）；我给出的"够好"门槛是
  **召回 ≥0.60 且过度分组 ≤0.10，同时约束满足率 ≥0.80**；
- 剩余误差集中在两条边界：`M000|M001` 与 `M002|M003`（相邻职责层之间），靠源侧结构猜不准；
- **"分组能否提升 PortGPT 的真实产出"完全未验证**——一次真实对照都还没跑。
  人工参照自己那份文档也声明它"尚未通过 PortGPT 对照实验验证"。

---

## 9. 已知限制 / 未实现

| 项 | 状态 |
|---|---|
| 真实目标锚点（方法级）与行为级锚点 | 未实现；无目标代码时只有**源侧代理锚点**（已在输出里标注 `anchor_evidence: source_side_proxy`） |
| 补丁外调用点扫描（旧调用点复核清单） | 未实现（需要目标版本代码；发现→覆盖→影响三步，机械可判的部分不应交给 LLM） |
| 跨 session 共享变更账本 | 未实现；需要"按上一组实际产出重写下一组输入"的驱动循环（或对 PortGPT 做小改动） |
| 真实 LLM 效果的多补丁验证、方差测量 | 未做（`--repeat N` 已支持，尚未系统使用） |
| 受控实验（固定证据量，只变划分方式） | 未做 |
| baseline vs treatment 真实对照 | **未做**（`scripts/portgpt_compare.py` 已就绪，环境未具备） |
| AST 级（tree-sitter/clang）符号分析 | 未实现：当前是词法级启发式，已在输出中标注为机器证据 |

---

## 10. 真实产出对比：怎么做（这是首要目标）

### 10.0 判定原则

**分组像不像人工参照只是代理指标；唯一有资格判定"够好"的是 PortGPT 的产出**：
移植成功率、编译通过率、测试通过率、回归率、token 与耗时。人工参照用于调优，不用于下结论。

### 10.1 预检（最快知道缺什么）

```powershell
python scripts/portgpt_compare.py `
  --portgpt-root <portgpt-src-modified> `
  --portgpt-config <它的 config.yml> `
  --spec results\<run>\hunk_groups.json `
  --merged-hunks results\<run>\merged_hunks `
  --dry-run
```

它会逐项检查并给出**可执行的结论**：PortGPT 入口存在？端口/密钥可用？依赖是否齐
（`yaml/git/httpx/langchain/langchain_openai/langchain_core`）？
`project_dir` 是否存在且是 git 仓库、三个 commit 是否可解析？`patch_dataset_dir` 是否存在？

### 10.2 跑对照

```powershell
python scripts/portgpt_compare.py --portgpt-root <…> --portgpt-config <…> `
  --spec <…hunk_groups.json> --merged-hunks <…merged_hunks>
# baseline = 把分组输入挪走，PortGPT 走原来的逐 Hunk 路径
# treatment = 放上我们的分组与协调指令
# 产物：results/compare-<tag>/{baseline.log,treatment.log,comparison.json}
```

判定只采用 **PortGPT 自己打印的结果**（成功/失败标记）+ `merged_hunks/ported/*.patch` 的产出数量；
haraness 不修改 PortGPT 代码，跑完会恢复它原来的输入文件。

### 10.3 若暂时不具备 PortGPT 运行环境——可自造最小工程

不需要 AOSP 全库也能验证链路：把目标版本的 19 个文件（`3-target-code-BEFORE`）作为一个 commit，
修复后状态（`4-target-code-AFTER` 或自己套补丁的结果）作为下一个 commit，即可得到一个满足
PortGPT 要求的 `project_dir`（含 `new_patch_parent` / `new_patch` / `target_release` 三个可解析的 commit）。
再准备一个 `patch_dataset_dir`，就能跑完整对照。

---

## 11. 48649 相关材料位置（同级的上级目录）

| 路径 | 内容 | 能用来干什么 |
|---|---|---|
| `..\48649-AOSP16-fix.patch` / `..\CVE-2025-48649-gpt5.5\1-AOSP16-fix.patch` | 上游修复补丁（两者都是 33 Hunk / 19 文件，内容同源） | 输入 |
| `..\CVE-2025-48649-gpt5.5\3-target-code-BEFORE\` | 目标版本（**修复前**）19 个受影响文件 | `--target-tree`：打开目标版本感知；做锚点与调用点扫描 |
| `..\CVE-2025-48649-gpt5.5\4-target-code-AFTER\` | 同一批文件的修复后状态 | 作为参考/自造最小工程的第二个 commit |
| `..\CVE-2025-48649-gpt5.5\2-PortGPT-generated.patch` | **PortGPT 自己的回移产出** | baseline 侧真实读数 |
| `..\CVE-2025-48649-gpt5.5\run.log`（608KB） | **PortGPT 运行日志** | 抽取"哪些 Hunk 直打成功/哪些走 LLM/失败点"，作为 baseline 判定 |
| `..\CVE-2025-48649-merge-by-hand\*` | 人工合并的补丁与 `compile-error-1.md` | 人工侧参考与失败样本 |
| `..\CVE-2025-48649-groups.md` | **人工 5 组**（M000–M004）与它的自我说明 | 代理指标的参照；注意它声明自己未被实验验证 |
| `..\hunk_groups.json` | 早期由另一模型产出的 9 组 | 作为对照基线 |
| `..\portgpt-src-original` / `portgpt-src-modified` | PortGPT 两版源码（后者已支持按组移植） | 集成对象（**用 modified 那版**） |

---

## 12. 复现本次读数的命令

> 关于真实 PortGPT 的那两项集成测试（下标对齐 + `merged hunk` 逐字节一致）：
> 它们会自动寻找 PortGPT 检出（先看环境变量 `PORTGPT_ROOT` / `PORTGPT_PATCH`，再依次试探
> 仓库父目录、`<repo>/portgpt-src-modified`、`<父目录>/backport_patch/portgpt-src-modified`
> 等约定位置）。**显式设置了环境变量但路径无效会直接报错**，不会退化成 skip；
> **一个都没找到时会发出 pytest warning** 把"这两项验证没跑"显式打出来，
> 避免"全套通过"里混着一条静默跳过的证据缺口。

```powershell
# 带目标版本代码（最接近"文档默认语义"的一轮）
$env:PATCH_HUNK_AGENT_REASONING_EFFORT = "none"
python scripts/run_grouping.py `
  --patch ..\48649-AOSP16-fix.patch --patch-id cve-2025-48649 `
  --target-tree ..\CVE-2025-48649-gpt5.5\3-target-code-BEFORE `
  --portgpt-root ..\portgpt-src-modified `
  --out results\with-target

# 与人工参照打分（需要一份 reference_groups 数据集；格式见 README 的 Evaluation 一节）
python scripts/run_grouping.py --patch ..\48649-AOSP16-fix.patch `
  --patch-id cve-2025-48649 --out results\with-target --dataset <dataset.json> --repeat 3
```

---

## 13. 术语速查

| 术语 | 含义 |
|---|---|
| Hunk / `H1..Hn` | 补丁中的单块修改，本 agent 分配的稳定 ID（按文档顺序） |
| 工作包 / package / group | 一个可独立移植与验证的任务单元（一组 Hunk） |
| 耦合约束 / constraint | "只改一边会坏"的关系；强度 `STRONG/CONDITIONAL/WEAK/NONE` |
| `deferred constraint` | 因超出包尺寸上限而**拒绝合并**、改为上报与联合验证的约束 |
| merged hunk | 一个工作包对应的一份补丁文件（成员 Hunk 文本按序拼接） |
| `target_version_status` | 该前置条件在**目标版本**中的状态：`present/absent/partially_present/unknown` |
| 联合验证 | 单独编译/测试无意义，必须与其它包一起编译/测试的包组 |
| 代理指标 vs 真实产出 | 与人工分组的一致性 vs PortGPT 的移植成功率/编译/测试 |

---

## 14. 一句话交接

代码是可跑、有测试、可复现的；**分组粒度问题已解决（22→6），剩余差距在"切在哪"，
需要目标版本代码做锚点与调用点分析**；而**"够不够好"只能由 PortGPT 的产出回答**——
对照实验脚本已就绪，缺的只是运行环境（PortGPT 依赖 + 一个可 checkout 的工程目录）。
