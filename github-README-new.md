# AOSP16 → AOSP12 Backport 实验结果

用 **PortGPT** 把 Android 16（AOSP16）的安全修复补丁移植到 Android 12（AOSP12）的实测结果。
结果按 **模型 × 是否有构建/测试验证** 分批存放，每个批次一个目录。

> 实验过程中遇到的所有问题（网络中断、仓库历史缺失、工具崩溃、"假成功"、hunk 叠加等）
> 以及对 PortGPT 源码做了哪些改动，见 **[EXPERIMENT-NOTES.md](./EXPERIMENT-NOTES.md)**。

---

## 当前目录结构

| 目录 | 模型 | 批次说明 |
|---|---|---|
| `CVE-2026-0045-gpt5.5/` | gpt-5.5 | **唯一成功且通过编译+行为验证**的 0045 结果（输入是路径映射后的 sim 版）|
| `CVE-2025-48550-gpt5.5/` | gpt-5.5 | 产出的补丁用对了 A12 的 `ParsingPackageUtils`（编译验证 PASS）|
| `CVE-2025-48595-gpt5.5/` | gpt-5.5 | sqlite 案例（编译验证 PASS）|
| `CVE-2025-48649-gpt5.5/` | gpt-5.5 | 中止于 hunk 11；`2-PortGPT-generated.patch` 是**从会话日志复原**的中止前产出（7 文件/152 行）|
| `CVE-2025-32348-gpt5.5/` | gpt-5.5 | 迭代预算耗尽中止；同样是复原补丁（3 文件/82 行）|
| `CVE-2025-48533-gpt5.5/` | gpt-5.5 | 判定"无需移植"——A12 中确实没有对应代码，结论正确（`2-` 为空文件）|
| `CVE-2026-46054-gpt5.5/` | gpt-5.5 | 内核案例：第 1 次被端点内容策略拦截，第 2 次成功且**真 gcc 编译通过** |
| `CVE-2025-48649-round2/` | gpt-4o | 早期重复实验（未改源码跑 5 次）里成功的那一次 |
| `CVE-2025-48550-gpt5.6-sol/` | gpt-5.6-sol | 无构建/测试批次的运行记录 |
| `CVE-2025-48550-gpt5.6-sol-build&test/` | gpt-5.6-sol | 带构建/测试批次的运行记录 |
| `CVE-2025-48595-gpt5.5-build&test/` | gpt-5.5 | 带构建/测试的运行记录，并附带本次使用的 `tests/` 测试脚本 |

> 早期 gpt-4o 的 7 个裸目录（`CVE-xxxx/`）已在 `remove 4o` 提交中移除。

---

## 两种产物格式

### A. 四样东西（`CVE-<id>-<model>/`）

| 名称 | 是什么 |
|---|---|
| `1-AOSP16-fix.patch` | **AOSP16 官方修复补丁**，即"标准答案"（跨仓库案例里是**路径映射到 A12 布局**之后的版本）。 |
| `2-PortGPT-generated.patch` | **PortGPT 生成的补丁**（`git diff` 导出）。可能为空——工具判定"无需移植"时就是空文件；中止的运行由会话日志复原。 |
| `3-target-code-BEFORE/` | **原本要改的目标代码**，按原仓库路径存放。 |
| `4-target-code-AFTER/` | **已经改了的目标代码**，与 BEFORE 对比即可看出工具实际改了什么。 |
| `run.log` | 该次运行的完整日志（含模型的推理文本、每次工具调用、观察结果、验证阶段）。 |

### B. 构建/测试批次（`CVE-<id>-<model>-build&test/`）

| 名称 | 是什么 |
|---|---|
| `run-info.txt` | `CVE=` / `MODEL=` / `TARGET=` / `RUN_ID=` / `EXIT_CODE=`（部分批次还记了 `BUILD=` / `TEST=` 结论） |
| `result.patch` | 该次运行产出的补丁 |
| `changed-files.txt` | `git diff --name-status` |
| `diffstat.txt` | `git diff --stat` |
| `git-status.txt` | 运行结束时的 `git status` |
| `original/<路径>` | 改动前的文件 |
| `modified/<路径>` | 改动后的文件 |
| `tests/`（可选） | 该 case 使用的测试脚本：`test.sh` 会被 PortGPT 在**编译验证之后自动执行** |

---

## 七个 CVE

