# 181-side runner files

These files are synced to `~/baton-rsibench-20261025/runner/`. They use the
loopback reverse tunnel created by `evaluation/rsibench_codex_relay.py`.

The remote process never receives the Codex token. It sends prompts to the
local relay over `127.0.0.1:18765`; the relay invokes the local Codex CLI.
