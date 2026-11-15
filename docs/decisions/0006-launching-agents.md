# 0006 — How bellhop starts an agent

**Status:** accepted

**Recorded:** 2026-11-14

## Context

Task B7 lets `bellhop launch` and `send --wake` start a configured agent that is not running. A
start runs across two processes: the command that opens the tmux session, and the drainer, which
is the only long-lived bellhop process. A start also has to survive messages arriving close
together, a drainer that changes, and agents that stop at a screen bellhop must never answer.

## Decision

1. **The command opens, the drainer completes.** Under a per-agent `flock`, the command writes
   the `starting` binding and the first message in one transaction, opens the tmux session and
   records `tmux_target`. Only the drainer decides that a start is ready or failed
   (`launch.start_checker`). `launch` without `--no-wait` only watches the binding.
2. **`route = "launch"` marks a first message, not a delivery route.** The message travels as the
   harness's prompt argument. While its binding is `starting` it is `pending`; it becomes `sent`
   when the start is ready, `failed` when tmux refused the session, `sent` with
   `outcome unknown; start failed: …` when the start failed after the pane opened, and
   `failed` with `start abandoned: …` when the binding stopped being `starting` by other means.
3. **Ready means listed and reachable.** hooklinesinker lists the session (the pre-assigned id
   for Claude and Pi; for Codex, the thread on the agent's own app server whose preview is the
   prompt) and its route answers. Startup screens and login screens fail a start only before the
   session is listed. The 90 s deadline holds whatever the check raises.
4. **A failed start is recognised by its `detail`**, which starts with `start failed:`. `send
   --wake` refuses such an agent until `launch` retries it. Other failed bindings are replaced.
5. **The child's environment is an allowlist**, applied with `/usr/bin/env -i` so neither the
   caller's nor the tmux server's environment reaches the agent, and the tmux client never
   follows the caller's `$TMUX`: the tmux client runs with the same allowlist (plus `TMUX_TMPDIR`
   and `TERM`), so a server it starts holds none of the caller's session or key variables.

## Consequences

- A drainer that cannot run tmux or read the config still fails a start at its deadline, so a
  `starting` binding cannot hold an agent forever; `launch` also starts again over a binding that
  has been `starting` for more than two minutes.
- The tmux server a start used is recorded in `tmux_target` (`-L <name>`/`-S <path>` before
  `<session>:<pane>`), so the drainer reads the right pane whatever the config says now; the
  drainer re-reads the config when the file changes (B-7f).
- A typed failure kind on the binding would replace the `detail` prefix; it needs a schema
  change and is left for later.
