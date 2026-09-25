# AOSP16 → AOSP12 Backport 实验结果

用 **PortGPT** 把 Android 16（AOSP16）的安全修复补丁移植到 Android 12（AOSP12）的实测结果，
共 7 个 CVE。每个 CVE 一个目录，目录里放四样东西。

> **实验过程中遇到的所有问题**（网络中断、仓库历史缺失、工具崩溃、"假成功"等）
> **以及对 PortGPT 源码做了哪些改动**，见 **[EXPERIMENT-NOTES.md](./EXPERIMENT-NOTES.md)**。

---

## 每个 CVE 目录里的四样东西

| 名称 | 是什么 |
|---|---|
| `1-AOSP16-fix.patch` | **AOSP16 官方修复补丁**。Android Security Bulletin 里那条修复提交的完整 diff，即"标准答案"。 |
| `2-PortGPT-generated.patch` | **PortGPT 生成的补丁**。工具针对 AOSP12 实际产出的补丁（`git diff` 导出）。可能为空——工具判定"无需移植"时就是空文件。 |
| `3-target-code-BEFORE/` | **原本要改的目标代码**。AOSP12 目标提交上，涉及文件的原样内容，按原仓库路径存放。 |
| `4-target-code-AFTER/` | **已经改了的目标代码**。把 `2-PortGPT-generated.patch` 应用到目标代码之后的结果。与 BEFORE 对比即可看出工具实际改了什么。 |

三份 patch/代码都按原仓库路径组织，所以同一个文件在 BEFORE / AFTER 里路径相同，可以直接 diff。

> 例外：`CVE-2025-48649` 另有一个 **[`CVE-2025-48649-round2/`](./CVE-2025-48649-round2)** 目录，
> 是重复实验里"跑成功的那一次"的结果，同样是上面这四样东西，详见下方"重复实验"一节。

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

注：`CVE-2026-0045` 是**跨仓库**案例——修复提交在 `packages/modules/Bluetooth`，
而 AOSP12 的对应代码在 `system/bt`（Android 12 时代蓝牙协议栈的位置）。
`CVE-2025-48595` 的修复横跨 `external/sqlite` 与 `build/release` 两个仓库，
本目录只包含可运行的 sqlite 部分（`build/release` 在 AOSP12 中不存在）。

---

## 结果概览

工具自报的判定 vs 实际改动量：

| CVE | 工具判定 | PortGPT 改动 | AOSP16 修复改动 | 核心文件覆盖 |
|---|---|---|---|---|
| CVE-2026-46054（内核） | 成功 | 2 文件 | 2 文件 | **2/2** |
| CVE-2025-48595（sqlite） | 成功 | 9 文件 | 8 文件 | **8/8** |
| CVE-2025-32348 | 成功 | 4 文件 | 8 文件 | 3/4 核心 |
| CVE-2025-48533 | 成功 | 2 文件 | 3 文件 | 2/3 |
| CVE-2025-48550 | 成功 | 1 文件 | 1 文件 | 1/1（补丁可直接应用，未调用 LLM） |
| CVE-2025-48649 | 成功 | **1 文件** | 19 文件 | **1/19**（18 个文件被判"无需移植"） |
| CVE-2026-0045（蓝牙） | 成功 | **0 文件** | 3 文件 | **0/3**（全部判"无需移植"） |

**七个案例工具都报"成功"，但后两个是"假成功"**——工具依据历史分析判定"这段代码是新引入的，
旧版本不存在，无需移植"，而实际 AOSP12 中存在等价代码，移植是必需的。

> **后续更正（2026-09-25）**：上表 `CVE-2025-48649` 那一行是**单次运行**的结果。随后用未改源码的
> PortGPT 把它连续跑了 5 次，其中 1 次给出了**文件级完整的移植**（AOSP12 中真实存在、且官方修复
> 触及的 10 个文件全部改到，0 个多余文件），见 [`CVE-2025-48649-round2/`](./CVE-2025-48649-round2)。
> 因此把该 CVE 简单归为"假成功"并不准确——它更像"低概率真成功 + 高概率中途卡死"。

> **后续更正（2026-09-25，CVE-2026-0045）**：上表最后一行同样是"环境问题"，不能计入工具表现。
> AOSP16 的修复提交在 `platform/packages/modules/Bluetooth`（该仓库根目录下多一层 `system/`），
> AOSP12 的对应代码在 `platform/system/bt`，两边**路径差一层、git 历史也互不相干**
> （工具算出的 `merge_base` 为空，直接崩溃），所以第一轮既取不到目标代码、也没能产出补丁。
> 把路径映射对齐后重跑 3 次：工具能产出补丁了，但**只有 1 次可用**（另两次分别是字段照抄新版本
> 导致编译不过、语义完全反向），而三次都自报 "Successfully"。详见 [`CVE-2026-0045/`](./CVE-2026-0045)
> 里的 `0-README.md`。

---

## 重复实验：`CVE-2025-48649-round2/`（未改源码，跑 5 次）

为了排除"结论是单次抽签抽出来的"这种可能，我们用**未改源码**的 PortGPT
（只保留三项环境适配：LLM 端点可配置、跳过用量查询、编译走宿主机；无任何能力改动）
把 CVE-2025-48649 连续跑了 5 次，模型 gpt-4o，temperature 0.5。

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
- AOSP16 官方补丁动 19 个文件，其中 **10 个在 AOSP12 里真实存在**（另外 9 个是 AOSP13+ 才拆出来的
  `*ServiceImpl` / `*TracingDecorator` / Kotlin `PermissionService` 和单元测试，AOSP12 中根本没有）。
  第 2 次生成的补丁改的 10 个文件**正好就是这 10 个**，一个不多、一个不少。
- 本目录的 `2-PortGPT-generated.patch` 就是第 2 次的那份补丁；`3-target-code-BEFORE/` 与
  `4-target-code-AFTER/` 的差异文件，与它的 10 个文件一一对应。

---

## 数据来源

- **AOSP16 修复提交**：Android Security Bulletin（`source.android.com/docs/security/bulletin`）公开的补丁链接
- **AOSP12 目标**：`android-security-12.0.0_r69` tag（框架/蓝牙/sqlite）；内核为 Linux 5.10.270
- **源码镜像**：清华大学 TUNA 镜像（`mirrors.tuna.tsinghua.edu.cn/git/AOSP`、`.../git/linux-stable.git`）

## 说明

- 本仓库只包含源码片段与补丁文本，**不含任何 API key、令牌或凭据**。
- `3-target-code-BEFORE` 与 `4-target-code-AFTER` 只包含该 CVE 涉及的文件，不是完整源码树。
