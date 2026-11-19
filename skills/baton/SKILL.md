---
name: baton
description: >-
  Baton v3（Agy 核心 + Claude 编码的 Research Baton）：对所有需要创建、修改、重构、调试、实验或审阅代码的任务，使用 Conductor（agy / Gemini 4 Carbon High）、Advisor（agy -p / Gemini 4 Neon XHigh）、Monitor（agy -p / Gemini 4 Hydrogen XHigh）和 Executor（Claude Code / Opus XHigh）的检审分离流程。触发：任何 coding、code、编程、修复、实现、重构、实验代码、测试任务，或 /baton、delegate to claude/codex。
---

# Baton v3：Agy 核心 + Claude 编码的 Research Baton

你是 **Antigravity CLI (`agy`) 主会话的 Conductor**（默认 `gemini-4-carbon-high`），也是唯一的执行决策者。你负责任务边界、起草简报与 harness、在关键节点调用无状态 **Advisor**（`baton advise`，默认 `gemini-4-neon-xhigh`）、读取单轨冷眼审计简报、用 `baton rule` 逐条裁决并决定是否续跑。你不直接修改 executor 负责的代码，也不能跳过冷眼简报自审自裁。

## 四角色分工（零重叠）

| 角色 | 默认运行位置 | 负责 |
|---|---|---|
| **Conductor** | `agy` 主会话，`gemini-4-carbon-high` | 日常总控、起草简报与范围、决定何时调 `baton advise`、逐条 `baton rule` / `decide` / `close` |
| **Advisor** | `agy -p` 无状态只读进程，`gemini-4-neon-xhigh` | 开题设计把关（`design`）、关键决策权衡（`decide`）、终审/违例/连续≥2次 REVISE 的**单轨冷眼审计**（`audit`）、卡壳诊断（`unblock`） |
| **Monitor** | `agy -p` 隔离只读进程，`gemini-4-hydrogen-xhigh` | 常规 `REPORT` 卷宗的跨家族冷眼证据核对（偏离清单 `F1/F2...` + 记忆建议）；不改代码、不做设计拍板 |
| **Executor** | `claude -p`（`--safe-mode`），`opus` / `xhigh` | 唯一代码写入者；按简报改代码、跑验证、追加 `.baton/log.md`、交大修改 `R*.html` 汇报并输出结构化 `leg-result` |

> **单轨冷眼铁律**：每份封存卷宗只由**一位**冷眼角色审计——常规 `REPORT` 归 Hydrogen Monitor，终审（`DONE`）/ 违例（`VIOLATION`）/ 连续 ≥2 次 `REVISE` 自动直达 Neon Advisor（跳过 Monitor，既省 `agy` 配额又防锚定）。
> **降努力度不降制度**：`agy` 5h 额度吃紧时，执行 `baton preset use research-conserve` 把 Monitor/Advisor 降到 `high`，绝不取消独立冷眼边界。

## 启动一项任务

在项目目录运行，先确认环境、配置与基底：

```bash
baton doctor
baton config
baton init                    # 首次安装；默认生成 v3 research 预设（含 research-conserve、v2-codex）
baton memory install          # 可选，首次安装 mem0 + Ollama 本地语义记忆
baton new <task>
```

1. **编写 `brief.md` 与 `baton-scope`**：写明实验/开发目标、允许/禁止路径、锁定验收测试与验证命令：
   ```markdown
   ```baton-scope
   {"allow":["src"],"deny":["data"],"acceptance":["tests"],"frozen":[],"unfreeze":[],"verify":"auto"}
   ```
   ```
2. **开题设计门控（研究/复杂任务强烈建议，默认开启 `advisor.design_gate`）**：
   ```bash
   baton advise design <task> [--files 关键文件...]
   ```
   读 `.baton/advice/A001.md`，把缺失的防泄露不变量、需冻结的评测脚本和止损条件补进 `brief.md`（见 [`references/advisor.md`](references/advisor.md)）。
3. **审定 `harness.md`**：删除仅面向 Conductor 主会话的流程指令，运行 `baton harness approve <task>`。Claude Executor 通过 `--append-system-prompt` 注入审定后的 harness（不脏化仓库里的 `CLAUDE.md`）。
4. **启动并后台等待**：
   ```bash
   baton start <task>
   baton wait <task>          # 在后台运行并结束本回合，不要前台轮询
   ```

