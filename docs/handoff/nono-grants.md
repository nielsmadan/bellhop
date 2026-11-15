# nono grants for bellhop and callboard

**What to do:** decide, group by group, what the `claude-local`, `codex-local` and `pi-local`
profiles (or the `<active>-local` profile each extends) should grant; add it; restart the
sessions; run the `nono why` lines from inside a sandboxed session of that profile. Nothing here
was applied or run. The profile syntax is nono's own (`nono profile guide`); this note gives paths,
access and consequences only.

Group 1 is what messaging needs. It is not free: it lets a sandboxed agent put any text into any
session bellhop can reach, as described under group 1. Groups 2 to 4 each open a further hole,
described plainly below. This note recommends nothing beyond group 1, and group 1 too is your
decision.

Paths are the defaults for uid 501 with no `XDG_*`, `BELLHOP_RUNTIME_DIR`, `CODEX_HOME`,
`CLAUDE_CONFIG_DIR` or `PI_CODING_AGENT_DIR` set; substitute yours if they are set. `/tmp` is
`/private/tmp` on macOS: grant the resolved path. `nono why` reports `path_not_granted` for a path
that does not exist yet even when its parent is granted, so check those after first use (or check
the parent).

## What the drainer guarantees

The store, the state directory and the runtime directory may be writable by a sandboxed agent
while the drainer runs outside any sandbox. So nothing a store row or a file in those directories
says makes the drainer do more than deliver text to a session:

- it never runs a program the store or those directories name: it finds `tmux` on its own `PATH`,
  else at `/opt/homebrew/bin/tmux`, `/usr/local/bin/tmux` or `/usr/bin/tmux`, and hooklinesinker the
  same way it always does;
- it never writes through a link planted there. The files it writes (`bellhop.sqlite`,
  `drain.lock`, `drain.log`, `drain.status.json`, `codex-<agent>.log`) are opened without following
  a symlink and refused unless each is a regular file of yours with one link; the status file and
  the Codex log are created fresh each time. A planted `bellhop.sqlite-wal` or `-shm` is removed
  and each is created as such a file before SQLite opens the store, and SQLite itself refuses a
  symlinked one; a hard link put there in the gap between that check and SQLite's own open is not
  caught. `launch/<agent>.lock` and the relay's `claude-<pid>.lock` are opened without following a
  symlink and are only locked, never written;
- it reads `drain.status.json` and `drain.lock` without following a link, without blocking, and
  only when each is a regular file;
- it contacts only the tmux server `config.toml`'s `tmux_socket` names; a start whose row names
  another server fails without tmux being run.

What it does deliver is the text itself, under the sender name the row carries.

## Why the drainer matters

`bellhop` and `callboard` run as commands inside the agent session, so they get its sandbox. The
background drainer that `bellhop send` starts when a message has to wait inherits the sandbox of
whichever session started it, and then delivers and confirms for every harness until it exits.
So what a profile grants decides what every waiting message can do while a drainer started from
that profile runs.

**The alternative to groups 2 to 4:** run the drainer, and any `send --wake` / `bellhop launch`,
from an unsandboxed session (a plain terminal of yours, or a launchd job running `bellhop doctor`
when something waits). A drainer started there is not sandboxed, and the sandboxed profiles then
need group 1 only. Such a drainer reads rows and files a sandboxed agent can write; the guarantee
above is what it holds to: it delivers their text and does nothing else they ask. A drainer is
started by whichever bellhop command finds work first, so with sandboxed senders active you cannot
always choose; `bellhop doctor` from a plain terminal starts it when none is running.

## 1. Messaging (send, receive, inbox)

Sending to and receiving from Claude and Pi sessions, `bellhop inbox`, and callboard.

**What it allows:** a sandboxed agent with this group can put any text, under any sender name,
into any session bellhop can reach, including agents that run outside a sandbox and may act on
what they read. That is what the product does: sender names are self-asserted and messages are
unauthenticated. It can also read every held message and every message body in the store, and
plant or remove files in the state and runtime directories; the drainer's guarantee above is what
keeps that from becoming more than messaging. bellhop's and callboard's config and
hooklinesinker's program are read-only in this group; bellhop's state and runtime directories,
hooklinesinker's state directory (its `sessions --json` takes a lock there) and callboard's data
directory are read-write.

