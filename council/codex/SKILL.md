---
name: council
description: >-
  Call council BEFORE substantive work — before writing, before committing to an interpretation,
  or before building on an assumption. If the task requires orientation first (finding files,
  fetching a source, seeing what's there), do that, then call council. Also call council when
  you believe the task is complete, before declaring the deliverable; when stuck (errors
  recurring, approach not converging, or results not fitting); or when considering a change of
  approach. On tasks longer than a few steps, call council at least once before committing to an
  approach and once before declaring done. On short reactive tasks where the next action is
  dictated by tool output, a call is optional.
---

# Council · Codex consultation

This skill launches a stronger reviewer team as a fork of the current Codex session. The chief
sees the inherited conversation, composes a focused query, runs the councilors, and returns a
verdict for the main agent to act on. It must not recurse into this skill.

The launcher starts chief with `--dangerously-bypass-approvals-and-sandbox`,
matching Claude chief's `--dangerously-skip-permissions`. Chief runs without a
sandbox so councilor CLIs can access login credentials and start their own
sandboxes without nesting macOS seatbelt sandboxes. Councilor read-only boundaries
remain in their backends: Codex uses `codex exec --sandbox read-only`, and Claude
follows the charter supplied by `council.sh`.

When you need a consultation, resolve this installed skill directory and run its
`scripts/launch.sh` by **absolute path**, for example:

```shell
bash "/absolute/path/to/skills/council/scripts/launch.sh"
```

Do not use a Claude-specific skill-directory variable or a relative path. The launcher takes the
current Codex session id from its environment; an explicit id may be passed as its first argument.

The launcher calls model APIs and the Codex sandbox normally has networking disabled
(`CODEX_SANDBOX_NETWORK_DISABLED=1`). Request the required network/escalated execution before
running it. The consultation blocks while the chief and councilors work: run it in the foreground
with a command timeout of **at least 45 minutes**. Do not background it yourself or use a short
shell timeout.

The chief keeps the round queries and councilor opinions under the working directory's
`.council/` directory. Read its final response under these headings:

```text
## Verdict
## Why
## Dissent worth weighing
## Coverage
```

The procedure allows at most three rounds and asks the chief to cross-examine only when opinions
conflict in a way that changes the decision. Follow the verdict, while preserving any dissent the
chief marks as worth weighing.
