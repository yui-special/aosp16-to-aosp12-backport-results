# gpt-5.5 重跑（带构建验证）

本轮把之前 7 个 case 全部用 **gpt-5.5** 重跑，并且**给每个 case 配了 build.sh**，
让 PortGPT 的 Validation Chain（编译 → 测试 → PoC）真正运行起来。上一轮因为 dataset 里
没有这些脚本，三个阶段全部空转，工具报的 "Successfully" 只等于"补丁能被 git apply"。

本目录里所有产物都带 `-gpt5.5` 后缀，用来和之前 gpt-4o 那批结果区分。

- 模型：`gpt-5.5`（端点：GreatRouter，`temperature=1.0`）
- 工具源码改动：仅环境适配 —— LLM 端点可配置；注释 `get_usage()`；编译改宿主机执行；
  增加"gpt-5 系列不接受非默认 temperature"的处理
- 每个 case 的完整运行日志随目录一起提供

## 各 case 状态（上传时的快照）

| 目录 | 结果 | 编译验证 | 说明 |
|---|---|---|---|
| `CVE-2026-0045-gpt5.5/` | ✅ Successfully | **PASS** | 100 秒产出 1 文件补丁；改动位置与 AOSP16 修复一致 |
| `CVE-2026-0045-original-case-gpt5.5/` | ❌ 崩溃 | — | 跨仓库导致 `git merge-base` 为空，工具直接崩（原 case 本身跑不了）|
| `CVE-2025-48649-gpt5.5/` | ❌ 中止 | — | 卡在 hunk 11（迭代预算耗尽），未进入编译阶段 |
| `CVE-2026-46054-gpt5.5/` | ❌ 中止 | — | 第 1 次被端点内容安全策略拦截（HTTP 400）；第 2 次上传时仍在跑 |
| `CVE-2025-48595 / CVE-2025-32348 / CVE-2025-48533 / CVE-2025-48550` | ⏳ 跑中 | — | 跑完后会以同样的 `-gpt5.5` 命名补进来 |

机器可读的状态表见 `status-gpt5.5.tsv`。

## CVE-2026-0045-gpt5.5/ 这一组（四样东西）

- `1-AOSP16-fix.patch` —— AOSP16 官方修复，**映射到 A12 目录布局后**的版本
  （原提交在 `packages/modules/Bluetooth`，仓库根目录多一层 `system/`）
- `2-PortGPT-generated.patch` —— 本轮 gpt-5.5 的产出
- `3-target-code-BEFORE/` —— A12 目标提交上的原样代码
- `4-target-code-AFTER/` —— 打完补丁之后的代码
- `run.log` —— 完整运行日志（含编译验证阶段）

与 gpt-4o 那次的对比（同一 case、同一份输入）：

| | gpt-4o（此前） | **gpt-5.5（本轮）** |
|---|---|---|
| 改动位置 | `if (LINK_KEY_KNOWN)` 分支**外** | 分支**内**（与 AOSP16 修复一致）|
| 字段名 | A12 扁平字段（可编译）| A12 扁平字段（可编译）|
| 编译验证 | 无（当时没有 build.sh）| **PASS** |
| 耗时 | 80s | 100s |

## 关于推送方式

`github.com:443` 在本实验的两台服务器上均不可达（已实测），无法使用 `git push`；
仓库更新一律通过 GitHub Contents API 完成，每次写文件都是在分支上**新增 commit**，
不重写、不覆盖已有历史。
