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

---

## 数据来源

- **AOSP16 修复提交**：Android Security Bulletin（`source.android.com/docs/security/bulletin`）公开的补丁链接
- **AOSP12 目标**：`android-security-12.0.0_r69` tag（框架/蓝牙/sqlite）；内核为 Linux 5.10.270
- **源码镜像**：清华大学 TUNA 镜像（`mirrors.tuna.tsinghua.edu.cn/git/AOSP`、`.../git/linux-stable.git`）

## 说明

- 本仓库只包含源码片段与补丁文本，**不含任何 API key、令牌或凭据**。
- `3-target-code-BEFORE` 与 `4-target-code-AFTER` 只包含该 CVE 涉及的文件，不是完整源码树。