| CVE | 涉及文件 | AOSP16 修复提交 | AOSP12 目标 | 来源公告 |
|---|---|---|---|---|
| CVE-2025-48649 | 19 | `7b9a9069` (frameworks/base) | `android-security-12.0.0_r69` | 2026-06-01 |
| CVE-2025-48533 | 3 | `b1c68906` (frameworks/base) | `android-security-12.0.0_r69` | 2025-08-01 |
| CVE-2025-32348 | 8 | `b96d7a51` (frameworks/base，共 7 个提交) | `android-security-12.0.0_r69` | 2026-06-01 |
| CVE-2025-48550 | 1 | `bae8ab23` (frameworks/base) | `android-security-12.0.0_r69` | 2025-09-01 |
| CVE-2026-0045 | 3 | `2ad8e1e0` (packages/modules/Bluetooth) | `android-security-12.0.0_r69` (system/bt) | 2026-06-01 |
| CVE-2025-48595 | 8 | `aaebce3d` (external/sqlite) | `android-security-12.0.0_r69` | 2026-06-01 |
| CVE-2026-46054 | 2 | `bc6c380c` (Linux 6.6.143) | Linux 5.10.270 | 内核 CVE |

注：`CVE-2026-0045` 是**跨仓库**案例——修复提交在 `packages/modules/Bluetooth`，AOSP12 的对应代码在
`system/bt`（两边路径差一层 `system/`、git 历史也互不相干）。`CVE-2025-48595` 的修复横跨
`external/sqlite` 与 `build/release`，本仓库只包含可运行的 sqlite 部分。

---

## 结果概览

| CVE | 模型 | 工具判定 | 产出 | 编译验证 | 行为测试 | 备注 |
|---|---|---|---|---|---|---|
| CVE-2026-0045 | gpt-5.5 | Successfully | 1 文件 | **PASS** | **PASS** | 改动位置与 AOSP16 修复一致 |
| CVE-2025-48550 | gpt-5.5 | Successfully | 1 文件/44 行 | **PASS** | — | 用对了 A12 的 `ParsingPackageUtils` |
| CVE-2025-48550 | gpt-5.6-sol | — | 1 文件/14 行 | — | — | 队友批次 |
| CVE-2025-48595 | gpt-5.5 | Successfully | 1 文件/14 行 | **PASS** | **PASS**（含 A12 原生 43 用例） | 覆盖度低于官方 8 文件补丁 |
| CVE-2026-46054 | gpt-5.5 | Successfully | 2 文件 | **PASS**（真 gcc） | — | 第 1 次被内容策略拦截 |
| CVE-2025-48649 | gpt-5.5 | 中止 | 7 文件/152 行（复原） | PASS（语法/符号检查） | — | 卡在 hunk 11 |
| CVE-2025-32348 | gpt-5.5 | 中止 | 3 文件/82 行（复原） | PASS（语法/符号检查） | — | 迭代耗尽 |
| CVE-2025-48533 | gpt-5.5 | Successfully | **0 文件** | PASS（空补丁必然通过） | — | 判定"无需移植"，**结论正确** |
| CVE-2025-48649 | gpt-4o | Successfully | 10 文件/295 行 | FAIL（语法错） | — | round2，重复实验最好的一次 |

**关键提醒：工具自报的 "Successfully" 不能直接当作成功率。** 早期 gpt-4o 批次里 7 个案例全部报成功，
但逐个体检发现：有 5 个的产出连编译都过不了，其中 `CVE-2025-48649` 只改了 1/19 个文件。
换用更强的模型后，产出质量明显提升（可编译率 2/7 → 6/6），但工具自报的成功率反而下降（7/7 → 5/7）——
因为强模型会真正去改代码，容易撞上工具"单个 hunk 30 轮预算 + 一个 hunk 卡住就整体放弃"的设计。

---

## 验证链：`build.sh` → `test.sh` → `poc.sh`

PortGPT 在拼完补丁后会依次执行项目目录里的 `build.sh`（编译）、`test.sh`（功能测试）、`poc.sh`
（漏洞验证），脚本不存在就视为通过。**原版这三步在 AOSP 案例上是空转的**（dataset 里没有脚本），
本仓库的实验为每个 case 补齐了 `build.sh`，并为部分 case 补齐了 `test.sh`：

