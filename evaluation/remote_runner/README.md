# 181-side runner files

These files are synced to `~/baton-rsibench-20261025/runner/`. They use the
loopback reverse tunnel created by `evaluation/rsibench_codex_relay.py`.

The remote process never receives the Codex token. It sends prompts to the
local relay over `127.0.0.1:18765`; the relay invokes the local Codex CLI.

Task containers default to direct device mappings for `/dev/nvidia6` and
`/dev/nvidia7`, with `CUDA_VISIBLE_DEVICES=6,7`. Override with
`RSIBENCH_GPU_DEVICES` when launching the runner.
