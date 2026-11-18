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

## Not measured

- `bellhop codex` inside a nono sandbox. Codex puts the real socket of every app server in
  `/tmp/codex-daemon-<uid>` and leaves a link at the path it was asked to listen on, so the
  session's sandbox must allow that directory (see `docs/handoff/nono-grants.md`).
- `-c sandbox_mode="danger-full-access"`, the override the user's own wrappers pass.
