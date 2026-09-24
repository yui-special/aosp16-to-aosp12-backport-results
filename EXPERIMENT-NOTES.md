# 实验过程记录：遇到的问题与对 PortGPT 源码的改动

本文件记录用 PortGPT 做 AOSP16 → AOSP12 跨版本移植实验时踩到的坑，以及对 PortGPT 源码做了哪些改动。

---

## 一、跑实验时遇到的问题

### 1. LLM 端点不稳定（最常见，也最致命）

**现象一**：任务直接崩，报
```
ssl.SSLEOFError: UNEXPECTED_EOF_WHILE_READING
openai.APIConnectionError: Connection error.
```

**现象二**：HTTP 请求挂起——进程没有任何 socket、日志不再更新，**永远不会超时**。
实测有一次卡在某个 hunk 上超过 10 分钟没有任何输出。

**为什么后果严重**：PortGPT **没有超时和重试机制**。端点抖动（实测 TCP 443 连通率低至 1/5）
一次就能毁掉已经跑了 25–60 分钟的任务。

**应对**（外部包装脚本，不改源码）：
- 开跑前探测端点，连续 3 次 TCP 443 成功才启动
- 看门狗：日志 5 分钟不增长就判定挂死，杀掉并在 60 秒后重试（每案例最多 5 次）

---

### 2. 仓库历史不完整导致崩溃（最隐蔽）

`git_history` 工具内部逻辑：
```python
merge_base = repo.merge_base(self.target_release, self.new_patch_parent)
start_commit = merge_base[0].hexsha if merge_base else None
...
repo.git.log("--oneline", f"-L {start_line},{end_line}:{filepath}",
             f"{start_commit}..{self.new_patch_parent}")
```

浅克隆时 `merge_base` 返回空 → 拼出 `None..<sha>` → git 直接报错 → 整轮任务崩溃：
```
git.exc.GitCommandError: Cmd('git') failed due to: exit code(128)
cmdline: git log --oneline -L 68,83:flags/security.aconfig None..ab906156...
stderr: fatal: ambiguous argument 'None..ab906156...'
```

**尝试过的三种方案**：

| 方案 | 结果 |
|---|---|
| 造合成提交（树 = AOSP16） | ❌ 该提交的 diff 达 **42619 文件 / 460 万行**（两个大版本的全部差异），LLM 一调 `git_history` 就被淹没，最终迭代耗尽 |
| 造合成提交（树 = 目标版本，no-op） | ❌ `git log -L` 返回空 → 工具走到 except 分支提示 "git_history is empty"，历史信息完全丢失 |
| **补全真实历史** `git fetch --unshallow` | ✅ **有效**。frameworks/base 的 `.git` 从 1.7G 增至 8.3G，`merge_base` 得到真实共同祖先，`git_history` 正常返回真实提交 |

**补全历史的副作用**：`git log -L` 要在跨越 5 年、上百万提交的历史上逐提交追踪代码，
**单次调用需要几分钟**（实测有一次持续 4.5 分钟、CPU 100%）。

---

### 3. 编译步骤依赖一个不存在的 docker 镜像

工具源码里编译走容器：
```python
docker_command = ["docker", "run", "-v", f"{self.dir}:{self.dir}", "--rm",
                  "build-kernel-ubuntu-16.04", "/bin/bash", "-c",
                  f"cd {self.dir}; bash build.sh"]
```
本机没有 `build-kernel-ubuntu-16.04` 镜像，拉取被 Docker 仓库白名单拒绝（耗时 42 秒）。

**更糟的是错误提取逻辑**：
```python
error_lines = "\n".join(line for line in compile_result.splitlines()
                        if "error:" in line.lower())
```
Docker 的报错文本里不含 `error:` → **喂给 LLM 的编译错误是空字符串** →
模型只能盲改补丁，实测首次运行 9 轮迭代全部失败、浪费 528 秒。

**解决**：改为在宿主机直接执行 `bash build.sh`。

