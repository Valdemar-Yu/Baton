<p align="center"><img src="assets/brand/baton-v2-logo.png" alt="Baton v2" width="240"></p>

# Baton

Baton 让 Codex 主会话负责决策和监督，让 Codex executor 执行代码，并把每一轮的证据、裁决和记忆留下来，便于安全续跑。Hermes backend 仍可由项目配置显式启用。

## 摘要

让一个强模型指挥、另一个模型写代码，是 Claude Code 里常见的分工，难点在指挥手里的证据。v1 里 Opus 审的是执行者自己写的汇报，监督靠每 30 分钟读一次快照。执行者改了验收测试或者偏离了简报，指挥往往要等汇报交上来才知道，而那份汇报正是执行者写的。

Baton v2 把这件事拆给三个角色，并把能机械检查的约束交给程序。conductor 是 Codex 主会话，也是唯一的决策者，默认使用 gpt-6-astra、ultra 和 Fast。executor 默认使用 Codex CLI 的 gpt-6.1-sol、ultra 和 Fast。monitor 是隔离的只读进程，默认使用 gpt-5.6-luna 的最高可用 effort max；它只读 baton 封存的卷宗，里面有完整 diff、本轮终点的代码快照和 baton 亲跑的逐条测试结果，逐条列出偏离。conductor 必须对每条偏离写明采纳或驳回，裁决和监督简报一起写进哈希链账本，没有绑定当前卷宗的裁决，续跑就被拒绝。验收测试和测试配置在任务开始时冻结，runner 每 60 秒核对一次，越界即停。轮中监督由规则信号和 monitor 的退避式方向检查组成，conductor 只在出现偏离时被叫醒。

这些取舍有两方面依据。council.skill [1] 在 SWE-bench Lite dev 的 23 个任务上报告，Sonnet 主 agent 引入多模型合议后，解决数从 8 升到 12 且没有回归，效果好于单一顾问。v2 因此让 monitor 默认用和 conductor 不同家族的模型，两边读同一份证据，各自的盲区不重合。记忆方面，Mem0 [2] 表明按需抽取和检索的记忆层相比把全部历史塞进上下文，p95 延迟降低 91%，token 成本降低九成以上。DolphinBench [3] 用真实任务考察记忆，指出最难的一步是 agent 意识到自己需要回忆，效果取决于模型、harness 和记忆系统的组合。Baton 据此每轮主动推送 10 条相关记忆和一个常驻块，执行者只拿到只读检索工具，写入由 monitor 提炼、conductor 核准。存储用 mem0 加本地 Ollama 嵌入，数据留在本机，台账为准，向量库随时可以重建。

v2 由 v1 的 Baton 自举开发，带 239 个单元测试和 5 个连接真实 mem0、Ollama 与 MCP 的集成测试。真实 Hermes 执行者加真实 monitor 的端到端运行，从开工到 close 全流程通过。完整设计见 [docs/v2-design.md](docs/v2-design.md)。

## 三个角色

<p align="center"><img src="assets/brand/baton-v2-banner.png" alt="conductor、executor、monitor" width="420"></p>

- **conductor**：Codex 主会话，默认 `gpt-6-astra` / `ultra` / Fast，唯一决策者；写简报、定义范围、读取监督简报、裁决和最终验收。
- **executor**：Codex CLI，默认 `gpt-6.1-sol` / `ultra` / Fast；按简报改代码和验证。
- **monitor**：独立只读 Codex 进程，默认 `gpt-5.6-luna` / `max`；读取卷宗和快照写监督简报，不改代码、不替 conductor 做决定。

记忆由 mem0、本地 Ollama 和 `baton-qwen3-embed:latest` 提供。Ollama 离线时会退回关键词检索并记录原因。

## 两种模式

`baton.json` 里的 `mode` 取 `standard`（默认）或 `lean`。lean 是极限省 token 的模式，只保留一种交互，executor 把活干完，conductor 审一次。

| | standard | lean |
|---|---|---|
| executor | gpt-6.1-sol ultra，Fast | gpt-5.6-luna medium，不升级 |
| conductor 思考深度 | 不限 | medium（`baton` 管不了主会话，事件里会提示用户调整） |
| monitor 与监督简报 | 每份汇报一份，裁决须逐条回应 | 不设 monitor，不生成卷宗 |
| 轮中监督 | 规则信号加 monitor 方向检查，DRIFT/BREAKING 叫醒 conductor | 关闭，规则信号只附在轮末事件里 |
| 汇报与决策请求 | 大修改交汇报、遇到决策停下等裁决 | 一轮做到底，自己拿主意写进 log，做完交一份汇报 |
| conductor 出场 | 每份汇报、决策请求和偏离事件 | 只在 done、blocked、failed 时审一次 |
| 记忆 | 推送、monitor 提炼候选、定期整理回合 | 照常推送，候选由 conductor 审查时核准，没有整理回合 |

