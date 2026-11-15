# nono grants for bellhop and callboard

**What to do:** add these paths to the `claude-local`, `codex-local` and `pi-local` profiles (or to
the `<active>-local` profile each extends), restart the sessions, then run each `nono why` line
from inside a sandboxed session of that profile. Nothing here was applied or run. The profile
syntax is nono's own (`nono profile guide`); this note gives paths and access only.

Paths are the defaults for uid 501 with no `XDG_*`, `BELLHOP_RUNTIME_DIR`, `CODEX_HOME`,
`CLAUDE_CONFIG_DIR` or `PI_CODING_AGENT_DIR` set; substitute yours if they are set. `/tmp` is
`/private/tmp` on macOS: grant the resolved path. `nono why` reports `path_not_granted` for a path
that does not exist yet even when its parent is granted, so check those after first use (or check
the parent).

## Why every profile needs all of it

`bellhop` and `callboard` run as commands inside the agent session, so they get its sandbox. The
background drainer that `bellhop send` starts inherits it too, and the drainer delivers to every
harness and confirms from every harness's transcript. So the `claude-local`, `codex-local` and
`pi-local` profiles each need the whole list, not only their own harness's paths. hooklinesinker's
hooks also run inside every agent session and write its state directory.

## bellhop

| Path | Access | Why |
|---|---|---|
| `/private/tmp/bellhop-501` | readwrite | runtime dir: created 0700; endpoint sockets (`claude-<pid>.sock` and `.lock`, `pi-<pid>.sock`, `codex-<agent>-<token>.sock`) |
| `~/.local/state/bellhop` | readwrite | `bellhop.sqlite` (+ `-wal`, `-shm`), `drain.lock`, `drain.log`, `drain.log.1`, `launch/<agent>.lock`, `codex-<agent>.log` |
| `~/.config/bellhop/config.toml` | read | agents, `runtime_dir`, `tmux_socket` (the Pi extension reads it too) |
| `~/.local/share/hooklinesinker` | read | the `hooklinesinker` binary bellhop runs (`bin/` links into `versions/`) |
| `~/.local/state/hooklinesinker` | readwrite | `hooklinesinker sessions --json` takes `status.lock` and makes its directories private; it is not a read-only call |
| `/private/tmp/tmux-501` | readwrite | hls runs `tmux list-panes`; `bellhop launch` opens sessions (`tmux_socket` names another socket in this directory, or a path) |
| `~/.codex/app-server-control` | read | the daemon's control socket link |
| `/private/tmp/codex-daemon-501` | readwrite | the daemon socket the link points to (connecting needs write); launched agents' per-session sockets |
| `~/.codex/sessions` | read | Codex rollouts, to confirm delivery |
| `~/.claude/projects` | read | Claude transcripts, to confirm delivery |
| `~/.claude/sessions` | read | the Claude registry the relay reads (inside Claude's own sandbox) |
| `~/.pi/agent/sessions` | read | Pi session files, to confirm delivery |

For `bellhop launch` and `send --wake` also: each agent's `cwd` (read), its homes from `env`, and
`/usr/bin/env` (read, to execute).

## callboard

| Path | Access | Why |
|---|---|---|
| `~/.local/share/callboard` | readwrite | `callboard.sqlite` (+ `-wal`, `-shm`), `attachments/` and their `.tmp-*` files |
| `~/.config/callboard/config.toml` | read | `max_attachment_bytes`, `ping_command` |
| files passed to `--attach` / `--out` | read / write | user-chosen paths |

callboard runs `bellhop` for identity and pings, so it needs everything in the bellhop table.

## Checks

Run inside a sandboxed session of each profile (`--self` is required):

```sh
nono why --self --path /private/tmp/bellhop-501 --op readwrite
nono why --self --path ~/.local/state/bellhop --op readwrite
nono why --self --path ~/.config/bellhop/config.toml --op read
nono why --self --path ~/.local/share/hooklinesinker --op read
nono why --self --path ~/.local/state/hooklinesinker --op readwrite
nono why --self --path /private/tmp/tmux-501 --op readwrite
nono why --self --path ~/.codex/app-server-control --op read
nono why --self --path /private/tmp/codex-daemon-501 --op readwrite
nono why --self --path ~/.codex/sessions --op read
nono why --self --path ~/.claude/projects --op read
nono why --self --path ~/.claude/sessions --op read
nono why --self --path ~/.pi/agent/sessions --op read
nono why --self --path ~/.local/share/callboard --op readwrite
nono why --self --path ~/.config/callboard/config.toml --op read
```

Then `bellhop doctor` inside the session should report every check `ok`.
