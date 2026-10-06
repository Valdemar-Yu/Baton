# Baton

Claude Code skill in which Claude Opus 5.5 conducts and judges while GPT-6.1-Sol, driven through the Codex CLI, writes the code. Opus writes the brief, makes the hard calls, reviews every major change and checks on the executor every 30 minutes.

Baton 是一个 Claude Code skill。Claude Opus 5.5 担任**指挥**，在终端里调用 Codex CLI，让 GPT-6.1-Sol 担任**执行者**写代码。Opus 负责写任务简报、拍板关键决策、审阅每一次大修改，并且每 30 分钟检查一次执行者有没有跑偏、有没有把代码改坏。执行者每完成一处小修改就记一条日志，每完成一次大修改就交一份 HTML 汇报，然后停下来等 Opus 审阅。

这个仓库就是 skill 本体。装一次以后，在任何项目里对 Claude Code 说一句 `/baton <任务>` 就能用，Baton 的运行记录写在那个项目的 `.baton/` 目录里。

## 分工

| 角色 | 模型与设置 | 负责 |
|---|---|---|
| 指挥 | Claude Opus 5.5，Claude Code 主会话 | 简报、决策、审阅汇报、30 分钟监管、最终验收 |
| 执行者 | `gpt-6.1-sol`，思考深度 `xhigh`，`service_tier="default"` 并禁用 `fast_mode` | 按简报改代码、跑验证、记日志、写汇报、提决策请求 |
| 顾问团 | [council.skill](https://github.com/ParadoxZW/council.skill)，chief 为 Opus，顾问默认为 Opus 与 GPT-6.1-Sol | 重大决策时给 Opus 提供独立意见 |

执行者的每次运行称为一**轮**（leg）。首轮是 `codex exec`，之后每轮都是带着指挥消息的 `codex exec resume <thread>`，所以执行者始终在同一个 Codex 会话里，前面的上下文都在。每轮结束时，执行者按固定的 JSON schema 交回 `done`、`decision`、`report` 或 `blocked` 四种状态之一，Baton 据此唤醒 Opus 做对应的事。

```
Opus 写简报 ── baton start ──▶ Codex 执行（独立进程，会话关闭也不中断）
     ▲                              │
     │        baton wait（后台）◀───┤ 本轮结束 / 满 30 分钟 / 额度低于 5%
     │                              │
     └── 审阅汇报 · 拍板决策 · 纠偏 ── baton resume ──▶ 同一个 Codex 会话继续
```

## 安装

需要 Claude Code、已登录的 Codex CLI（在 0.160 上测试）、Python 3.9 以上和 git。macOS 与 Linux 可用，桌面通知目前只在 macOS 上发。

```bash
git clone https://github.com/Valdemar-Yu/Baton.git && cd Baton
./install.sh            # skill 链接到 ~/.claude/skills/baton，CLI 链接到 ~/.local/bin/baton
./install.sh --council  # 安装 council.skill，并写入 Baton 用的顾问配置
baton doctor            # 自检
```

skill 是软链接，`git pull` 之后立即生效，不用重装。

`--council` 不会覆盖已有的 council 安装和 `~/.local/bin/council-def.sh`。Baton 自带的顾问定义不需要任何 API key，Opus 顾问走 Claude Code 订阅，GPT-6.1-Sol 顾问走本机 Codex 登录。

> [!IMPORTANT]
> GPT-6.1-Sol 顾问和执行者共用同一份 Codex 额度。额度紧张时，把 `~/.claude/skills/council/config.json` 的 `councilors` 改成只留 `council-cc-opus`。

`install.sh` 遇到冲突时不覆盖任何东西。如果 `~/.claude/skills/baton` 已经是一个普通目录，或者 PATH 上已有另一个叫 `baton` 的程序，它会跳过、提示你先处理，并以非零状态退出。

## 使用

在 Claude Code 里打开目标项目，用 Opus 5.5 会话直接交代任务，例如

```
/baton 给 parser 加流式解析，接口保持兼容，tests/ 里的用例全部要过
```

Opus 会先读代码、写出 `.baton/tasks/<task>/brief.md`（目标、可检查的验收标准、允许与禁止修改的路径、验证命令），启动执行者，然后在后台运行 `baton wait`。之后不需要你盯着，Opus 被下面这些事件唤醒。

| 事件 | 触发 | Opus 的处理 |
|---|---|---|
| `TICK` | 距上次监管满 30 分钟 | 读 `baton status` 快照，对照简报判断 on_track、drifting 或 breaking，必要时纠偏或叫停 |
| `REPORT` | 执行者完成一次大修改并写了 HTML 汇报 | 用 diff 和验证命令核对汇报，写审阅意见，APPROVE 或 REVISE 后续跑 |
| `DECISION` | 执行者遇到需要拍板的问题 | 常规问题直接定；重大问题召集 council；需求取舍问你 |
| `DONE` | 执行者认为任务完成 | 对照验收标准逐条验收，向你汇报 |
| `QUOTA_LOW` | Codex 额度剩余低于 5% | 在回复里提醒你，任务照常进行 |
| `PAUSED_UNSUPERVISED` | 超过 70 分钟没有监管 | 执行者已被自动暂停，Opus 先检查再续跑 |

## 执行者遵守的协议

首轮消息附带完整的 `references/executor-protocol.md`，之后每轮消息末尾都有一句协议提醒。协议的核心是三条。

**小修改**指单一目的、少于 4 个文件且增删少于 200 行、不动公共接口和依赖的改动。每完成一处，执行者在 `.baton/log.md` 末尾追加一条记录，写明文件和行数、改了什么、为什么、怎么验证的，然后继续工作。

**大修改**指新功能、跨模块重构、接口或依赖或数据格式的变化，以及自上次汇报以来累计达到阈值的改动。执行者按 `templates/report.html` 写 `.baton/reports/R<NNN>-<slug>.html`，列出改动清单、设计取舍、实际跑过的验证命令和结果、风险，以及最希望指挥检查的位置，然后结束本轮等待审阅。

**决策请求**用于多个方案会影响架构、接口或难以回退，简报有歧义，或者同一问题连续修 3 次没修好的情况。执行者写 `.baton/decisions/D<NNN>-<slug>.md` 后停下，Opus 把决定写进同一文件末尾，再续跑。

协议还禁止执行者提交或改写 git 历史、删改 `.baton/` 里已有的记录，以及用删测试、放宽断言、改 CI 的方式让检查通过。阈值可以在配置里调整。

## 监管与回滚

监管的计时从每轮开始或上一次监管算起。到点后 Opus 先跑 `baton status`，看执行者这段时间跑过哪些命令、退出码是多少、改了哪些文件，再看相对任务起点和上次监管的 diff 统计。被删除的文件、被改动的测试文件，以及构建、依赖、CI 文件会单独标出来，log.md 的新增内容也列在最后。Opus 据此给出结论，用 `baton supervised` 记入 `.baton/supervision.md`。

执行者跑在独立进程里，关掉 Claude Code 不会打断它。为了不让它长时间无人看管，超过 70 分钟没有监管时它会被自动暂停，等 Opus 回来检查后再继续。

每轮开始和结束、每次监管时，Baton 都把工作区（已跟踪和未跟踪的文件）存成 `refs/baton/<task>/...` 下的一个 git 提交。它用临时索引生成，不改动你的分支、HEAD 和暂存区。被 `.gitignore` 忽略的文件和子模块内部的改动不在回滚点里。`baton rollback <task> <ref> --yes` 把工作区文件恢复到任一回滚点，HEAD、分支、暂存区和 `.baton/` 下的记录都不动。执行前当前状态先存成一个新回滚点，所有要改写或删除的文件再原样复制一份到 `.baton/rollback-backup/`，被忽略的文件和经过过滤器的文件因此也能找回。删除和写入都不会穿过符号链接落到仓库外面，也不会进入子模块。Opus 只在你同意后执行回滚。

## Codex 额度

Baton 通过 `codex app-server` 的 `account/rateLimits/read` 读取账户额度。这个调用只查账户状态，不消耗模型 token。结果缓存 120 秒，app-server 不可用时退回到最近一次 Codex 会话记录里的额度数据。

Opus 在一个项目里第一次运行 `baton init` 时，会把这个项目的 statusline 配成两行。第一行是你原来的 statusline。Baton 的脚本按 Claude Code 的设置优先级找到它，再原样调用，所以 terminal-label 这类顺带改终端标题的 statusline 照常工作。第二行是 Codex 额度。

```
Codex gpt-6.1-sol·xhigh │ 7d 剩余 92% █████████░ ↻3d18h │ credits 62500 │ 重置券 1
```

配置写在项目的 `.claude/settings.local.json`，它的优先级高于用户级设置，只影响这个项目；路径是本机的，Baton 会把它加进 `.git/info/exclude`，不会被提交；如果这个文件已经被 git 跟踪，Baton 默认不改它，确实要装就用 `baton statusline install --force`。Claude Code 会热加载设置文件，项目原本就有 `.claude/` 目录时当前会话直接生效，目录是新建的就需要重开一次会话。`baton statusline uninstall` 可以恢复原样，`baton init --no-statusline` 则完全不碰 statusline。statusline 命令指向 `~/.claude/skills/baton` 下的脚本，所以挪动或重新克隆仓库后重跑一次 `install.sh` 就行；如果要彻底删掉 Baton，先在用过它的项目里运行 `baton statusline uninstall`，否则这些项目的 statusline 两行都会消失。

剩余低于 5% 时，这一行前面出现红底的「Codex 额度低于 5%」，macOS 上每个额度窗口弹一次桌面通知，`baton start`、`resume`、`status`、`quota`、`doctor` 的输出和每一轮结束的事件里都会带 `BATON::QUOTA_LOW`，后台的 `baton wait` 在一轮运行期间每 5 分钟查一次，同一个窗口第一次跌破阈值时提前唤醒 Opus。Opus 看到后会在回复里提醒你。statusline 只读缓存，过期时在后台刷新，渲染一次约 0.15 秒。

> [!NOTE]
> 两行显示已在 Claude Code 2.1.289 的交互界面里实测。statusline 每 60 秒刷新一次，沿用你原有配置的 `refreshInterval`。

## 配置

默认值在 `skills/baton/config.json`。除 `quota` 一节外，项目都可以在 `.baton/config.json` 里覆盖；额度按账号计算，`quota` 只读 skill 自己的配置。

| 键 | 默认值 | 说明 |
|---|---|---|
| `executor.model` | `gpt-6.1-sol` | 执行者模型 |
| `executor.reasoning_effort` | `xhigh` | 该模型支持 low / medium / high / xhigh / max / ultra |
| `executor.service_tier` | `default` | 标准速度；Fast 档对应 `priority` |
| `executor.disable_fast_mode` | `true` | 运行时附加 `--disable fast_mode` |
| `executor.sandbox` | `workspace-write` | 执行者只能写项目目录 |
| `executor.network_access` | `false` | 需要装依赖或下载时设为 `true` |
| `supervision.interval_minutes` | `30` | 监管间隔 |
| `supervision.max_unsupervised_minutes` | `70` | 超时自动暂停，`0` 表示关闭 |
| `supervision.test_command` | 空 | 监管和审阅时运行的验证命令 |
| `quota.warn_remaining_percent` | `5` | 额度告警阈值 |
| `change_size.major_files` / `major_lines` | `4` / `200` | 大修改阈值 |

额度相关的两项也可以用环境变量临时覆盖，`BATON_QUOTA_WARN_PERCENT` 改告警阈值，`BATON_DESKTOP_NOTIFY=0` 关掉桌面通知。任务进行中改了阈值或验证命令，Baton 会在下一条发给执行者的消息里带上新的值。

## 项目内的文件

```
.baton/
  config.json             项目级配置
  log.md                  执行者的修改日志
  supervision.md          Opus 的监管记录
  tasks/<task>/           brief.md 简报、state.json 状态、steer-*.md 纠偏消息
  reports/R<NNN>-*.html   执行者的大修改汇报
  reviews/                Opus 的审阅意见与最终验收
  decisions/D<NNN>-*.md   决策请求与指挥决定
  runs/<task>/            每轮的 prompt、Codex 事件流、最终输出、stderr（已 gitignore）
```

## 命令

| 命令 | 作用 |
|---|---|
| `baton doctor` | 检查 codex、模型与思考深度、加速是否关闭、额度、council、git |
| `baton init` / `baton new <task>` | 初始化 `.baton/` 并配置项目 statusline，生成简报模板 |
| `baton statusline install/uninstall/status [--force]` | 单独管理项目 statusline 的 Codex 额度行 |
| `baton start <task>` | 首轮 |
| `baton resume <task> <file> --kind 决策/审阅/纠偏` | 带指挥消息续跑 |
| `baton wait <task>` | 阻塞到下一个事件，在后台运行 |
| `baton status <task>` | 监管快照 |
| `baton supervised <task> --verdict ... --note ...` | 记录监管结论并重新计时 |
| `baton steer <task> <file>` / `baton stop <task>` | 停止当前轮并纠偏续跑 / 只停止 |
| `baton diff <task> --since base/leg/tick/report` | 相对任务起点、本轮起点、上次监管、上次汇报的改动 |
| `baton refs <task>` / `baton rollback <task> <ref> --yes` | 回滚点列表 / 回滚 |
| `baton quota` | 查看 Codex 额度 |

## 致谢与许可

决策会商使用 [ParadoxZW/council.skill](https://github.com/ParadoxZW/council.skill)。Baton 以 MIT 许可发布。
