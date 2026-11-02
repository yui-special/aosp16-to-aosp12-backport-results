# AOSP 16 → AOSP 12 六 CVE 回移结果

运行日期：2026-11-01  
服务器：`ncwang-server` (`172.28.8.69`)

## 结论

六个案例均已完成启动、执行和结果采集，最终没有实验进程残留。六份 `complete.patch` 都能在干净的 AOSP 12 target worktree 上通过 `git apply --check`。

机械 verdict 为 5 个 `OK`、1 个 `PARTIAL`。人工语义复核后，`CVE-2025-32348` 的 `OK` 不成立；`CVE-2025-48595` 保持 `PARTIAL`，其唯一失败组看起来更可能是目标版本不存在该实现机制，而不是可直接重复移植的漏改。

| 用户 CVE | 服务器案例名 | 运行 | verdict | complete.patch | 人工判断 |
|---|---|---|---|---:|---|
| CVE-2025-32348 | CVE-2025-32348 | rc=0 | OK | 8419 | **失败，假 OK** |
| CVE-2025-48649 | CVE-2025-48649 | rc=0 | OK | 16104 | 未发现明显语义错误 |
| CVE-2025-48533 | CVE-2025-48533 | rc=0 | OK | 9131 | 未发现明显语义错误 |
| CVE-2025-48595 | CVE-2025-48595 | rc=0 | PARTIAL | 17384 | 保留 PARTIAL；失败组很可能不适用于 A12 |
| CVE-2025-48550 | CVE-2025-48550 | rc=0 | OK | 1774 | 未发现明显语义错误 |
| CVE-2025-0045 | CVE-2026-0045 | rc=0 | OK | 740 | 未发现明显语义错误 |

## 代码与运行条件

- 部署源码根：`/data/ncwang/backport_patch/patch-hunk-agent-classifier-mapping-20261101/data_ncwang`
- `src/agent/invoke_llm.py` 的服务器 SHA-256：`03ca73e821bdc86705499d18f1400e492d1271903cf03cc824241897326ac451`
- 与本地最新修复文件哈希一致。
- `final_candidate_sources` 在服务器项目环境中验证了两种情况：live candidate 优先，checkpoint 仅在没有 live candidate 时兜底；2/2 通过。
- API 鉴权均为 `auth=200`。
- 控制并发最多 3 个；没有覆盖旧结果目录，也没有向 GitHub 上传任何内容。
- 所有 verdict 均标明“编译未执行”。

## 逐项结果

### CVE-2025-32348

- RUN_ID：`final-candidate-CVE-2025-32348-20261101`
- 结果目录：`/data/ncwang/backport_patch/final-candidate-CVE-2025-32348-20261101/results/CVE-2025-32348`
- 状态：`EXIT 0`、`DONE rc=0`、`verdict: OK`、chain rc=0
- 补丁：8419 字节，SHA-256 `8dcd4b040c12aa73869f364b4262aa5232ee2c07e47ee769d682044b7ee19978`
- 人工验收失败：
  - 补丁包含 `WindowContainer.alwaysTruePredicate()`；A12 target 中不存在该符号。
  - 原 `predicate == null || predicate.test(wpc)` 被改成直接 `predicate.test(wpc)`，删除了原有 null 保护。
  - 虽然包含 `hasVisibleNotPinnedActivity`，但没有按人工要求保留正确的 `match(uid, null)` 等价实现。
  - 额外存在 `hasActivityInPinnedTask()` 映射，与上游 `inPinnedWindowingMode()` 传参方式不同，是次要风险点。
- 该案例的机械 `OK` 是假 OK；这是本次结果中最明确的语义/编译风险。

### CVE-2025-48649

- RUN_ID：`six-latest-20261101-CVE-2025-48649`
- 结果目录：`/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48649/results/CVE-2025-48649`
- 状态：13/13 组 `complete=true`、`global_covered=true`、`validation_complete=true`；`EXIT 0`、verdict OK。
- 补丁：16104 字节，SHA-256 `ef6d7e4fd2464e5d10b3978ccc39053d001e50a32be30fe938e981ea9d98f679`
- API 链：检测到 4 个签名变化，旧参数个数调用点为 0。
- 参数贯穿 AIDL、ActivityManagerService、PackageManagerService、权限管理层。
- `clearApplicationUserDataLIF` 的 null packageName 保护仍保留，权限实现保留 `Objects.requireNonNull(pkg)`。
- 最终 `complete.patch` 与 `latest-validated.patch` 哈希一致；不存在旧 checkpoint 覆盖新候选。
- 未发现明显漏改参数、旧 checkpoint 覆盖或空指针保护缺失。

