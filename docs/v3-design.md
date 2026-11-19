# Baton v3 设计稿（Agy 核心 + Claude 编码的 Research Baton）

Baton v3 是面向研究型开发与实验工程（Research Baton）的全新大版本，以 **Antigravity CLI (`agy`)** 为核心编排与冷眼审计平台，以 **Claude Code (`claude`)** 专注编码执行，并引入经 Gemini 4 Neon (XHigh) 首席顾问拍板的 **单轨冷眼审计 + 一等 `baton advise` + 0-token 研究红线守卫** 体系。

## 一、核心架构与四角色分工

```text
用户 ◄──► Conductor (agy / Gemini 4 Carbon High) ──[baton advise design|decide|unblock]──► Advisor (agy -p / Gemini 4 Neon XHigh, 无状态)
                │                                                                                  ▲
                │ start / resume / steer                                                           │ 终审 CLOSE / VIOLATION /
                ▼                                                                                  │ 连续≥2 REVISE（单轨直达，跳过 Monitor）
         Executor (claude -p / Claude Opus XHigh) ──stream-json──► Baton CLI (L1 0-token Watchdog + 冻结校验 + 亲跑验证 + RED_FLAGS 扫描 + 卷宗封存)
                                                                                                   │
                                                                                                   │ 常规 REPORT
                                                                                                   ▼
                                                                                      Monitor (agy -p / Gemini 4 Hydrogen XHigh, 只读卷宗)
                                                                                                   │
                                                                                                   ▼
                                                                                    Findings (F1/F2...) ──► Conductor `baton rule` 入哈希链账本
```

| 角色 | 默认模型 / 运行时 | 上下文状态 | 职责边界 |
|---|---|---|---|
| **Conductor（日常总控）** | `gemini-4-carbon-high` / `agy` 主会话 | 持续会话 | 起草 `brief.md` / `harness.md`、驱动状态机、判断是否需要 `baton advise`、逐条 `baton rule` / `baton decide` / `baton close`。严禁直接改代码、严禁绕过冷眼审计自裁。 |
| **Advisor（首席设计 & 高阶终审）** | `gemini-4-neon-xhigh` / `agy -p` | **严格无状态冷启动** | ① 开题/大修设计把关（`baton advise design`）；② 关键决策权衡与判例草案（`baton advise decide`）；③ 终审/违例/连续≥2次 REVISE 的单轨冷眼审计（`baton advise audit` 或自动路由）；④ 卡壳根因诊断（`baton advise unblock`）。 |
| **Monitor（常规冷眼审计）** | `gemini-4-hydrogen-xhigh` / `agy -p` | **只读卷宗冷启动** | 对常规 `REPORT` 卷宗执行跨家族冷眼证据核对，逐条输出偏离（`F1/F2...`）和记忆候选建议；不做设计/路线拍板。 |
| **Executor（代码执行者）** | `claude` CLI / `opus` (`--effort xhigh`) | 按阶段 `--resume` | 唯一代码写入者；按简报实现、跑测试、追加 `.baton/log.md`、交大修改报告 `R*.html` 并按 `leg-result.schema.json` 输出。 |

## 二、为什么选择「单轨冷眼 + 0-token 轮中守卫」（Neon XHigh 架构裁决）

1. **为什么保留独立 Monitor，而不是全部收回给 Conductor + Advisor？**
   - 研究型任务最致命的失败是**静默科学失效**（退出码 0、曲线好看，但存在数据泄露、评测口径漂移、静默吞异常或遗留调试截断 `[:100]`）。
   - 若取消常规轮次的独立只读审计，Carbon 主会话既写 brief 又审自己模糊需求的产出，极易被 Claude Opus 的流畅叙事说服，导致第 1–3 轮混入的数据泄露浪费后续数小时实验。
   - 保持 Gemini 家族（Hydrogen/Neon）只读冷证据审阅 Claude Opus 产出，形成**跨模型家族制衡**。
2. **为什么重构为「单轨冷眼」，且取消默认 10/30/60m 定时 LLM 轮询？**
   - 在 v3 中，Conductor（Carbon）、Advisor（Neon）、Monitor（Hydrogen）共享同一份 `agy` 5h/7d 配额池（Executor 走独立 Claude 配额）。
   - **单轨冷眼**：每份封存卷宗只由**一位**独立冷眼角色审计——常规 `REPORT` 归 Hydrogen Monitor；终审（`final`）、违例（`violation`）和连续 ≥2 次 `REVISE` 直接路由给 Neon Advisor（跳过 Hydrogen，不叠罗汉、防止低阶结论锚定高阶审查）。
   - **L1 0-token 轮中守卫**：默认关闭盲目定时 LLM 检查（`monitor.midleg_llm_checks: false`），由 Baton CLI 的 60s 冻结校验 + 确定性规则信号 + `claude --output-format stream-json` 实时流解析（抓权限拒批、连续工具报错死循环、compaction 爆发）承担轮中看护，把 `agy` 审计消耗降低约 70%。
   - **降努力度不降制度**：`agy` 5h 配额低于 30% 时，一键 `baton preset use research-conserve` 把 Monitor/Advisor 降到 `high`，绝不取消独立冷眼边界。

