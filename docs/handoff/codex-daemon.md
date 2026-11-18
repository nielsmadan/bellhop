# Codex and bellhop

**What to do:** start Codex through `bellhop codex` (change your `codex` and `codex-raw` wrappers
as shown below), then restart the Codex sessions you want reachable. Nothing here was applied.

## Why

bellhop reaches a Codex session through an app server it can connect to. Your wrappers in
`~/ac/.airc.d/codex.zsh` pass `-c sandbox_mode="danger-full-access"` on every start, and Codex
0.167.1 keeps a session with such an override off its shared daemon (it shows "Running without the
shared background server: command-line configuration overrides … requires embedded mode"). On
2026-11-15 none of your 15 Codex sessions was reachable, sandboxed or not. See
[ADR 0007](../decisions/0007-codex-session-servers.md).

`bellhop codex [CODEX ARGUMENTS...]` starts an app server for the session and attaches the Codex
terminal UI to it. Every argument goes to `codex` unchanged, and anything that is not an
interactive start (`exec`, `login`, `--version`, …) runs as plain `codex`.

## The wrapper change

In `~/ac/.airc.d/codex.zsh`, put `bellhop` in front of `codex` in the three launch lines:

```zsh
AGENT_HARNESS=codex sops-exec bellhop codex "$@"                     # in _codex_launch, unsandboxed profile
AGENT_HARNESS=codex _agent_sandboxed codex-local bellhop codex -c sandbox_mode='"danger-full-access"' -c approval_policy='"never"' "$@"
_codex_raw_launch() { AGENT_HARNESS=codex sops-exec bellhop codex -c sandbox_mode='"danger-full-access"' "$@"; }
```

Try one line by hand first, in a folder Codex already trusts:

```sh
AGENT_HARNESS=codex sops-exec bellhop codex -c 'sandbox_mode="danger-full-access"'
```

Give it one prompt (hooklinesinker lists a Codex thread only after its first turn), then
`bellhop sessions --json` shows it `reachable: true` on `codex-app-server`.

## What was and was not tested

- **Tested on your machine, unsandboxed:** `bellhop codex -c model_reasoning_effort="low" -c
  sandbox_mode="read-only"` in `~/wrksp/efg-all`: delivered, confirmed, answered; both overrides
  were in force in the session.
- **Not tested: `sandbox_mode="danger-full-access"`.** The session that built this was not allowed
  to start a Codex with its sandbox off.
- **Not tested: inside nono.** The sandboxed line needs two things from the `codex-local` profile:
  it must be able to run `bellhop` (`~/.local/bin/bellhop` and the tool's files under
  `~/.local/share/uv/tools/bellhop`), and Codex must be able to create its socket (group 3 in
  [nono-grants.md](nono-grants.md)). If either is missing, `bellhop codex` either fails to start
  (the first) or warns and starts plain `codex` (the second), and the session is unreachable as
  it is today.

## What to expect

- **One more process per Codex session:** the `bellhop codex` wrapper, which waits for the
  terminal UI. The app server replaces the one Codex ran inside the terminal UI's process.
- **The app server stops when you quit Codex.** A message to that session then fails as
  `session gone`.
- **`daemon_auto_start` no longer matters for these sessions.** It still decides whether a plain
  `codex` with no overrides attaches to the shared daemon, which bellhop also reaches.
- **The daemon updates and restarts itself** (plain `codex` sessions only). After a restart
  hooklinesinker stops listing the daemon's threads until each thread's next turn, so for that
  window bellhop cannot reach them.