### CVE-2025-48533

- RUN_ID：`six-latest-20261101-CVE-2025-48533`
- 结果目录：`/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48533/results/CVE-2025-48533`
- 状态：6/6 组 `complete=true`、`global_covered=true`、`validation_complete=true`；`EXIT 0`、verdict OK。
- 补丁：9131 字节，SHA-256 `68782438e8875426db10fd4bd0bda54025bf808eca5954a831dd0f9be9a9c1fe`
- 修改 `ActivityRecord`、`StartingData`、`Transition`、`WindowState`，实现 keyguard unoccluding 期间延迟移除 starting window。
- 最终补丁与 `latest-validated.patch` 哈希一致。
- 部分原始轮次 `round_succeeded=false`，但最终语义成员覆盖、全局覆盖和 validation 均为 true；未发现假 OK。

### CVE-2025-48595

- RUN_ID：`six-latest-20261101-CVE-2025-48595`
- 结果目录：`/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48595/results/CVE-2025-48595`
- 状态：10/11 组成功；`EXIT 0`，但 `verdict: PARTIAL`，全局 hunk 覆盖率失败。
- 补丁：17384 字节，SHA-256 `a45cb638f9c7ad0fd127bbfe9fabb66617e8ebf17783398b81b30ae8d36e0a4a`
- 失败组：hunks `[39, 61, 62]`，涉及 `whereAddIndexedExpr` 的 `bMaybeNullRow` 改法。
- A12 target 中没有 `whereAddIndexedExpr`、`pIdxEpr`、`bMaybeNullRow`；表达式索引改写只在 `pLevel->iLeftJoin==0` 时执行，A12 中 RIGHT/FULL OUTER JOIN 又会被拒绝。
- 因此该失败组很可能是 3.44 新机制在 3.32 目标上不适用，而不是漏了一个可直接套用的同名补丁。
- 没有编译和运行测试，仍不能把机械 `PARTIAL` 升格为完整通过。

### CVE-2025-48550

- RUN_ID：`six-latest-20261101-CVE-2025-48550`
- 结果目录：`/data/ncwang/backport_patch/six-latest-20261101-CVE-2025-48550/results/CVE-2025-48550`
- 状态：2/2 组 `complete=true`、`global_covered=true`、`validation_complete=true`；`EXIT 0`、verdict OK。
- 补丁：1774 字节，SHA-256 `2ac04d333b57c2c7df2f7d248477690371f3c273008bec24b7e89d5eb20763a7`
- 首版错误使用目标中不存在的 `FrameworkParsingPackageUtils`；重试后正确改为 `ParsingPackageUtils`。
- 两个 `PkgUser` 构造路径均调用 `enforceValidPackage()`。
- 最终补丁与 `latest-validated.patch` 哈希一致；未发现明显语义错误。

### CVE-2025-0045 / 服务器名 CVE-2026-0045

- RUN_ID：`six-latest-20261101-CVE-2026-0045`
- 结果目录：`/data/ncwang/backport_patch/six-latest-20261101-CVE-2026-0045/results/CVE-2026-0045`
- 状态：1/1 组完成；`EXIT 0`、verdict OK；triage port/skip=1/2。
- 补丁：740 字节，SHA-256 `5823615635292cea4b0d07dfd3fc2757809a9cb574db1be2ab55e2b9fddfd9cc`
- 补丁在 `btm_sec.cc` 增加 temporary bonding 且要求认证时将 `is_possible` 置为 true。
- 新版 flag 和 bta_jv 删除 hunks 不适用于 A12 target；未发现明显语义错误。

## 附加校验

- 六份 final patch 均在干净 target worktree 上通过 `apply --check`。
- 六次 chain check 均为 rc=0，均未发现按旧参数个数调用的残留。
- 所有新启动案例使用新目录；32348 使用任务开始时已存在且未重复启动的目录。没有覆盖旧结果。
- 运行 manifest：本地 `work/run-manifest.md`。