| Path | Access | Why |
|---|---|---|
| `/private/tmp/bellhop-501` | readwrite | runtime dir: created 0700; the Claude relay's and Pi extension's sockets |
| `~/.local/state/bellhop` | readwrite | `bellhop.sqlite` (+ `-wal`, `-shm`), `drain.lock`, `drain.log`, `drain.status.json`, `launch/<agent>.lock`, `codex-<agent>.log` |
| `~/.config/bellhop/config.toml` | read | agents, `runtime_dir`, `tmux_socket` (the Pi extension reads it too) |
| `~/.local/share/hooklinesinker` | read | the `hooklinesinker` binary bellhop runs |
| `~/.local/state/hooklinesinker` | readwrite | `hooklinesinker sessions --json` takes `status.lock` and makes its directories private; it is not a read-only call |
| `~/.local/share/callboard` | readwrite | `callboard.sqlite` (+ `-wal`, `-shm`), `attachments/` |
| `~/.config/callboard/config.toml` | read | `max_attachment_bytes`, `ping_command` |

hooklinesinker also runs `tmux list-panes` and `tmux display-message`; without access to the tmux
socket directory it still lists sessions, without their pane and terminal.

## 2. The tmux socket directory: only for `launch` and `send --wake`

| Path | Access | Why |
|---|---|---|
| `/private/tmp/tmux-501` | readwrite | `bellhop launch` / `send --wake` open the agent's tmux session; the drainer checks its pane |

**What it allows:** any sandboxed agent with this grant can talk to your tmux servers, which run
outside its sandbox: `tmux new-window '<command>'` or `send-keys` into one of your panes runs any
command with none of the profile's limits. It is a sandbox escape.

**Without it:** a `send --wake` from inside the sandbox cannot start the agent. When tmux refuses
the connection, bellhop stores the message `pending` with a `cannot launch` warning and leaves
the agent alone; start it from outside the sandbox (`bellhop launch <agent>` in a plain
terminal) and the message is delivered. Messages to running agents are unaffected.

**Start the fleet's tmux server yourself, outside any sandbox.** A tmux server inherits the
sandbox of whatever starts it: if bellhop's `new-session` is the first call on that socket and
it runs inside a sandboxed session, the server and every session on it run inside that sandbox,
and an agent `command` wrapper that applies its own sandbox may fail to start nested inside it.

## 3. The Codex daemon socket directory: only to reach Codex sessions

| Path | Access | Why |
|---|---|---|
| `~/.codex/app-server-control` | read | the daemon's control socket link |
| `/private/tmp/codex-daemon-501` | readwrite | the daemon socket the link points to (connecting needs write); launched Codex agents' sockets |

**What it allows:** any sandboxed agent with this grant can speak JSON-RPC to your Codex app
servers: start or steer turns in any loaded thread (whose tools then run with the app server's
permissions, not the caller's) and answer its approval requests. bellhop never answers one; any
other client can.

**Without it:** a message to a Codex session waits (`pending`, or `no_route` when sent directly)
until a drainer outside the sandbox delivers it. Claude and Pi messages are unaffected.

## 4. Transcripts: to confirm delivery

| Path | Access | Why |
|---|---|---|
| `~/.claude/projects` | read | Claude transcripts |
| `~/.codex/sessions` | read | Codex rollouts |
| `~/.pi/agent/sessions` | read | Pi session files |
| each configured agent's homes (`CLAUDE_CONFIG_DIR/projects`, `CODEX_HOME/sessions` and `app-server-control`, `PI_CODING_AGENT_DIR/sessions`, from its `env`) | read | the same, for agents with their own homes |
| `~/.claude/sessions` | read | the Claude registry the relay reads (inside Claude's own sandbox) |

**What it exposes:** every conversation of every harness (and every configured agent) becomes
readable by every sandboxed agent with this grant.

**Without it:** messages are delivered but stay `sent` instead of `confirmed`; anything that waits
for `confirmed` (callboard's read state, `send --confirm`) does not see it.

## Checks

Run inside a sandboxed session of the profile (`--self` is required), for the groups you
granted:

```sh
# 1
nono why --self --path /private/tmp/bellhop-501 --op readwrite
nono why --self --path ~/.local/state/bellhop --op readwrite
nono why --self --path ~/.config/bellhop/config.toml --op read
nono why --self --path ~/.local/share/hooklinesinker --op read
nono why --self --path ~/.local/state/hooklinesinker --op readwrite
nono why --self --path ~/.local/share/callboard --op readwrite
nono why --self --path ~/.config/callboard/config.toml --op read
# 2
nono why --self --path /private/tmp/tmux-501 --op readwrite
# 3
nono why --self --path ~/.codex/app-server-control --op read
nono why --self --path /private/tmp/codex-daemon-501 --op readwrite
# 4
nono why --self --path ~/.claude/projects --op read
nono why --self --path ~/.codex/sessions --op read
nono why --self --path ~/.pi/agent/sessions --op read
nono why --self --path ~/.claude/sessions --op read
```

Then `bellhop doctor` inside the session reports what is reachable.
