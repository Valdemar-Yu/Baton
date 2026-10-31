# Local Codex RSIBench driver

There are two modes.

The direct local driver runs the orchestration command locally and uses 181 for
Docker/verifier work:

```bash
python3 evaluation/rsibench_local_codex.py \
  --task-id terminal-bench-adaptive-rejection-sampler --cell a0 --execute
```

For the requested **remote-start mode**, keep a relay process running on the
local machine. It owns the local Codex account and opens a reverse SSH tunnel;
181 starts the benchmark command and sends prompts through that tunnel:

```bash
# local machine; keep this terminal alive
python3 evaluation/rsibench_codex_relay.py --remote 181 --remote-port 18765
```

Copy the printed token into the 181 shell, then run on 181:

```bash
ROOT=$HOME/baton-rsibench-20261025
export LOCAL_CODEX_RELAY_URL=http://127.0.0.1:18765
export LOCAL_CODEX_RELAY_TOKEN='PASTE_THE_PRINTED_TOKEN'
export LOCAL_CODEX_DIRECT=$ROOT/runner/remote_local_codex_direct.py
export BATON_COMMAND="$ROOT/runner/run_baton_cell.sh {workspace} {task_id}"

python3 "$ROOT/runner/rsibench_compat.py" \
  --root "$ROOT/compatible-snapshot" \
  --output "$ROOT/runner/a0-local-codex.json" \
  execute-one --task-id terminal-bench-adaptive-rejection-sampler \
  --cell a0 --execute

python3 "$ROOT/runner/rsibench_compat.py" \
  --root "$ROOT/compatible-snapshot" \
  --output "$ROOT/runner/baton-local-codex.json" \
  execute-one --task-id terminal-bench-adaptive-rejection-sampler \
  --cell baton --execute
```

The remote runner starts Docker and the verifier on 181. The local relay invokes
`codex exec` using the local OAuth account; no token is copied to 181. Results
are stored under the remote runner directory. Stop the local relay with
`Ctrl-C` after the run.

The 181-side runner defaults task containers to GPU devices `6,7` using explicit
Docker device mappings (the host does not have the NVIDIA Docker runtime/CDI
configured). Override with `RSIBENCH_GPU_DEVICES=...` on 181 if needed. The
local Codex relay itself does not use 181 GPUs.

If preflight reports `relay_unavailable` or `Connection refused`, restart the
local relay and keep that terminal open. On 181, verify the reverse tunnel before
starting a task:

```bash
curl -fsS -H "Authorization: Bearer $LOCAL_CODEX_RELAY_TOKEN" \
  "$LOCAL_CODEX_RELAY_URL/health"
```

`LOCAL_CODEX_RELAY_URL` must be the plain value
`http://127.0.0.1:18765`, without Markdown link brackets.
