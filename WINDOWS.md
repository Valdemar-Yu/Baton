# Baton v2 on native Windows

Validated on Windows 11 with Python 3.12. Requires Python 3.9+, Git for Windows (including Git Bash), and a native
`codex.exe` on PATH. The Python entry point and `bin/baton.cmd` work in
PowerShell; `bin/baton` works in Git Bash. No npm shell shim is used for Codex
or Git. `BATON_CODEX_BIN`, when used by the quota reader, must resolve to an
`.exe`, not a `.cmd` or `.ps1` file.

Windows defaults to `executor.backend: codex` and `memory.backend: local`.
Project settings can explicitly select mem0, but it needs a separately installed
memory environment and a working Ollama embedding model. Local memory keeps the
ledger and keyword retrieval; doctor reports WARN rather than FAIL.

Windows also supports a native, **unsandboxed** Hermes executor. It is opt-in:
`baton.json` must set `executor.backend: "hermes"` and `executor.sandbox_exec: false`.
Hermes then runs with `--yolo` and can read and write any file the current user can
access. This trade-off is intentional for native Windows, which has no
`sandbox-exec`; a future WSL version may restore a sandbox. If `sandbox_exec` is
missing or true, `baton init/start/resume/doctor` refuse the configuration and
explain the required setting. The runtime prints a WARN on `start`/`resume` and
doctor reports WARN once the opt-out is explicit.

Hermes monitoring is not available on Windows. `monitor.backend` must remain
`codex`; `monitor.provider` (for example `deepseek`) continues to run through
Codex's read-only sandbox. `monitor.backend: "hermes"` raises a ConfigError that
names this restriction.

The v2 Codex monitor, shadow repository, 60-second frozen checks, mid-leg checks,
ruling ledger/verdict gate, lean mode, capacity retries and quota/statusline remain.

From PowerShell, preview installation before installing:

```powershell
powershell -NoProfile -File ./install_windows.ps1 -WhatIf
powershell -NoProfile -File ./install_windows.ps1
```

The installer creates a directory junction at `~/.claude/skills/baton` and
absolute-path wrappers at `~/.local/bin/baton` and `~/.local/bin/baton.cmd`.
It does not require Developer Mode or elevation. All destinations are checked
before changes: existing different targets, files, and directories cause an
error. An identical installation is left alone. `CLAUDE_SKILLS_DIR` and
`BATON_BIN_DIR` override the destinations, as on POSIX; `-SkillsDir` and
`-BinDir` are also available. Add the bin directory to PATH yourself if needed.
The POSIX installer and optional council installation are unchanged.
`CLAUDE_CONFIG_DIR` overrides `~/.claude` for the default skill destination,
`baton harness base`, legacy-instruction warnings and statusline settings.

## Hermes executor

Use a native `hermes.exe`, not `hermes.cmd`, `hermes.bat` or `hermes.ps1`.
The installer and `baton doctor` reject wrapper launchers because cmd.exe parses
long prompts, quotes and `%` differently. Point at the real executable explicitly:

```json
{
  "executor": {
    "backend": "hermes",
    "sandbox_exec": false,
    "hermes_bin": "C:/path/to/hermes.exe",
    "hermes_profile": "baton-exec-v2",
    "provider": "openai-codex"
  },
  "monitor": {
    "backend": "codex",
    "provider": "deepseek"
  }
}
```

`executor.hermes_bin` (or `HERMES_BIN`) may be omitted when a native `hermes.exe`
is on PATH. `executor.provider` selects the Hermes provider; for example use
`deepseek` when the profile has a DeepSeek credential. Log in once in the user's
own terminal, not through Baton, for the provider selected by `executor.provider`:

```powershell
hermes -p baton-exec-v2 auth add <provider> --type oauth
hermes -p baton-exec-v2 auth list <provider>
# OpenAI Codex: provider is openai-codex; DeepSeek: provider is deepseek.
# Use the credential type required by that provider if it is not oauth.
```

The prompt is written to `.baton/tasks/<task>/hermes-prompt.txt` and passed with
`--query-file` on Windows, avoiding the 32767-character command line limit and
cmd.exe quoting. Resume uses `--resume`; POSIX installations continue to use
`--query=` and `-r`. Without a sandbox, Hermes's own tool policy is the only
guardrail. Run it only in projects where the user accepts that risk.

## Codex home and Windows sandbox

Each task's `CODEX_HOME` inherits ordinary config files, but never runtime state:
`*.lock`, `*.sqlite` and its `-wal`/`-shm`/`-journal`, `*.tmp*`, and
`.DS_Store`. SQLite session data is left to Codex. Set `CODEX_SQLITE_HOME` to a
dedicated directory (for example `%BATON_HOME%\codex-sqlite`) so task homes do
not each build independent session databases; start, resume and doctor print a
WARN when it is unset.

`auth.json` is only ever linked, never copied. If Windows denies the symlink,
OpenAI-login executors fail with instructions to enable Developer Mode
(`Start ms-settings:developers`) or switch to a non-OpenAI provider/Hermes; those
modes do not need `auth.json`. Other regular files fall back to a copy and a
failed copy is skipped with a WARN. The shared directories `cache`, `plugins`,
`packages` and `skills` fall back to a directory junction, which needs neither
elevation nor Developer Mode.

