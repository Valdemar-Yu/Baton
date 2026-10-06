# 审阅大修改汇报与最终验收

## REPORT：审阅汇报

1. 读事件里给出的汇报 HTML。
2. 核对汇报与事实：
   - `baton diff <task> --since report --stat` 对照汇报的改动清单。`--since report` 的起点是上一次交汇报那一轮的终点（没有就是任务起点），中间经过决策、纠偏的轮次都算在内；REPORT 事件里「自上次汇报以来」一行是同一个统计。diff 里的每个文件都应出现在汇报中，汇报没提到的文件要追问。行数以 baton 的统计为准，执行者在沙箱里只能拿到相对 HEAD 的累计数和新文件的 `wc -l`，数字对不上不算问题。
   - `baton diff <task> --since report` 读关键 diff，先看汇报里「请指挥重点审阅」列出的位置。
   - 自己跑一遍验证命令，结果应与汇报一致。
3. 判断：是否推进了简报目标、满足相应验收标准；设计取舍是否站得住；有没有范围外改动、接口破坏、测试被删弱；风险一节是否如实。
4. 写审阅到 `.baton/reviews/R<NNN>-review.md`（编号与汇报相同）：

   ```markdown
   # R<NNN> 审阅

   - 结论：APPROVE / REVISE / ROLLBACK
   - 核对：diff 与汇报是否一致；验证命令及结果

   ## 问题
   按严重程度排列，每条给出 文件:行号、问题、期望的改法。没有就写「无」。

   ## 给执行者的指令
   APPROVE：下一步做什么（可直接沿用汇报里的下一步）。
   REVISE：逐条列出必须修改的点，修完后是否需要再交汇报。
   ```

5. 处理：
   - APPROVE / REVISE：`baton resume <task> .baton/reviews/R<NNN>-review.md --kind 审阅`，后台 `baton wait <task>`。
   - ROLLBACK：先告诉用户原因和回滚点，用户同意后 `baton rollback <task> <ref> --yes`，再写新的指令续跑。
6. 告诉用户：审阅结论、主要理由、汇报路径。

## DONE：最终验收

1. `baton diff <task> --since base --stat` 和关键 diff，看整个任务的改动。
2. 对照简报的验收标准逐条检查，验证命令自己跑。
3. 写 `.baton/reviews/<task>-final.md`：每条验收标准是否满足、验证结果、遗留问题、回滚点（`baton refs <task>`）。
4. 有不满足的验收标准：写指令后 `baton resume <task> <文件> --kind 审阅`，继续守候。
5. 全部满足：向用户汇报完成了什么、验证结果、遗留问题、改动统计。不自行 git commit 或 push，用户要求时再做。
