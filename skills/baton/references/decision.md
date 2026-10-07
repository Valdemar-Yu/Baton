# 处理执行者的决策请求

触发：`DECISION` 事件，产物是 `.baton/decisions/D<NNN>-<slug>.md`。

1. 读请求文件。背景描述可能不完整，涉及具体代码时自己读相关文件核实。
2. 分类：
   - **常规决策**：答案明确、可逆、影响局部。直接决定。
   - **重大决策**：影响架构、公共接口、数据格式或依赖，事后难以回退，或你自己没有把握。读取生效配置；仅当 `judge.council_for_major_decisions=true` 时召集 council（见下），关闭时由主会话或指定 judge 子 agent 直接决定。
   - **超出授权**：需求取舍只有用户能回答，或涉及删除数据、花钱、对外发布。问用户，等回复后再决定。
3. 把决定写进请求文件末尾的「指挥决定」一节：结论；理由；执行要点（具体到文件、接口、先后顺序）；是否经过 council，经过就写 `.council/` 下的记录文件名。
4. `baton resume <task> <请求文件> --kind 决策`，后台 `baton wait <task>`。
5. 告诉用户：决定是什么，为什么。

## 使用 council

council skill（https://github.com/ParadoxZW/council.skill）把当前对话交给一组外部模型独立会商，再由 chief 汇总成裁断。它继承你的完整对话，所以调用前先把决策请求和相关代码读进上下文。

```bash
bash ~/.claude/skills/council/scripts/launch.sh   # council 装在别处时换成它的 scripts/launch.sh
```

不带参数，用 Bash 的后台模式运行，完成后读取 stdout 里的裁断（`## Verdict` 等四节）。一轮通常要几分钟到十几分钟。

裁断是参考，决定由你做。裁断与你已掌握的证据冲突时，按 council skill 的约定再召集一次，把冲突点讲清楚。

council 的 Codex 顾问会消耗 Codex 额度。召集前看一眼 `baton quota`，额度低于阈值时优先只用 Claude 顾问（改 council 目录下 `config.json` 的 councilors 前先问用户）。没有安装 council 时直接自己决定，并在「指挥决定」里注明。
