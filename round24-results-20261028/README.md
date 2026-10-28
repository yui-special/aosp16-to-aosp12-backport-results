# Round24：六案例完整运行结果

批次 `20261028T073110Z`，北京时间 2026-10-28 15:31:44—19:50:17。六例均执行结束，**5 例验证通过，1 例测试未通过**；整轮状态为 `INCOMPLETE`。

沿用 round23 的按 CVE 平铺结构：每例保留 `complete.patch`、`portgpt-summaries.md`、`run.log`，另外保留本轮的逐组移植前后对照 `group-comparison.md`。对照记录逐组产出，整包修复后的结果以 `complete.patch` 为准。48550 逐组阶段直接应用，未生成模型输入摘要，其摘要文件仅记录这一事实。

| 案例 | 组数 / 最大组 hunk 数 | 最终补丁 | 实际编译 | 定向测试 | 结果 |
|---|---:|---|---|---|---|
| CVE-2025-32348 | 14 / 10 | 7 文件，+274/−10 | 通过 | 25 通过 | VALIDATED |
| CVE-2025-48533 | 2 / 1 | 4 文件，+45/−4 | 通过 | 150 通过 | VALIDATED |
| CVE-2025-48550 | 2 / 2 | 1 文件，+14/−0 | 首次失败，修复后通过 | 3 通过 | VALIDATED |
| CVE-2025-48595 | 11 / 7 | 2 文件，+82/−20 | 通过 | 104 通过 | VALIDATED |
| CVE-2025-48649 | 10 / 17 | 9 文件，+77/−19 | 通过 | 5 通过，1 忽略 | VALIDATED |
| CVE-2026-0045 | 2 / 1 | 1 文件，+8/−0 | 通过 | 28 通过，1 失败 | INCOMPLETE |

## 修复与失败说明

- **32348**：最终语义审查发现缺项，整包修复后通过。与 `CVE-2025-32348-merge-by-hand/32348_manual_v2.patch` 的核心拦截逻辑一致；当前基线上可以采用。25 项 ActivityStarterTests 包含新增的两个 pinned 子场景，另外两个新增测试类的 7 项测试未实际执行。
- **48550**：初稿引用 AOSP12 不存在的 `FrameworkParsingPackageUtils`；改为 `ParsingPackageUtils.validateName(...)` 后编译和测试通过。
- **48649**：逐组阶段仍遗漏 `PackageManagerService.resetApplicationPreferences()` 中的两参数调用，最终 API 链检查后补成 `resetRuntimePermissions(pkg, userId, true)`。最终权限恢复逻辑与 `CVE-2025-48649-merge-by-hand/merge-by-hand-3.patch` 一致，保留外层 default/role 分支，在内部判断 restore 参数；hunk 19 和 28 由同一个旧版 Java 实现覆盖，没有 round23 错误合并外层条件的问题。实现可作为集成候选，但本轮 ActivityManagerTest 未直接验证权限恢复分支。
- **0045**：`net_test_bta` 17 项和 `net_test_stack_btm` 11 项通过；`net_test_stack_rfcomm` 报 `No test results`、`Test run incomplete. Expected 21 tests, received 0`，测试退出码 5。现有日志不足以确定补丁或环境根因。该例 `complete.patch` 是未通过测试的候选。

## 测试范围与来源

| 案例 | 实际测试目标 |
|---|---|
| CVE-2025-32348 | `WmTests:com.android.server.wm.ActivityStarterTests` |
| CVE-2025-48533 | `WmTests:com.android.server.wm.ActivityRecordTests`、`WmTests:com.android.server.wm.WindowStateTests` |
| CVE-2025-48550 | `FrameworksUiServicesTests:com.android.server.slice.SlicePermissionManagerTest` |
| CVE-2025-48595 | `CtsDatabaseTestCases:android.database.sqlite.cts.SQLiteDatabaseTest`、`libsqlite3_phone_number_utils_test` |
| CVE-2025-48649 | `FrameworksServicesTests:com.android.server.am.ActivityManagerTest` |
| CVE-2026-0045 | `net_test_stack_btm`、`net_test_stack_rfcomm`、`net_test_bta` |

使用自研 `patch-hunk-agent`，复用外部 `run_portgpt.sh`。摘要、分诊、分组、映射使用 GreatRouter `DeepSeek-V4-Pro`，生成和独立语义审查使用 GreatRouter `gpt-5.5`。构建目标为 `sdk_phone_x86_64-eng`，逐组累积移植后执行整包检查、真实编译和定向测试。

原始目标提交：frameworks/base 为 `25d54573e0f3d584b428e5a51491b0475932be50`，SQLite 为 `b5abf5d924e8b82312f57c2eddd61323a212c7db`，Bluetooth 为 `6fb8dc0913bbca8c89af8c33573afa67d8011d5f`。五个通过案例的最终补丁与实际验证点一致。

完整配置、阶段文件、编译测试日志和冻结源码保留在服务器批次 `/data/lyh/aosp/full-tests/20261028T073110Z` 及对应 `/data/lyh/aosp/cve-*/portgpt-case/logs/` 运行目录。构建脚本 `/data/lyh/aosp/build-frameworks-base.sh`、测试脚本 `/data/lyh/aosp/run-aosp-test.sh` 均留在服务器。仓库不重复保存中间补丁、检查点副本、源码压缩包和环境快照；归档日志已脱敏。