---

### 4. 用量查询请求被墙的域名，任务启动即崩

`src/check/usage.py` 的 `get_usage()` 请求 `https://api.openai.com/v1/usage`。
本服务器访问不到该域名，且实验使用的是兼容端点（GreatRouter）的 key，查询必然失败 →
**任务在初始化阶段就抛 `ConnectionError` 退出**。

**解决**：注释掉调用。（上游代码没有提供任何关闭开关；参照实现里出现过 `PORTGPT_DISABLE_USAGE` 环境变量的做法。）

---

### 5. 工具自身的缺陷（跨版本场景下被触发）

| # | 缺陷 | 触发条件 | 报错 |
|---|---|---|---|
| 1 | `_apply_hunk` ↔ `_apply_file_move_handling` 相互递归 | 补丁里的文件路径在目标版本不存在 | `RecursionError`（无限递归） |
| 2 | 相似文件搜索 `os.walk` 未排除 `.git` | 模糊匹配命中 `.git/` 下的文件 | `KeyError: "Blob or Tree named '.git' not found"` |
| 3 | `revise_patch` 未校验行号上界 | 新旧版本文件长度差异大 | `IndexError: list index out of range` |
| 4 | 数据集目录不能含子目录 | `shutil.copy2` 遇到目录 | `IsADirectoryError` |

前三个通过"**路径归一化**"规避——把目标代码的目录层级改成与 AOSP16 补丁期望的一致
（例如 sqlite：AOSP12 的 `dist/sqlite3.c` ↔ AOSP16 的 `dist/sqlite-autoconf-3440300/sqlite3.c`）。
归一化后"文件不存在"分支不再触发，这些崩溃点也就绕开了。

---

### 6. 算法层面的失效（不是崩溃，是"假成功"）

**a) 迭代耗尽 → 整轮中止，成果全丢**

单个 hunk 超过 `max_iterations`（默认 30）后，`do_backport` 直接 `return`：
既不报成功也不报失败，而且**已经成功应用的 hunk 全部被丢弃**，最终产出 0 字节。

CVE-2025-48649 连续三次尝试分别死在 hunk 13 / 6 / 7，每次都颗粒无收。

**b) "无需移植"造成的假成功**

当 AOSP16 的改动在目标版本里"找不到对应代码"时，工具按提示词的规则判定 `need not ported`：
```
3.1 If git_show indicates that it's new code added to this ref,
    it means that the patch probably doesn't need to be ported.
```

CVE-2025-48649 里 **19 个文件中 18 个**被这样判掉，最终只改 1 个文件，却报告 `Successfully backport`。

**实测反证**：被判定"无需移植"的 `ActivityManagerService.java`，其 `clearApplicationUserData`
方法与 `CLEAR_APP_USER_DATA` 权限检查在 AOSP12 中**确实存在**，AOSP16 的修复正是改这段代码 →
该移植是必需的，工具的判断是错的。

**c) 零 AI 参与的成功**

当补丁可以原样 `git apply` 到目标版本时（CVE-2025-48550），工具一次 LLM 都没调用，
仍然报告成功。这种"成功"考的是上下文是否一致，而不是 AI 的移植能力。

---

## 二、对 PortGPT 源码的改动

> 改动前 / 改动后的完整源码备份：
> `portgpt-src-BEFORE-skip-modification-20260924.tar.gz`
> `portgpt-src-AFTER-skip-modification-20260924.tar.gz`

### 改动 1：编译从容器切到宿主机（环境适配）

文件：`src/tools/project.py`

```diff
-        docker_command = ["docker", "run", "-v", f"{self.dir}:{self.dir}", "--rm",
-                          "build-kernel-ubuntu-16.04", "/bin/bash", "-c",
-                          f"cd {self.dir}; bash build.sh"]
-        build_process = subprocess.Popen(docker_command, ...)
+        build_process = subprocess.Popen(
+            ["/bin/bash", "build.sh"],
+            stdin=subprocess.DEVNULL,
+            stdout=subprocess.PIPE,
+            stderr=subprocess.PIPE,
+            cwd=self.dir,
+            text=True,
+        )
```