## 三、研究型确定性红线（0-token `RED_FLAGS`）与防锚定顺序

- **卷宗封存静态扫描**：`baton` 在生成 `evidence.md` 顶部自动运行 `v3_research.scan_diff_red_flags(diff_patch)`，扫描新增代码行中的：
  - `silent_exception`（静默 `except: pass` / 吞错回退）
  - `hardcoded_truncation`（疑似调试截断 `[:N]`、`max_samples=`、`nrows=`、`.head(N)`）
  - `test_skip_or_xfail`（新增 `skip`/`xfail`）
  - `tolerance_or_metric_relax`（修改 `atol`/`rtol`/`tolerance`/`threshold`）
  - `seed_or_split_change`（修改 `seed`/`random_state`/`test_size`）
  - `nan_or_error_drop`（新增 `dropna`/`nan_to_num`/`ignore_errors=True`）
- **防锚定阅读顺序**：`monitor-prompt.md` 与 `advisor-prompt.md` 明确要求先读 `charter.md` + `precedents.md` → 再读 `evidence.md`（含 `RED_FLAGS` 和 Baton 亲跑验证）+ `diff.patch` + `code/` → **最后才读** `report.html`。

## 四、一等子命令：`baton advise`

严禁 Conductor 用自然语言“转述病情”去手工调用 Advisor。所有 Advisor 调用走确定性 CLI 子命令，上下文由 CLI 从权威文件打包进只读快照目录，结果落盘 `.baton/advice/A<NNN>.{json,md}` 并记入哈希链账本（`type="advice"`）：

| 命令 | 时机 | CLI 自动打包输入 | 产出与联动 |
|---|---|---|---|
| `baton advise design <task>` | `start` 前开题门控 / 重大重设计 | `brief.md`、`harness.md`、`precedents.md`、`--files` | `A<n>.{json,md}`：科学漏洞、缺失不变量、建议冻结路径、止损条件 |
| `baton advise decide <task> --decision D001` | `DECISION` 事件 / 路线转向 | 决策请求、`brief.md`、最新 `evidence.md`、`precedents.md` | 推荐方案、权衡、判例草案；可由 `baton decide --advice A<n>` 关联入账 |
| `baton advise audit <task> --dossier <id>` | 显式升级卷宗冷眼审计 | 封存只读卷宗（禁附 Conductor 个人论述） | 复用 `monitor-brief.schema.json`，直接写入 `reviews/<id>-brief.md` 与账本，无缝接 `baton rule` |
| `baton advise unblock <task> -q "..."` | `BLOCKED` / 连续调试卡壳 | 近 2 轮 `evidence.md`、`diff.patch`、`log.md` 尾迹、错误日志 | 根因假设 + 最小诊断步骤 + 是否建议重置 Executor 会话 |

## 五、Claude Code (`claude`) Executor 后端关键工程要点

1. **隔离用 `--safe-mode`，禁用 `--bare`**：`--safe-mode` 屏蔽用户 `CLAUDE.md`、skills、hooks、plugins、MCP，同时完整保留本机 Claude OAuth 订阅鉴权（`--bare` 会跳过 OAuth 导致订阅用户鉴权失败）。
2. **Harness 不污染仓库**：审定后的 `harness.md` 与章程摘要拼装到 `$BATON_HOME/projects/<id>/tasks/<task>/claude/system-prompt.md`，通过 `--append-system-prompt` 注入，绝不改动仓库里的 `CLAUDE.md`。
3. **会话重置防快照漂移**：Claude Code 默认 `--system-prompt-snapshot on`（利于 prompt cache）。当 `harness_sha256` 变更、`VIOLATION` 后、`rollback` 后、连续轮数达到 `max_legs_per_session`（默认 4）或单轮发生 ≥2 次 compaction 时，Baton 自动开启新 UUID 会话并自动拼接压缩交接包（`brief.md` + `precedents.md` + `log.md` 尾迹）。
4. **双通道流式观测 + 越权围栏**：统一使用 `--output-format stream-json --verbose --json-schema <leg-result.schema.json>` + `--permission-mode bypassPermissions --permission-prompts none --disallowedTools "Bash(baton *)" "Bash(git push*)" "Bash(rm -rf /*)"`；若中途被 `stop`/`violation` 中断，自动基于已捕获事件与 `log.md` 合成兜底结果，保证卷宗封存不断链。

## 六、内置预设与 v2 兼容

`baton init` 默认写入三套预设，随时可用 `baton preset use <name>` 切换：
- `research`（默认）：`agy` Carbon(High) + Neon(XHigh) + Hydrogen(XHigh) + `claude` Opus(XHigh)
- `research-conserve`：`agy` 额度紧张时降 Monitor/Advisor 到 `high`，不取消独立冷眼边界
- `v2-codex`：完整兼容 Baton v2 的 Codex/Hermes 编排流