Codex executor commands default to `-c windows.sandbox="unelevated"` for
`workspace-write` and `read-only`. An explicit `windows.sandbox` value in
`executor.extra_config` is kept and is not duplicated.

Windows sandbox grants writing only to files created after the grant. Existing
workspace files that did not inherit the sandbox capability SID remain readable
but cannot be modified by an unelevated sandbox; create/replace the file from
outside the sandbox or repair the ACL. This is a sandbox/ACL property, not a
Baton configuration error.

Run `baton init --no-statusline`, then `baton doctor` in a new project. Statusline commands on
Windows require Git Bash and quote absolute forward-slash paths, including
paths with spaces. Use `baton init --no-statusline` to leave settings alone.
Desktop notifications are disabled on Windows. Upstream model names and protocol
schemas are retained. v2 state and the shadow Git directory live outside the
project, under `BATON_HOME/projects/<project-id>` (default `~/.baton/projects`).
`BATON_HOME` must be outside the project. Relative paths in the shadow index use
Git's forward slashes; files and subprocess text use UTF-8.

To keep state on a larger drive, set `$env:BATON_HOME = 'D:/baton'` in PowerShell before
running Baton. The default is `~/.baton`. When moving existing state, stop running tasks,
move the old state directory's contents to the new directory, and keep the same setting
for subsequent commands.
Dossier `code/` copies omit untracked files ignored by the project's own Git rules;
tracked files and non-Git projects retain their existing coverage. Omitted file/byte counts
and the reason appear in `manifest.json` under `code_omissions`. Each new mid-leg check
removes older check code copies for that task and leg. `baton close` removes all of the
task's dossier code copies. Other records, the original manifest inventory, ledger bindings
and rollback snapshots remain; `code_pruned` records the removal. Pruned dossiers retain
record verification but cannot rerun a monitor.
Use `baton gc --dry-run` to preview cleanup for already-closed tasks in the current project,
then `baton gc` to remove their code copies and print dossier/file/byte counts.

Read-only Codex monitor commands explicitly set `windows.sandbox="unelevated"`, including
checks, briefs, curation and the doctor probe, because they ignore user config.
`baton doctor --monitor-probe` requires an actual read of a sealed dossier's `charter.md`.
An unreadable dossier is reported as a monitor infrastructure failure.

Verification commands use Windows `cmd.exe` syntax. To run a POSIX verification
command, explicitly invoke the absolute Git Bash executable and quote for cmd.
`chmod` only supplies a read-only hint on Windows, not a Unix security boundary.
The Codex sandbox and frozen-file hashes provide the relevant execution checks.

## Process behavior

- PID probes use `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` and
  `GetExitCodeProcess`, never `os.kill(pid, 0)`. Handles are closed.
- PID-reuse checks obtain full Unicode command lines from PowerShell CIM,
  with an eight-second timeout. Query failures raise; they are not treated
  as a dead process. Descendant cleanup also checks creation times.
- Runners, executors, monitors, mid-leg check runners and verification processes use hidden private consoles and attempt
  `CREATE_BREAKAWAY_FROM_JOB`. A denied breakaway logs an explicit diagnostic
  and retries inside the job. Such a fallback **cannot survive that job being
  destroyed**. The host must permit breakaway for survival of job teardown.
  A hidden console is used instead of `DETACHED_PROCESS`/`CREATE_NO_WINDOW`
  so that targeted `CTRL_BREAK_EVENT` delivery is possible.
- Stop sends CTRL_BREAK, allows the existing graceful time budget (20 + 10
  seconds in the runner; 8 seconds for orphan cleanup), then uses
  `taskkill /PID ... /T /F`, with the existing final 5/3-second checks.
  Descendants captured before graceful exit are checked for surviving orphans.
- SIGBREAK lets the runner stop its executor and record a failed leg when the
  runner itself is interrupted. An unconditional `TerminateProcess` or
  `taskkill /F` cannot run cleanup handlers; the next Baton liveness refresh
  uses the existing failed-runner recovery path.
- State, launch, shadow-index, ruling-ledger, memory-ledger/index and quota locks use `msvcrt.locking`. Text files and text
  subprocess pipes use UTF-8. The quota reader uses `PeekNamedPipe` instead
  of POSIX `select` on anonymous pipes.
- Atomic state/ledger/cache replacement retries transient Windows sharing/access
  violations for at most two seconds; readers always see a complete old or new file.

## Validation

```powershell
python tests/run_windows_batches.py
python tests/smoke_windows.py
```

Each test file runs in a fresh process, sequentially, to bound memory. The runner
isolates HOME/USERPROFILE/BATON_HOME/cache in TEMP and blocks live Codex/Claude
calls. Fake executables use the launcher shipped in the already-installed pip;
no dependency is installed. Live mem0/Ollama/MCP integration and unsupported
POSIX installation paths are explicitly skipped; Windows Hermes tests use a
fake native `hermes.exe` and never invoke a model. Logs/counts go to
`test-results/`. The smoke script uses a synthetic quota cache, never `start`.

Windows tests exercise real process probes, cross-process lock exclusion,
CTRL_BREAK, forced parent/child cleanup, runner-result persistence, UTF-8,
and statusline paths with spaces and Unicode. They do not invoke a model.
Live host detachment must also be checked from the actual Claude Code Bash
tool; a standalone Git Bash test alone does not establish the host's job policy.
