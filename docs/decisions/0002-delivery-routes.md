# 0002 — Delivery routes

**Status:** accepted

**Recorded:** 2026-11-14

## Context

[ADR 0001](0001-foundations.md) left the inside piece for each harness to a measured spike. The
spike ran every candidate through S1 (idle wake), S2 (push during a running turn, at the boundary
and as an interrupt), S3 (permission prompt open), S3b (question dialog open), S7 (draft in the
composer) and S10 (`/clear` or `/new` in the same process), three repetitions each, on Claude Code
2.1.325, Codex 0.167.1 and Pi 1.5.2 with hooklinesinker 1.5.1. The table, the method and the
answers to the plan's questions 1–5 are in [routes.md](../reference/routes.md).

The rules applied: a cell passes at 3/3, and `n/a` counts as a pass. A candidate that fails S3 or
S3b is out. The `idle` route is the candidate passing the most of S1, S7 and S10, ties broken by
the least user configuration and, for Claude, then by the MCP relay. `now` takes the best candidate
that passes S2 mid-turn, else degrades to `idle`.

## Decision

| Harness | `idle` route | `now` route | Route name |
|---|---|---|---|
| Claude | MCP relay, frame `priority: "later"` | the same relay, frame `priority: "next"` | `claude-relay` |
| Codex | the app server the session is attached to: wait until the thread is idle, then `turn/start` | the same app server: `turn/steer` into the running turn (`turn/start` when idle) | `codex-app-server` |
| Pi | extension, `sendUserMessage(text, { deliverAs: "followUp" })` | the same extension, `deliverAs: "steer"` | `pi-extension` |

- **Claude.** No candidate failed S3 or S3b. The MCP relay, the hook-started relay and the
  mod-spawned relay each passed S1, S7 and S10 3/3, and so did the mod calling `$.prompt.submit`.
  Each needs one durable entry in the user's setup (an MCP server, a `SessionStart` hook, or an
  enabled plugin), so the tie goes to the MCP relay. That also avoids two costs the others carry:
  a hook-started relay has to detach and outlives its session unless it watches the Claude pid, and
  `$.prompt.submit` can never interrupt. `now` uses the same relay: `priority: "next"` landed
  mid-turn 3/3. The relay listens on `<runtime_dir>/claude-<pid>.sock`, where `<pid>` is its parent,
  the Claude process hls reports, and it reads `CLAUDE_CODE_MESSAGING_SOCKET` and
  `CLAUDE_CODE_MESSAGING_TOKEN` from its own environment. It never imitates another session and
  never needs `crossSessionInbound`.
- **Codex.** No candidate failed S3. S3b could not be measured: Codex opened no question dialog
  in its default mode (see Consequences). `turn/start` on the shared daemon, `codex queue` on the
  shared daemon, and `turn/start` on a per-session server each passed S1 and S7 3/3 and failed S10
  0/3, a three-way tie. By the least-configuration rule the per-session server ranks first (it
  needs no user setting, where the other two need the shared daemon running). It is the same
  mechanism as `turn/start` on the daemon, JSON-RPC to the app server the TUI is attached to, so one
  route covers both, as ADR 0001 requires: the daemon's socket when the session is attached to it,
  a per-session server for sessions bellhop launches. `codex queue` is not used: it needs the daemon
  and has no interrupt. `turn/steer` on the same connection passed S2 mid-turn 3/3, so it is `now`.
  The route does not queue to the boundary itself; bellhop's drainer waits until the thread is idle.
  A stand-alone Codex session stays `hold`-only.
- **Pi.** S3 and S3b are `n/a`; the extension passed S1, S7 and S10 3/3, `followUp` waited for the
  boundary and `steer` landed mid-turn, 3/3 each.

## Consequences

Known limits, each measured, with what bellhop does about it:

- **Codex S3b is unmeasured.** The decision rule that drops a candidate failing S3b was never
  exercised for `turn/start` or `turn/steer`. Before Task B5 enables `now` for Codex, it re-measures
  S3b in a mode that shows a question dialog; until then a `now` push to a Codex session whose hls
  phase is `permission` is held back like an `idle` one.

- **Pi is invisible to hooklinesinker 1.5.1 when launched the usual way.** Pi runs as `node` and
  retitles its process `pi`, so hls records no process and leaves the session out of
  `sessions --json`. Until hls identifies it, bellhop cannot see such a Pi session. `bellhop launch`
  can start Pi through an executable named `pi` (a link to `node`), which hls does identify. A
  proposal to hls is handed to the user.
- **Codex `/new` is not followed.** hls kept naming the old thread after `/new`, so an `idle` or
  `now` push went into a thread the TUI no longer shows (S10 0/3 on every Codex route). Delivery is
  confirmed from the receiver's transcript, so such a push stays `sent` and never reaches
  `confirmed`, which `bellhop status` shows.
- **Codex app-server sessions have no pid or pane of their own in hls.** hls reports the server's
  pid (shared by every TUI on the daemon) and no tmux pane. Bindings for these sessions are keyed by
  thread id and app-server socket, as the plan's store section already says, and `whoami` uses
  `CODEX_THREAD_ID` rather than the process tree.
- **The shared Codex daemon updates and restarts itself.** Five to seven minutes after it started,
  with a newer release available, its updater installed it and restarted the daemon, forcing the
  restart after a 60 s grace. The drainer therefore treats a refused connection as `busy` (try again
  later), and a push cut off by the restart as outcome `unknown`, which is never repeated.
- **Claude `idle` is not queued by bellhop.** With `priority: "later"` Claude itself holds the
  message until the turn ends (`queues_until_idle` is true for this route).
- **`whoami`** reads `CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID` or `PI_SESSION_ID`. The Claude and
  Pi values follow `/clear` and `/new`; Codex's after `/new` was not verified.
- **Launching with an initial prompt** works for all three harnesses. Claude's `--mcp-config` takes
  a list, so the prompt must not follow it directly.
