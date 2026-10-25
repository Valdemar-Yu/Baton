#!/usr/bin/env bash
set -euo pipefail
WORKSPACE="$1"
TASK_ID="$2"
BATON_ROOT="${BATON_ROOT:-$HOME/baton-rsibench-20261025/baton}"
BATON_BIN="$BATON_ROOT/skills/baton/bin/baton"
PROFILE="${HERMES_PROFILE:-baton-bench-exec}"
RUNNER_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$WORKSPACE"
if [ ! -d .git ]; then git init -q; git config user.email rsibench@localhost; git config user.name rsibench; git add -A; git commit -qm baseline; fi
if [ -n "${LOCAL_CODEX_RELAY_URL:-}" ]; then
  mkdir -p .rsibench-local-bin
  cat > .rsibench-local-bin/codex <<EOF
#!/usr/bin/env bash
exec python3 "$RUNNER_DIR/remote_local_codex_shim.py" "\$@"
EOF
  chmod +x .rsibench-local-bin/codex
  export PATH="$WORKSPACE/.rsibench-local-bin:$PATH"
  BACKEND=codex
  PROVIDER=local-codex-relay
else
  BACKEND=hermes
  PROVIDER="${HERMES_PROVIDER:-openai-codex}"
fi
cat > baton.json <<EOF
{
  "executor": {
    "backend": "$BACKEND",
    "hermes_profile": "$PROFILE",
    "provider": "$PROVIDER",
    "model": "${RSIBENCH_CODEX_MODEL:-gpt-6.1-sol}",
    "reasoning_effort": "${RSIBENCH_CODEX_EFFORT:-ultra}",
    "escalation": {"enabled": false}
  },
  "mode": "lean"
}
EOF
"$BATON_BIN" --root "$WORKSPACE" init --no-statusline --fresh >/tmp/rsibench-baton-init.log 2>&1
"$BATON_BIN" --root "$WORKSPACE" new "$TASK_ID" --brief "$WORKSPACE/rsibench_instruction.md" >/tmp/rsibench-baton-new.log 2>&1
"$BATON_BIN" --root "$WORKSPACE" start "$TASK_ID" >/tmp/rsibench-baton-start.log 2>&1
"$BATON_BIN" --root "$WORKSPACE" wait "$TASK_ID" >/tmp/rsibench-baton-wait.log 2>&1
