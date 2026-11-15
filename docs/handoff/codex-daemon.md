# The Codex daemon and bellhop

**What to do:** check one setting in your Codex config; change nothing if it is already on. This
note was written from measurements on an isolated Codex home (Codex 0.167.1) only; your own daemon
was never started, stopped, queried or connected to.

## The setting

```toml
# $CODEX_HOME/config.toml (default ~/.codex/config.toml)
[features]
daemon_auto_start = true
```

It is stable and on by default in Codex 0.167.1, so it only needs attention if you turned it off.

## What it changes

- **On:** a plain `codex` starts a shared app server (the daemon) in its `CODEX_HOME` if none is
  running and attaches its TUI to it. bellhop reaches every such session through the daemon's
  socket (`$CODEX_HOME/app-server-control/app-server-control.sock`): `idle` messages start a turn
  when the thread is idle, and `now` messages steer a running turn. A message appears in the
  attached TUI like a typed prompt.
- **Off, or `codex --no-daemon`:** the TUI runs its own private app server that bellhop cannot
  reach. `bellhop send` to that session answers `no_route` (exit 4), or stores a `hold` with
  `--or-hold` that the session reads with `bellhop inbox`.
- `bellhop launch` does not depend on the setting: it starts its own app server for each Codex
  agent it launches.

## What to expect when it is on

- **The daemon updates and restarts itself.** When a newer Codex is available, its updater
  installs it and restarts the daemon after a 60 s grace (seen five to seven minutes after start).
  Running TUIs keep working. After a restart hooklinesinker stops listing the daemon's threads
  until each thread's next turn, so for that window bellhop cannot reach them: a message to the
  session id waits, and an agent bound to one is shown `failed` until its next turn.
- **A quit TUI's thread stays loaded in the daemon.** A message to it starts a turn that no TUI
  shows.
- **One daemon per `CODEX_HOME`.** Sessions with another `CODEX_HOME` attach to another daemon;
  give an agent's `CODEX_HOME` in its bellhop config `env` so bellhop finds the right one.