原因：见问题 3。镜像不存在，不改这条任何案例的编译都会失败。

### 改动 2：LLM 端点从配置读取（环境适配）

文件：`src/agent/invoke_llm.py`

```python
base_url = getattr(data, 'llm_base_url', "https://endpoint.greatrouter.com/v1")
model_name = getattr(data, 'llm_model', "gpt-4o")
```

原因：上游硬编码 `https://api.openai.com/v1`，实验环境无法访问该域名。

### 改动 3：注释掉用量查询调用（环境适配）

文件：`src/backporting.py` —— `get_usage()` 的调用被注释。

原因：见问题 4，不改则任务启动即崩。

### 改动 4：迭代耗尽的 hunk 改为「跳过并继续」，并在日志末尾汇总（**能力改动**）

文件：`src/agent/invoke_llm.py`

**改前**：
```python
                logger.error(f"Reach max_iterations for hunk {idx}")
                return                      # 整轮中止，已成功的 hunk 全部丢弃
```

**改后**：
```python
    skipped_hunks = []                      # 循环前初始化
    ...
                logger.error(
                    f"Reach max_iterations for hunk {idx}, "
                    f"SKIPPED this hunk and continue with the next one"
                )
                skipped_hunks.append((idx, pp))
                try:
                    project.repo.git.reset("--hard")
                except Exception:
                    pass
                continue                    # 跳过这个 hunk，继续处理下一个
    ...
    # 所有 hunk 处理完后，把跳过的集中写到日志末尾
    if skipped_hunks:
        logger.error("=" * 72)
        logger.error(
            f"[SKIPPED HUNKS] {len(skipped_hunks)} of {len(pps)} hunks were skipped "
            f"(max_iterations reached): {[i for i, _ in skipped_hunks]}"
        )
        for _idx, _pp in skipped_hunks:
            logger.error("-" * 72)
            logger.error(f"--- skipped hunk #{_idx} ---")
            logger.error(_pp)
        logger.error("=" * 72)
```

动机：CVE-2025-48649（19 文件 / 33 hunk）连续三次因单个 hunk 迭代耗尽而整轮归零，
得不到任何部分结果。改动后至少能保住已成功的 hunk，并把失败的 hunk 完整记录在日志末尾便于人工补做。

### 试过但**已回滚**的改动（为了保持与"未改源码版"的可比性）

| 改动 | 当时为什么加 | 为什么回滚 |
|---|---|---|
| `_git_history` 跨仓库降级（`merge_base` 为空时返回说明而非报错） | 避免崩溃 | 后来改用「补全真实历史」，不再需要 |
| `_apply_hunk` 递归保护（只允许一次路径重映射） | 避免无限递归 | 路径归一化后不再触发 |
| `find_most_similar_files` 排除 `.git` | 避免 KeyError | 同上 |
| `_git_history` 额外注入「AOSP16 侧代码」给模型 | 让模型同时看到新旧两版代码 | 与"stock 行为对照实验"冲突，撤掉 |

对应备份：`src.before-restore/`、`src/tools/project.py.bak*`、`src/tools/utils.py.orig`

---

## 三、小结：PortGPT 在这批任务上的三层问题

1. **环境依赖脆弱**：容器镜像缺失、被墙的域名、缺 ctags，任何一个都会让任务直接失败。
2. **强依赖"目标仓库里存在修复提交的真实历史"**：该前提在 AOSP16→AOSP12 场景下不成立，
   必须人工补全历史（frameworks/base 需 8.3G）才能正常运转。
3. **判定规则跨版本失效**：把"新旧版本代码形态不同"误判为"旧版本无需修改"，
   从而**放弃必要的移植并报告成功**——这是本批实验中最主要的失败形态。
