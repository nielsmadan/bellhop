# Getting started

bellhop delivers a short message into a running Claude Code, Codex or Pi session, and keeps
messages a session reads later. It learns which sessions are live from
[hooklinesinker](https://github.com/nielsmadan/hooklinesinker) (hls); it never reads a harness's
files to find sessions and never types into a terminal.

## 1. Requirements

- Python 3.13 or newer, and [uv](https://docs.astral.sh/uv/).
- hooklinesinker with its hooks installed for every harness you want to reach. bellhop runs the
  `hooklinesinker` named by `$BELLHOP_HOOKLINESINKER`, else
  `$XDG_DATA_HOME/hooklinesinker/bin/hooklinesinker`, else the one on `PATH`.
- tmux, only if bellhop should start agents (`bellhop launch`, `send --wake`).

## 2. Install

From a checkout of this repository:

```sh
uv tool install .
bellhop --version
```

## 3. Connect each harness

A session is reachable only through its harness's own input route.

**Claude Code.** bellhop's relay is an MCP stdio server that Claude starts with each session:

```sh
claude mcp add --scope user bellhop -- bellhop _relay
```

`bellhop` must be on the `PATH` Claude starts MCP servers with. A running Claude keeps the relay
it started with: restart it after installing or upgrading bellhop. `bellhop integration show
claude` prints the same instructions and the packaged config file, for `claude --mcp-config`.

Without the relay, bellhop can write to a Claude session's own messaging socket instead, when
Claude's user settings (`~/.claude/settings.json`, or `$CLAUDE_CONFIG_DIR/settings.json`) say:

```json
{ "crossSessionInbound": "accept" }
```

bellhop reads that file to decide, so the setting has to be there and not only in a `--settings`
flag or a managed policy. It uses the socket only when the config directory, its `sessions`
directory and the socket's directory are yours and not writable by anyone else. Such a session shows the route `claude-socket` in `bellhop sessions --json`; a
session whose relay answers keeps `claude-relay`. `accept` is Claude's own switch and is wider than
bellhop: Claude then delivers a message from any process running as you, other Claude sessions
included, without holding it for your review.

**Pi.** Copy bellhop's extension to the directory Pi loads extensions from:

```sh
bellhop integration export pi --to ~/.pi/agent/extensions
```

Pi loads it at start (`/reload` in a running Pi). Export it again after upgrading bellhop.
hooklinesinker lists a Pi session only when Pi runs as an executable named `pi`.

**Codex.** Nothing to install, but start Codex through bellhop:

```sh
bellhop codex
bellhop codex resume --last
```

Every argument goes to `codex` unchanged. `bellhop codex` starts an app server for that session on
a socket in bellhop's runtime directory, attaches the Codex terminal UI to it, and stops it when
you quit. A subcommand other than `resume` and `fork` (`exec`, `login`, …) and a Codex that cannot
use an app server of bellhop's (`--remote`, `--no-daemon`, `--oss`, `--profile`) run as plain
`codex`. If the app server cannot start, bellhop says so and starts plain `codex`: the session
works but cannot be reached.

A plain `codex` is reachable only while it is attached to Codex's shared daemon under
`$CODEX_HOME` (default `~/.codex`). Codex 0.167.1 does not attach when it is started with
`--no-daemon`, `--profile`, `--oss`, `--search` or a `-c` override other than a feature switch; it
then says "Running without the shared background server" and has no route. See
[ADR 0007](../decisions/0007-codex-session-servers.md).

## 4. Check

```sh
bellhop doctor
bellhop sessions
```

`doctor` checks hooklinesinker, the state and runtime directories, the store, each answering
relay and extension, and each Claude session that accepts peer messages. `sessions` lists the live sessions bellhop can see, for example:

```
ID        HARNESS  PHASE    TITLE
2e2042a9  claude   idle     alpha
7f00aa11  pi       working  beta
```

## 5. Send a message

```sh
bellhop send alpha "the build is green; ship when ready"
```

The receiver sees `[bellhop bh_… from <you>] the build is green; ship when ready`. By default
the message waits until the receiver's current turn ends; see [Sending](sending.md) for the
three urgency levels, receipts and the inbox.

Give agents the [bellhop skill](../../skills/bellhop/SKILL.md) so they know how to send and
read messages (`bellhop integration export skill --to <skills dir>` writes
`<skills dir>/bellhop/SKILL.md`), and add `bellhop inbox` to each agent's allowed commands.

## 6. See what happened

bellhop keeps a trace of what it did: every command with its result, every push with the
receiver's answer, why a session could not be reached, and what the drainer, the Claude relay and
`bellhop codex` did.

```sh
bellhop log
bellhop log bh_1f0c9a2b7e44
bellhop log no_route -n 10
bellhop log --errors
```

`bellhop log` prints the last 40 events; each TEXT keeps only the events that contain it (a
message id, a session id, an event name, an error code). `--errors` keeps only what went wrong: a
command that failed, a push the receiver refused or did not answer, a message that failed, and
what the drainer or `bellhop codex` could not do. The trace records command lines,
including a message given as an argument, cut at 300 characters. `BELLHOP_TRACE=off` in a
command's environment stops that command from writing to it.

## Where things live

| What | Where |
|---|---|
| Messages and bindings | `$XDG_STATE_HOME/bellhop/bellhop.sqlite` |
| Agents and settings | `$XDG_CONFIG_HOME/bellhop/config.toml` ([Agents](agents.md)) |
| Endpoint sockets | `$BELLHOP_RUNTIME_DIR`, else `runtime_dir` in the config, else `/tmp/bellhop-<uid>` (mode 0700) |
| Background drainer log | `$XDG_STATE_HOME/bellhop/drain.log` |
| Trace of what bellhop did | `$XDG_STATE_HOME/bellhop/trace.jsonl` (4 MiB, then one older file `trace.jsonl.1`) |

Messages are unauthenticated: everything runs as one user and sender names are self-asserted.
