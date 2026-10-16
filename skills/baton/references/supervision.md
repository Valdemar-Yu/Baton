# 轮中监督

v2 有三层监督，conductor 只在出现偏离时介入：

1. `baton` 每轮约 60 秒检查冻结文件、测试删弱、吞异常、停滞、重复失败、无汇报的大改动和外部命令，命中就记 `ALERT`。即使测试文件通过 scope 的 `unfreeze` 解除整文件冻结，删除测试、减少测试数量、跳过测试等测试完整性 ALERT 仍然检查。
2. monitor 在第 10 分钟、改动首次超过 3 个文件、出现 ALERT 或上次结论为 drifting 时做方向检查；on_track 自动继续，drifting 发 `DRIFT`，breaking 立即结束并发 `BREAKING`。
3. monitor 连续失败两次时退回 v1 的 TICK 兜底；monitor 关闭或 lean 模式也只保留 TICK 说明。`baton wait <task>` 输出 TICK 时先读 `baton status <task>`。

收到 `DRIFT`：读轮中简报，每条偏离都要处理。认可方向问题就写消息并 `baton steer <task> <message-file>`；认为偏离不成立就 `baton supervised <task> --dismiss F1 --reason "..."`，不能直接忽略。

收到 `BREAKING`：先 `baton stop <task>`（若已停会打印已有事件），核对 `baton diff <task> --since leg` 和回滚点，再决定 steer、恢复冻结文件或请求用户。未处理的 DRIFT 到期会结束本轮，不能用 resume 跳过简报。
