# gpt-5.5 重跑（带构建验证）

本轮把之前 7 个 case 全部用 **gpt-5.5** 重跑了一遍，并且**给每个 case 配了 build.sh**，
让 PortGPT 的 Validation Chain（编译 → 测试 → PoC）真正运行起来——之前那一轮因为
dataset 里没有这些脚本，三个阶段全部空转，工具报的 "Successfully" 只等于"补丁能被 git apply"。

- 模型：`gpt-5.5`（端点：GreatRouter）
- 源码改动：仅环境适配 —— 端点可配置；`get_usage()` 注释掉；编译改宿主机执行；
  另加"gpt-5 系列不接受非默认 temperature"的处理（`temperature` 按模型族取 1.0 / 0.5）
- 每轮日志：`run.log`（成功/失败原文）
- 状态表：`status.tsv`

## 各 case 状态（上传时的快照）

| case | 结果 | 编译验证 | 说明 |
|---|---|---|---|
| **CVE-2026-0045**（路径映射 sim 版） | ✅ Successfully | **PASS** | 100 秒产出 1 个文件的补丁，见本目录四样东西 |
| CVE-2026-0045（原始 case） | ❌ 崩溃 | — | 跨仓库导致 `git merge-base` 为空，工具直接崩 |
| CVE-2025-48649 | ❌ 中止 | — | 卡在 hunk 11（迭代预算耗尽），未进入编译阶段 |
| CVE-2026-46054（第 1 次） | ❌ 中止 | — | 端点返回 HTTP 400 `GR_CONTENT_POLICY_VIOLATION` |
| CVE-2026-46054（第 2 次） | ⏳ 跑中 | — | 上传时仍在运行 |
| CVE-2025-48595 / CVE-2025-32348 / CVE-2025-48533 / CVE-2025-48550 | ⏳ 跑中 | — | 跑完后补进来 |

## CVE-2026-0045 这一组

- `1-AOSP16-fix.patch` —— AOSP16 官方修复**映射到 A12 目录布局后**的版本
  （原提交在 `packages/modules/Bluetooth`，根目录多一层 `system/`）
- `2-PortGPT-generated.patch` —— 本轮 gpt-5.5 的产出（1 文件）
- `3-target-code-BEFORE/` / `4-target-code-AFTER/` —— A12 目标提交上的原样 / 打完补丁之后
- `run.log` —— 完整运行日志

与之前 gpt-4o 的产出对比（同一个 case、同一份输入）：

| | gpt-4o run1 | **gpt-5.5（本轮）** |
|---|---|---|
| 改动位置 | 放在 `if (LINK_KEY_KNOWN)` 分支**外** | 放在分支**内**（与 AOSP16 修复一致） |
| 字段名 | A12 扁平字段（可编译） | A12 扁平字段（可编译） |
| 编译验证 | 无（当时没有 build.sh） | **PASS** |
| 耗时 | 80s | 100s |

## 不能 git push 的说明

`github.com:443` 在本实验的两台服务器上都不可达（已实测），所以无法 `git push`；
本仓库的更新一律走 GitHub Contents API。每次写文件都是在分支上**新增一个 commit**，
不会重写或覆盖已有历史。
