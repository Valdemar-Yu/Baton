# Local Codex RSIBench driver

This driver keeps model calls on the local machine and uses 181 only for Docker
sandboxing and verification. It does not copy Codex/Hermes credentials to 181.

First run a dry check:

```bash
python3 evaluation/rsibench_local_codex.py \
  --task-id terminal-bench-adaptive-rejection-sampler --cell a0
```

A real pilot requires `--execute` and consumes local Codex quota:

```bash
python3 evaluation/rsibench_local_codex.py \
  --task-id terminal-bench-adaptive-rejection-sampler --cell a0 --execute
python3 evaluation/rsibench_local_codex.py \
  --task-id terminal-bench-adaptive-rejection-sampler --cell baton --execute
```

A0 invokes local `codex exec`. Baton initializes a local lean Baton project with
Codex as its executor, then syncs the resulting workspace to 181 before the
remote verifier runs. Results are stored under `.rsibench-local-runs/`.