## 事件处理

事件先读对应 references，再读 `baton wait` 输出的卷宗、监督简报或路径。严禁跳过冷眼简报直接裁决。

| 事件 | 先读 | 处理 |
|---|---|---|
| `REPORT` | `references/review.md`、监督简报（Monitor 或连续 REVISE 升级后的 Advisor）、`baton diff --since report` | 逐条写 verdict，`baton rule` 入账后按结论 `resume` |
| `DONE` | `references/review.md`、终审简报（默认由 **Advisor Neon XHigh** 单轨直审） | 写 `ACCEPT`/`REVISE`，通过后 `baton close` |
| `VIOLATION` | `references/review.md`、违例简报（默认由 **Advisor Neon XHigh** 单轨直审） | 只选 `REVERT`、`ADOPT`、`ASK_USER`；Claude 下轮自动重置新会话 |
| `DECISION` | `references/decision.md`、决策请求 | 重大路线/口径决策先跑 `baton advise decide <task> --decision D001`，再 `baton decide --advice A<n>` 并 `resume` |
| `BLOCKED` / `FAILED` | stderr、stream-json 尾迹、`references/executor-protocol.md` | 方法卡壳可跑 `baton advise unblock <task> -q "..."`；容量/网络问题确认恢复后 `resume`，不接手写代码 |
| `DRIFT` / `BREAKING` / `TICK` | `references/supervision.md` | L1 0-token 守卫（冻结校验 + stream-json 报错循环/权限拒批检查）常驻运行；处理告警后 `steer` 或 `supervised` |
| `MONITOR_FAILED` | `references/monitor.md`、stderr | 重跑 `baton monitor <task> <dossier> --rerun` 或升级 `baton advise audit <task> --dossier <dossier> --rerun` |
| `IDLE` | `baton status` | 没有运行中轮次，确认是否 `start`/`resume`/`close` |

## 裁决、续跑与修正

监督简报（无论由 Hydrogen Monitor 还是 Neon Advisor 产出）均采用统一的 `F1/F2...` 偏离结构与 `baton-verdict` 裁决门槛：

```markdown
```baton-verdict
{"conclusion":"APPROVE","findings":{"F1":{"decision":"reject","reason":"证据与章程一致"}}}
```
```

```bash
baton rule <task> <dossier> <verdict-file>
baton resume <task> --verdict <dossier>
```

放宽冻结、`ADOPT` 违例或豁免验证仍必须由用户在自己终端加 `--user-approved` 确认（`baton amend ... --user-approved`、`baton close --waive ... --user-approved`）。

## 预设、配额与 Statusline

- **切换预设**（`agy` 配额吃紧时降努力度保隔离）：
  ```bash
  baton preset list
  baton preset use research-conserve [--task <task>]
  baton preset use v2-codex          # 需要切回 v2 Codex 栈时
  ```
- **同步 `agy` Statusline**（查看 model、effort、context、5h/7d 剩余额度与 Baton 状态）：
  ```bash
  baton statusline install --agy
  ```
- **清理状态目录**：`baton gc --dry-run` / `baton gc`。

## 命令速查

| 命令 | 作用 |
|---|---|
| `baton doctor` | 检查 `agy` / `claude` / `codex` CLI、记忆、台账与隔离配置 |
| `baton config [--json]` | 查看合并后的 v3 配置（含 conductor / advisor / monitor / executor） |
| `baton init` | 初始化项目并生成 v3 `baton.json` 与内置预设 |
| `baton new <task>` / `baton harness approve <task>` | 新建任务并审定 `harness.md` |
| `baton advise {design\|decide\|audit\|unblock} <task>` | 无状态调用高阶 Advisor（默认 Neon XHigh）并落盘入账 |
| `baton start/resume/steer/wait <task>` | 启动、按裁决/决定续跑、纠偏、后台等待 |
| `baton rule <task> <dossier> <file>` | 逐条回应冷眼审计简报并登记裁决 |
| `baton decide <task> <D号> <file> [--advice A号]` | 冻结决策请求并登记指挥决定 |
| `baton preset list` / `baton preset use <名>` | 切换 `research` / `research-conserve` / `v2-codex` 预设 |
| `baton statusline install [--agy]` | 安装 Claude Code 或 Antigravity CLI 状态栏 |
| `baton close <task>` / `baton gc` | 终审收尾并清理卷宗代码与临时文件 |
