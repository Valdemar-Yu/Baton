#!/usr/bin/env bash
# council.sh — run one council round (with retries) in the foreground.
#
# Based on council.skill by ParadoxZW, Copyright (c) 2026 ParadoxZW, MIT License.
# Codex-port differences: this copy reads its sibling Codex config, keeps the
# Baton councilor defaults (Claude Opus + Codex Sol), and is launched by the
# Codex chief instead of a Claude fork. The retry lanes and output contract are
# otherwise kept in sync with the upstream implementation.
#
# Fans a curated query out to all councilors in parallel (read-only CLI agents on different model
# backends), each in its own INDEPENDENT retry LANE (a background subshell owning that councilor's
# whole attempt loop): up to 4 retries with SHORT backoff 10s/30s/1m/2m, and lanes never wait on
# each other — a fast-failing councilor retries on its own clock while a slow sibling keeps working.
# EARLY GIVE-UP: an attempt whose stdout is < 512 B produced no real opinion (an error line at most
# — real opinions are KBs; the CLI prints the terminal API error to stdout in -p mode); after 3
# consecutive no-content attempts the lane stops early — that shape is a PERMANENT failure
# (quota/auth/4xx) that retrying can't fix. A sizable-but-failed output (mid-stream drop after real
# content) RESETS the counter — that shape is transient and gets the full budget. `notfound` (not
# on PATH) is not retried. No per-attempt watchdog: a HANGING councilor blocks its lane. The caller
# bounds total wall-clock via the Bash tool's own timeout — finishes in time → returns normally;
# exceeds it → auto-backgrounds, caller reaps lanes by PID.
#
# Usage:  council.sh <query_file> [out_dir]      (query fed to each councilor via stdin)
# Stdout: OUT=<tmp> / COUNCIL=<.council dir> / LAUNCHED <name> <lane-pid> <outfile> <statusfile>
#   / RETRY <name> (was <status> ...; attempt N/M in <s>s) / GIVEUP <name> (...)
#   / RESULT <name> <status> <outfile>, status ∈ ok|empty|failed (final status → <statusfile>).
#   Each opinion → ./.council/<codename>-<ts>.md (kept). Exit 0 if ≥1 ok.

set -uo pipefail

# Best-effort TTL sweep of stale temp files (never fatal).
_TMPROOT="${TMPDIR:-/tmp}"
find "$_TMPROOT" -maxdepth 1 -type f \( -name 'council-*.jsonl' -o -name 'council-*.txt' -o -name 'council-query.*' \) -mtime +1 -delete 2>/dev/null || true
find "$_TMPROOT" -maxdepth 1 -type d -name 'council-out.*' -mtime +1 -exec rm -rf {} + 2>/dev/null || true

# Councilor backends: shell functions sourced from council-def.sh (override path via COUNCILOR_LIB).
COUNCILOR_LIB="${COUNCILOR_LIB:-$HOME/.local/bin/council-def.sh}"
if [[ -r "$COUNCILOR_LIB" ]]; then
  # shellcheck disable=SC1090
  source "$COUNCILOR_LIB"
fi