两种模式都保留不花模型 token 的部分，包括冻结与 60 秒核对、baton 亲跑验证、影子仓库和回滚点、容量自动重试、额度提醒。切到 lean 只要在项目根的 `baton.json` 写一行：

```json
{ "mode": "lean" }
```

项目层里显式写的键仍然优先，比如在 lean 下单独把 `executor.reasoning_effort` 设成 `high` 也会生效。

手写 `baton.json` 时记得带上 `"executor": {"backend": "hermes"}`。没有这个键的文件按 v1 兼容规则走 codex 后端；`baton init` 生成的文件已经写好了。

## 安装

需要 Claude Code、Python 3.9+、git、已登录的 Codex CLI，以及 Hermes。

```bash
git clone https://github.com/Valdemar-Yu/Baton.git
cd Baton
./install.sh
```

安装脚本只建立 skill/CLI 链接并检查依赖，不会替用户写家目录配置。准备本地 Ollama 和嵌入模型：

```bash
ollama serve
ollama pull qwen3-embedding:0.6b
```

如果 Ollama 没有 `ollama` 命令，按系统说明安装；`install.sh` 会提示缺失但仍完成其他安装。随后在用户自己的终端执行：

```bash
baton memory install
baton init
hermes -p baton-exec-v2 auth add openai-codex --type oauth
hermes -p baton-exec-v2 auth list openai-codex
hermes -p baton-exec-v2 auth remove openai-codex <根目录带进来的旧条目>
```

`baton init --upgrade-profile` 只在确认要覆盖 profile 的 SOUL.md/config.yaml 时使用。运行 `baton doctor` 检查 Hermes、Ollama、台账和 monitor 隔离。

## 一次任务

在目标项目里由 Claude 主会话执行：

```text
/baton 给 parser 加流式解析，保持接口兼容，tests/ 全部通过
```

conductor 会先运行 `doctor`、`config`，再 `baton new <task>`。第一次使用或 doctor 提示基底缺失/过期时，先运行 `baton harness base --init`，把 `~/.claude/CLAUDE.md` 改写成只供 executor 使用的基底，再运行 `baton harness base --approve`；之后每个任务都会自动带入这份基底。它写 `.baton/tasks/<task>/brief.md`（验收、允许/禁止路径、验证命令和 `baton-scope` 范围块），并审改 Baton 自动起草的 `.baton/tasks/<task>/harness.md`：只留这个项目的长期事实，删掉主会话专用的流程指令和上层目录的无关内容。审定后再启动：

```bash
baton harness approve <task>
baton start <task>
baton wait <task> &
```

`harness approve` 记录内容哈希；harness 没审定、审定后又被改过，`start` 都会拒绝。executor 拿到的项目指令只来自这份 harness：Hermes 渲染成启动目录下的 `.hermes.md`，Codex 渲染成 `AGENTS.override.md`，两者都写进 `.git/info/exclude`，任务 close 时清理。每个任务有独立的 Hermes home 和 Codex home，会话、记忆不跨任务共享，skills 共享。

项目根就是运行 `baton` 的目录。当前目录没有 `baton.json`、上层目录有时，`new`/`start` 会拒绝，提示先在当前目录 `baton init`（或显式传 `--root`），避免子目录任务落到上层项目、带上上层的 AGENTS.md 和记忆。executor 的工作目录也是这个目录。

executor 每次小修改追加 `.baton/log.md`；大修改写 `.baton/reports/R*.html` 后暂停。monitor 生成独立监督简报，conductor 逐条写 `baton-verdict`，运行 `baton rule`，通过门槛后才 `baton resume --verdict`。任务结束要经过终审和 `baton close`。容量错误会自动立即重试 3 次，再延迟 15 分钟重试 1 次；用尽后产生 FAILED，由 conductor 决定换模型、拆分或询问用户。

## 状态与卷宗存储

