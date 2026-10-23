#!/usr/bin/env bash
# launch.sh — launch the council chief as a Codex fork of the main session.
# Chief runs without a sandbox, matching Claude chief's
# --dangerously-skip-permissions: CLI authentication must remain accessible and
# councilors must be able to start their own sandboxes on macOS. Councilor
# read-only boundaries remain in their backends (Codex sandbox / Claude charter).
#
# The first positional argument is an explicit session id. When it is omitted,
# use the Codex variables exported by the current shell in the documented
# order. Do not fall back to a Claude session variable: a Codex process can
# inherit one from an outer Claude process, but it identifies the wrong thread.

set -uo pipefail

SID="${1:-}"
if [[ -z "$SID" ]]; then
  SID="${CODEX_THREAD_ID:-}"
fi
if [[ -z "$SID" ]]; then
  SID="${CODEX_SESSION_ID:-}"
fi
if [[ -z "$SID" ]]; then
  echo "launch.sh: no Codex session id (pass one as \$1 or set CODEX_THREAD_ID/CODEX_SESSION_ID)" >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$HERE/.." && pwd)"
CHIEF_PROC="$SKILL_DIR/references/chief.md"
CONFIG="$SKILL_DIR/config.json"
COUNCIL_SH="$SKILL_DIR/scripts/council.sh"

CHIEF_MODEL="gpt-6-astra"
CHIEF_REASONING_EFFORT="ultra"
if [[ -r "$CONFIG" ]]; then
  if command -v jq >/dev/null 2>&1; then
    _m=$(jq -r '.chief_model // empty' "$CONFIG" 2>/dev/null)
    _e=$(jq -r '.chief_reasoning_effort // empty' "$CONFIG" 2>/dev/null)
  else
    _m=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('chief_model') or '')" "$CONFIG" 2>/dev/null)
    _e=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('chief_reasoning_effort') or '')" "$CONFIG" 2>/dev/null)
  fi
  [[ -n "${_m:-}" ]] && CHIEF_MODEL="$_m"
  [[ -n "${_e:-}" ]] && CHIEF_REASONING_EFFORT="$_e"
fi

# launch.sh creates the round-1 query path so the chief does not have to guess
# a timestamp.
QUERY_PATH="$PWD/.council/main-$(date +%Y%m%d-%H%M%S).md"
mkdir -p "$PWD/.council" 2>/dev/null || true

CHIEF_ROLE="You are 'chief', an isolated council-orchestration subagent. The conversation you can see is INHERITED BACKGROUND ONLY: you did not write it, you must NOT continue it, must NOT address the user, and must NOT resume the main agent's task. You act solely as chief — run the council consultation per the procedure file you are pointed to. Never invoke the council skill or launch.sh recursively. Your final message must be ONLY the verdict document it specifies: a clear, actionable conclusion the main agent can act on directly — no wrapping text, no vague hedging."

TASK="Act as chief. Read and strictly follow this procedure file:
$CHIEF_PROC

Run the council with THIS script (use this exact absolute path; quote it as it may contain spaces):
$COUNCIL_SH

Save your curated round-1 query to EXACTLY this file (use shell redirection or another shell command; there is no editor tool):
$QUERY_PATH

From the inherited conversation, find what the main agent is about to commit to, about to declare done, or stuck on — the approach, interpretation, result, or recurring error that needs reviewing — and put THAT to the council, composing the full curated query yourself.

Run the consultation to completion per the procedure, and return a clear, actionable verdict the main agent can act on directly — not vague advice. Do not call this launcher or any other council skill from inside the chief run."

# fork accepts the session id and prompt as its two positional arguments.
# `codex exec fork --help` confirms the bypass flag is supported, so use it
# directly to avoid blocking CLI credentials or nesting macOS seatbelt sandboxes.
# Model/effort stay explicit; councilor backends retain their read-only boundaries.
exec codex exec fork \
  -m "$CHIEF_MODEL" \
  -c "model_reasoning_effort=\"$CHIEF_REASONING_EFFORT\"" \
  --dangerously-bypass-approvals-and-sandbox \
  --skip-git-repo-check \
  "$SID" "$CHIEF_ROLE

$TASK"
