# 0007 — An app server per Codex session, and a trace file

**Status:** accepted; adds to the Codex route in [ADR 0002](0002-delivery-routes.md)

**Recorded:** 2026-11-15

## Context

ADR 0002 reaches a hand-started Codex session through Codex's shared daemon. On the first real
install no Codex session was reachable: the daemon had no thread loaded although 15 sessions were
running, sandboxed and unsandboxed alike.

Codex 0.167.1 decides at start whether its terminal UI may use the shared daemon
(`codex-rs/tui/src/daemon_startup.rs`, tag `rust-v0.167.1`). It does not when it is started with
`--no-daemon`, `--oss`, `--profile`, or a `-c` override other than a boolean `features.*`,
`suppress_unstable_features_warning` or `tui.fullscreen_transcript`; `--search` counts as such an
override. It then runs an app server inside the terminal UI's own process, which has no socket.

Measured 2026-11-15 on the real install: a `codex -c model_reasoning_effort="low"` left the
daemon's `thread/loaded/list` empty and showed "Running without the shared background server:
command-line configuration overrides (-c, --enable, --disable, or --search) requires embedded
mode." Launch wrappers that set the sandbox mode per invocation pass such an override on every
start.

The same test showed that a refused `bellhop send` left no record anywhere, and that
`reachable: false` gave no cause: the sending agent guessed `--no-daemon`, which was wrong.

## Decision

**`bellhop codex [CODEX ARGUMENTS...]` starts Codex with an app server of its own.** It runs
`codex app-server --listen unix://<runtime dir>/codex-<pid>.sock` with the caller's `-c`,
`--enable` and `--disable` arguments, attaches `codex --remote` with every argument unchanged, and
stops the server when the terminal UI exits. This is what `bellhop launch` already did for a
configured agent (ADR 0006). A subcommand other than `resume` and `fork`, and `--remote`,
`--no-daemon`, `--oss`, `--profile`, `--help` and `--version`, run as plain `codex`. When the
runtime directory is unusable, the server cannot be started or it does not open its socket,
bellhop warns and starts plain `codex`, so wrapping every start is safe. SIGTERM and SIGHUP are
passed on to the terminal UI, and bellhop exits with its status.

**A `resume` or `fork` leaves permission overrides off the terminal UI.** Codex refuses them on a
remote resume or fork ("Permission overrides are not supported when resuming a remote task.", or
"…when forking…") and exits 1. So for `resume` and `fork` bellhop keeps from the terminal UI `-s`
and `-a` (given to the app server as `-c sandbox_mode=…` and `-c approval_policy=…`),
`--dangerously-bypass-approvals-and-sandbox` and its alias `--yolo` (given to the app server as
both), `--approve-for-me` and its alias `--not-so-yolo` (given as the three settings Codex makes of
it: `approvals_reviewer="auto_review"`, `approval_policy="on-request"`,
`sandbox_mode="workspace-write"`), every `-c` whose key starts with `approval_policy`,
`approvals_reviewer`, `sandbox_mode`, `sandbox_workspace_write`, `default_permissions`,
`permissions` or `network` (the app server already has it), and `--add-dir`, which has no
app-server form: bellhop names it and leaves it off. Other options, such as
`-c model_reasoning_effort=…`, `-m` and `-C`, reach the terminal UI as before. A resumed thread runs with the sandbox and approval policy
saved with it; a fork starts from the thread it forks.

**The Codex route asks every app server it knows.** That is the binding's socket or the shared
daemon, and every `codex-*.sock` in the runtime directory that is a link into Codex's own socket
directory (`/tmp/codex-daemon-<uid>`, where Codex puts the real socket). The one that has the
thread loaded is the session's; a server in the runtime directory wins over the shared daemon, and
a launched agent's own server over both. With no such link the route behaves as before.

**An unreachable session says why.** Every route has `obstacle(session)`; `sessions --json` shows
it as `reason`, and a `no_route` error ends with it.

**bellhop keeps a trace.** `$XDG_STATE_HOME/bellhop/trace.jsonl` gets one JSON line per command,
route probe, push, transcript confirmation and background event, written through `files.open_private`
and never allowed to fail a command. `bellhop log` reads it. The wrappers that write most of it sit
in `wiring` (`TracedRoute`, `TracedReader`), so delivery and the drainer are unchanged.
`BELLHOP_TRACE=off` turns it off.

## Measured (2026-11-15, Codex 0.167.1, the real install)

A session started with `bellhop codex -c model_reasoning_effort="low" -c sandbox_mode="read-only"`
in a trusted folder: the socket appeared 0.1 s after the start; after its first turn
`sessions --json` showed it `reachable` on `codex-app-server`; `bellhop send --confirm 60` returned
`confirmed` in 0.3 s and the agent answered; the rollout's `turn_context` recorded `effort: low`
and a `read-only` sandbox policy. Quitting the terminal UI removed the socket and the server log.
A second session started with `-s read-only`, which only the terminal UI is given, also ran with a
`read-only` sandbox policy; closing its terminal (SIGHUP) left no socket, log or app server.
`sessions --json` over 46 sessions took 0.2 s.

## Measured (2026-11-25, Codex 0.171.0, isolated smoke home)

The list of refused options is Codex's own `ResumePermissions::from_overrides`
(`codex-rs/tui/src/resume_permissions.rs`, identical in the `rust-v0.171.0` tag and on `main`): an
approval policy or reviewer, a sandbox mode, a permission profile or default permissions, extra
writable roots, and any of those keys in `-c`; `--cd` is allowed. Its module comment says
"Omitted choices let app-server restore the destination task's saved settings."

`tests/smoke/test_codex_resume.py` started a thread with `-a on-request -s read-only` and quit it,
then resumed it through `bellhop codex` with `-s workspace-write`, and again with
`-c sandbox_mode="workspace-write" -c approval_policy="on-request"`, and forked it with
`-s workspace-write`. Both resumes stayed up, were listed by hooklinesinker and reachable, and a
`bellhop send --confirm` was `confirmed`; the fork stayed up. Every resumed turn's `turn_context`
recorded a `read-only` sandbox policy: the saved setting, not the one the resume asked for.

## Not measured

- `bellhop codex` inside a nono sandbox. Codex puts the real socket of every app server in
  `/tmp/codex-daemon-<uid>` and leaves a link at the path it was asked to listen on, so the
  session's sandbox must allow that directory (see `docs/handoff/nono-grants.md`).
- `-c sandbox_mode="danger-full-access"`, the override the user's own wrappers pass, on a new
  session or a resume; checking it runs a real agent with its permission checks off, which is the
  user's call. By the measurement above, a thread resumed through bellhop runs with the sandbox it
  was started with, so a thread the wrappers started with `danger-full-access` keeps it, and one
  started with another sandbox keeps that one.