| case | 编译验证 | 行为/功能测试 | A12 原生测试套件 |
|---|---|---|---|
| CVE-2026-0045 | ✅ 成员名存在性检查 | ✅ 修复语义测试（从源码抽 `btm_sec_is_upgrade_possible` 跑断言）| ❌ 需 AOSP 构建系统（依赖 libchrome + Rust 桥接头）|
| CVE-2025-48595 | ✅ 真 gcc 编译 amalgamation | ✅ SQL 功能测试（含 300 列 GROUP BY）| ✅ **A12 自带 43 用例全过** |
| CVE-2025-48550 | ✅ javac 语法 + 新增符号存在性 | — | ❌ 需 Soong |
| CVE-2025-48649 / CVE-2025-32348 | ✅ 同上 | — | ❌ 需 Soong（A16 自带测试文件在 A12 中不存在）|
| CVE-2025-48533 | — | — | ❌ 同上，且 A12 无对应代码 |
| CVE-2026-46054 | ✅ 真内核编译（out-of-tree）| ❌ 需启动目标内核 | ❌ 5.10 无 SELinux selftest |

由这些验证得到的四条结论：

1. **"Successfully" 只等于"补丁能 `git apply`"**：编译/测试/PoC 缺失时，三层验证全部空转。
2. **编译验证挡不住"什么都没做"**：`CVE-2025-48533` 产出空补丁照样通过（空补丁必然编译通过），
   只有行为测试才能发现。
3. **编译/静态检查能抓住"照抄新版本 API"**：例如 `FrameworkParsingPackageUtils`（A12 无此类）、
   内核的 `lbs_backing_file` / `FMODE_BACKING`（5.15+ 才有）。
4. **行为测试能抓住"改反方向"和"字段用错"**：0045 的三次历史产出中，一次字段用错（编译不过）、
   一次改反方向（断言失败），只有一次通过。

---

## 重复实验：`CVE-2025-48649-round2/`（未改源码，跑 5 次）

为了排除"结论是单次抽签抽出来的"，用**未改源码**的 PortGPT（仅三项环境适配：LLM 端点可配置、
跳过用量查询、编译走宿主机）把 CVE-2025-48649 连续跑了 5 次，模型 gpt-4o，temperature 0.5。

| 次数 | 耗时 | LLM 调用次数 | 结果 | 产出 |
|---|---|---|---|---|
| 1 | 7 分钟 | 76 | 卡在 hunk 13，中止 | 0 文件 |
| **2** | **52 分钟** | **273** | **跑完** | **10 文件 / 295 行 / 20 hunk** ← 本目录收录的就是这一次 |
| 3 | 16 分钟 | 107 | 卡在 hunk 15，中止 | 0 文件 |
| 4 | 10 分钟 | 49 | 卡在 hunk 9，中止 | 0 文件 |
| 5 | 10 分钟 | 63 | 卡在 hunk 11，中止 | 0 文件 |

要点：

- **成功率 1/5**。4 次失败是同一个模式：某个 hunk 迭代 30 轮没收敛（`Reach max_iterations`）后中止，
  而不是"判定无需移植"；成功那次的耗时和 LLM 调用量都是失败次的 3~5 倍。
- AOSP16 官方补丁动 19 个文件，其中 **10 个在 AOSP12 里真实存在**（另 9 个是 AOSP13+ 才拆出来的
  `*ServiceImpl` / `*TracingDecorator` / Kotlin `PermissionService` 和单元测试）。第 2 次生成的补丁
  改的 10 个文件**正好就是这 10 个**。
- 与后续 gpt-5.5 重跑（`CVE-2025-48649-gpt5.5/`）对照：gpt-5.5 会真正逐 hunk 适配（单 hunk 4~44 轮），
  因此在 hunk 11 撞上 30 轮预算而中止——**它的"失败"比 gpt-4o 的"成功"更接近正确产出**。

---

## 数据来源

- **AOSP16 修复提交**：Android Security Bulletin（`source.android.com/docs/security/bulletin`）公开的补丁链接
- **AOSP12 目标**：`android-security-12.0.0_r69` tag（框架/蓝牙/sqlite）；内核为 Linux 5.10.270
- **源码镜像**：清华大学 TUNA 镜像（`mirrors.tuna.tsinghua.edu.cn/git/AOSP`、`.../git/linux-stable.git`）

## 说明

- 本仓库只包含源码片段、补丁文本与运行日志，**不含任何 API key、令牌或凭据**。
- `3-target-code-BEFORE` 与 `4-target-code-AFTER` 只包含该 CVE 涉及的文件，不是完整源码树。