# Councilor list from config.json (councilors[]); each must be a function in the lib above.
# Fall back to the built-in three. (jq if available, else python3; bash-3.2-safe array build.)
COUNCILORS=(council-cc-opus council-codex-sol)
_CFG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../config.json"
if [[ -r "$_CFG" ]]; then
  _list=()
  if command -v jq >/dev/null 2>&1; then
    while IFS= read -r _n; do [[ -n "$_n" ]] && _list+=("$_n"); done < <(jq -r '.councilors[]? // empty' "$_CFG" 2>/dev/null)
  else
    while IFS= read -r _n; do [[ -n "$_n" ]] && _list+=("$_n"); done < <(python3 -c "import json,sys
d=json.load(open(sys.argv[1])).get('councilors') or []
print('\n'.join(x for x in d if isinstance(x,str)))" "$_CFG" 2>/dev/null)
  fi
  [[ ${#_list[@]} -gt 0 ]] && COUNCILORS=("${_list[@]}")
fi

# Councilor prompt is LAYERED: a shared role/task + a per-backend capability note. Edit
# COUNCILOR_ROLE once and both backends track it; only the boundary text differs.

# Shared role + task (single source of truth).
COUNCILOR_ROLE="You are an external councilor invited to a consultation: give an independent second opinion on the query below, grounded in reading the working directory and reasoning. Respond with your analysis and a clear recommendation. Your output is only an opinion for the lead decision-maker to weigh — do not try to land any change on the user's behalf, and you never need to write your report to a file (your stdout is captured)."

# cc-* are NOT sandboxed (Bash can still write); the no-write boundary is on the model's honor — load-bearing.
COUNCILOR_CHARTER_EXTRA="You run on the user's machine with read access to the working directory and the ability to search the web, but you are NOT sandboxed — these limits are on your honor.
You MAY: read files in the working directory and the files named in the query; use WebSearch / WebFetch; and run a Bash command ONLY when you have confirmed it writes NOTHING to disk and is otherwise side-effect-free (no file / cache / artifact anywhere — not even a temp file; no git change; no heavy or long-running compute) — e.g. a read-only inspection. If a command or test would write to disk at all (most real test suites do — pytest caches, build artifacts), do NOT run it; reason from reading instead.
You MUST NOT: write any file to disk anywhere; change git (commit / push / add / checkout / rebase / reset / stash, or anything that writes .git); read credential / secret files outside the working tree (e.g. ~/.local/bin/council-def.sh, ~/.ssh, ~/.aws, any config holding a token / secret); install dependencies, start services or daemons; or run long / heavy commands. If unsure whether something is safe, do not run it."

# codex runs under `codex exec --sandbox read-only` (council-def.sh) — writes blocked at OS level; just name the mode.
CODEX_SANDBOX_NOTE="You are in a READ-ONLY sandbox: you can read files and run read-only commands, but any write / git change / side effect is blocked at the OS level. Just read, reason, and answer."

# Composed per-backend prompts.
COUNCILOR_CHARTER="$COUNCILOR_ROLE

$COUNCILOR_CHARTER_EXTRA"          # cc-* system prompt (via --append-system-prompt)
CODEX_PREAMBLE="$COUNCILOR_ROLE

$CODEX_SANDBOX_NOTE"               # codex: prepended to the query via stdin

COUNCILOR_FLAGS=(
  --allowedTools "Read" "Grep" "Glob" "WebSearch" "WebFetch" "Bash"
  --disallowedTools "Write" "Edit" "NotebookEdit"
  --append-system-prompt "$COUNCILOR_CHARTER"
)

# --- args ---------------------------------------------------------------------
if [[ $# -lt 1 ]]; then
  echo "usage: council.sh <query_file> [out_dir]" >&2
  exit 2
fi
QUERY_FILE=$1
if [[ ! -r "$QUERY_FILE" ]]; then
  echo "council.sh: query file not readable: $QUERY_FILE" >&2
  exit 2
fi
if [[ $# -ge 2 && -n ${2:-} ]]; then
  OUT=$2
  mkdir -p "$OUT" || { echo "council.sh: cannot create out_dir: $OUT" >&2; exit 2; }
else
  OUT=$(mktemp -d "${TMPDIR:-/tmp}/council-out.XXXXXX") \
    || { echo "council.sh: mktemp -d failed" >&2; exit 2; }
fi

# Per-round persisted opinions live in $PWD/.council/; .err/.status stay in the temp $OUT.
COUNCIL_DIR="$PWD/.council"
mkdir -p "$COUNCIL_DIR" || { echo "council.sh: cannot create $COUNCIL_DIR" >&2; exit 2; }
# Round tag: derived from the query filename, so a round's query and its opinions share ONE tag
# and pair up exactly in .council/ — main-<T0>.md -> cc-*-<T0>.md, and a cross-examination round
# main-<T0>-r2.md -> cc-*-<T0>-r2.md (launch.sh pre-generates <T0>; chief derives -rN by string
# edit, no wall clock involved). Fallback for arbitrary query names (manual runs): fresh date.
_qbase=$(basename -- "$QUERY_FILE")
_qre='^main-([0-9]{8}-[0-9]{6}(-r[0-9]+)?)\.md$'
if [[ "$_qbase" =~ $_qre ]]; then
  ROUND_TS=${BASH_REMATCH[1]}
else
  ROUND_TS=$(date +%Y%m%d-%H%M%S)
fi

# --- build per-councilor slots ------------------------------------------------
echo "OUT=$OUT"
echo "COUNCIL=$COUNCIL_DIR"

# Per-councilor parallel arrays (bash-3.2-safe: indexed, no associative/mapfile).
declare -a NAMES=() LABELS=() OUTFILES=() ERRFILES=() STATFILES=()
for name in "${COUNCILORS[@]}"; do
  label=$(basename -- "$name")
  codename=${label#council-}                   # council-cc-opus -> cc-opus
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "NOTFOUND $label"                      # not on PATH -> config dropout, NOT retried
    continue
  fi
  NAMES+=("$name")
  LABELS+=("$label")
  OUTFILES+=("$COUNCIL_DIR/$codename-$ROUND_TS.md")  # OPINION doc (persisted); overwritten on each retry
  ERRFILES+=("$OUT/$codename.err")
  STATFILES+=("$OUT/$codename.status")
done

# --- run: one INDEPENDENT retry lane per councilor ------------------------------
# Backoff (seconds) BETWEEN attempts → 4 retries after the initial try. Kept short on purpose:
# retrying can't fix a PERMANENT failure (quota/auth), so cap the wasted backoff (~3.7 min/lane).
# Lanes retry independently — no cross-councilor barrier: a fast-failing councilor never waits
# for a slow sibling before its next attempt.
BACKOFFS=(10 30 60 120)
MAX_ATTEMPTS=$(( ${#BACKOFFS[@]} + 1 ))         # 5 total: 1 initial + 4 retries
NOCONTENT_LIMIT=3    # consecutive no-content attempts before a lane gives up early
NOCONTENT_BYTES=512  # stdout below this = no real opinion (error line at most; real ones are KBs)

# run_lane <name> <label> <outfile> <errfile> <statfile>
# One councilor's WHOLE retry loop, run as a background lane. Writes the lane's FINAL textual
# status (ok|empty|failed) to <statfile>; streams RETRY/GIVEUP lines to stdout live.
# Attempt classification:
#   rc==0 && stdout non-empty            -> ok (done)
#   stdout < NOCONTENT_BYTES             -> no-content failure (error line at most) — counts
#                                           toward the early give-up limit
#   stdout >= NOCONTENT_BYTES but failed -> partial content then a mid-stream drop: transient
#                                           shape, RESETS the no-content counter, full budget
run_lane() {
  local name=$1 label=$2 outfile=$3 errfile=$4 statfile=$5
  local attempt=1 rc sz status nocontent=0 backoff
  while :; do
    case "$name" in
      council-codex-*)  # codex harness: preamble + query via stdin
        { printf '%s\n\n----- consultation query -----\n\n' "$CODEX_PREAMBLE"; cat "$QUERY_FILE"; } | "$name" >"$outfile" 2>"$errfile"
        rc=$? ;;
      *)                # claude code harness
        "$name" -p "${COUNCILOR_FLAGS[@]}" <"$QUERY_FILE" >"$outfile" 2>"$errfile"
        rc=$? ;;
    esac
    sz=$(wc -c <"$outfile" 2>/dev/null | tr -dc '0-9'); sz=${sz:-0}
    if [[ "$rc" -eq 0 && "$sz" -gt 0 ]]; then
      echo "ok" >"$statfile"; return 0
    fi
    if [[ "$rc" -eq 0 ]]; then status="empty"; else status="failed"; fi
    if [[ "$sz" -lt "$NOCONTENT_BYTES" ]]; then nocontent=$(( nocontent + 1 )); else nocontent=0; fi
    if [[ "$nocontent" -ge "$NOCONTENT_LIMIT" ]]; then
      echo "GIVEUP $label ($nocontent consecutive no-content attempts, last rc=$rc ${sz}B — permanent failure, not retrying)"
      echo "$status" >"$statfile"; return 1
    fi
    if [[ "$attempt" -ge "$MAX_ATTEMPTS" ]]; then
      echo "$status" >"$statfile"; return 1
    fi
    backoff=${BACKOFFS[$(( attempt - 1 ))]}
    echo "RETRY $label (was $status rc=$rc ${sz}B; attempt $(( attempt + 1 ))/$MAX_ATTEMPTS in ${backoff}s)"
    sleep "$backoff"
    attempt=$(( attempt + 1 ))
  done
}

# Launch every councilor's lane; print its PID. stdin/stderr are detached; stdout is KEPT so
# the lane's RETRY/GIVEUP lines stream into this script's output live. $! is the lane PID —
# killing it plus children (pkill -P) is how the caller reaps a still-running lane if the
# whole call gets auto-backgrounded.
for i in "${!NAMES[@]}"; do
  run_lane "${NAMES[$i]}" "${LABELS[$i]}" "${OUTFILES[$i]}" "${ERRFILES[$i]}" "${STATFILES[$i]}" </dev/null 2>/dev/null &
  echo "LAUNCHED ${LABELS[$i]} $! ${OUTFILES[$i]} ${STATFILES[$i]} (independent lane, up to $MAX_ATTEMPTS attempts)"
done

# Single barrier: all lanes have finished ALL their attempts. Foreground under the caller's
# Bash timeout. NOTE: no per-attempt watchdog — a HANGING councilor blocks its own lane only.
wait

# --- RESULT manifest (final lane statuses) -------------------------------------
any_ok=0
for i in "${!NAMES[@]}"; do
  st=$(cat "${STATFILES[$i]}" 2>/dev/null); st=${st:-failed}
  [[ "$st" == "ok" ]] && any_ok=1
  echo "RESULT ${LABELS[$i]} $st ${OUTFILES[$i]}"
done

if [[ "$any_ok" -eq 1 ]]; then
  exit 0
else
  echo "council.sh: all councilors failed after their retry budgets (no usable output)" >&2
  exit 1
fi