项目外状态默认在 `~/.baton/projects/<project-id>/`，包括影子仓库、任务状态、卷宗和账本。
设置环境变量 `BATON_HOME` 可以换到容量更大的磁盘，例如 macOS/Linux 的
`export BATON_HOME=/Volumes/Data/baton`，或 PowerShell 的 `$env:BATON_HOME = 'D:/baton'`。
该目录须在项目之外；更换位置时，先停止运行中的任务，再把原状态目录的内容移到新位置，后续命令保持相同设置。

卷宗的 `code/` 从影子快照复制，省略项目自身 git ignore 规则忽略的未跟踪文件；项目跟踪的文件仍保留。
非 git 项目保持完整复制。`manifest.json` 中的 `code_omissions` 记录省略文件数、字节数和原因。
同一任务同一轮的轮中检查只保留最新检查卷宗的 `code/`，每次新检查会删除旧检查的代码副本。
`baton close` 删除该任务所有卷宗的代码副本；其余记录、原始文件清单和账本保留，manifest 另记 `code_pruned`。
已清理的卷宗可核验保留的记录，但不能重跑 monitor。影子仓库的回滚快照保留。

清理当前项目中此前已关闭任务的卷宗代码：

```bash
baton gc --dry-run
baton gc
```

命令打印卷宗数量、文件数和字节数；未关闭任务的卷宗保留。

## 从 v1 迁移

旧项目先备份并迁移项目根的配置：

```bash
baton init --migrate
```

迁移会保留 `baton.json.v1.bak`，把 `judge` 改为 `conductor`/`monitor`，并把执行者默认切到 Hermes。旧项目没有 `executor.backend` 时仍按 codex 运行，方便逐项目迁移。

项目指令也要更新，例如 `~/yuzz/CLAUDE.md` 或项目内 `CLAUDE.md`：删除“派子 agent 设为 judge.model 当裁判”的 v1 说法，改为 `conductor.model`、独立 monitor、监督简报和 `baton rule`。`baton doctor` 发现旧说法会报 WARN，不会阻挡旧项目运行。

## 常见问题

**容量重试为什么等待？** 这是模型服务容量错误，不是代码失败。Baton 立即重试 3 次，再等待 15 分钟重试一次；全部失败后停止本轮，等 conductor 处理。

**monitor 为什么可能带着 Codex 全局指令？** Codex monitor 使用只读参数，但 `~/.codex/AGENTS.md` 是否被 CLI 读取由 CLI 版本决定。`baton doctor --monitor-probe` 会探测并在卷宗标明；monitor 只读，不会执行其中的写入要求。executor 不受这个问题影响：它用任务自己的 CODEX_HOME（不含全局 AGENTS.md），并以 `baton.json` 作为 project root marker，上层目录的 AGENTS.md 不会注入。

**旧任务会受 harness 门禁影响吗？** 不会。升级前创建、没有 `harness.md` 的任务沿用原来的 CODEX_HOME/HERMES_HOME，可以照常 resume。

**Hermes 没有 sandbox-exec 怎么办？** 在项目 `baton.json` 显式写：

```json
{"executor": {"sandbox_exec": false}}
```

随后由 `baton doctor` 显示退路状态；仍应保留项目范围和冻结检查。

**Ollama 离线会不会卡住任务？** 不会。记忆检索退回关键词，返回中带 fallback 原因；恢复 Ollama 后可运行 `baton memory reindex` 补建语义索引。

**安装脚本遇到已有目录怎么办？** 普通目录不会覆盖。已有软链接（包括旧的 `~/.local/share/baton-v1/baton`）会改指向当前仓库并打印变更；普通目录会跳过并提示手动处理。

## 参考文献

1. ParadoxZW. *council.skill*. GitHub, 2026. <https://github.com/ParadoxZW/council.skill>
2. Prateek Chhikara, Dev Khant, Saket Aryan, Taranjeet Singh, Deshraj Yadav. *Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory*. arXiv:2504.19413, 2025. <https://arxiv.org/abs/2504.19413>
3. Soumil Rathi, Deshraj Yadav, Taranjeet Singh. *DolphinBench: Mapping the Pareto Frontier of Agent Memory*. arXiv:2609.24971, 2026. <https://arxiv.org/abs/2609.24971>

## Windows

Native Windows 11 support uses Codex as executor and monitor, with local memory by default. Hermes is unsupported on Windows. See [WINDOWS.md](WINDOWS.md) for installation, process behavior, and v1/v2 coexistence.
