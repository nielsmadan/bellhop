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

**Pi.** Copy bellhop's extension to the directory Pi loads extensions from:

```sh
bellhop integration export pi --to ~/.pi/agent/extensions
```

Pi loads it at start (`/reload` in a running Pi). Export it again after upgrading bellhop.
hooklinesinker lists a Pi session only when Pi runs as an executable named `pi`.

**Codex.** Nothing to install. bellhop reaches a Codex session through the app server it is
attached to: the shared daemon that a plain `codex` starts, found under `$CODEX_HOME` (default
`~/.codex`). A `codex --no-daemon` session has no route; see
[the Codex daemon note](../handoff/codex-daemon.md).

## 4. Check

```sh
bellhop doctor
bellhop sessions
```

`doctor` checks hooklinesinker, the state and runtime directories, the store, and each live
session's socket. `sessions` lists the live sessions bellhop can see, for example:

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
read messages, and add `bellhop inbox` to each agent's allowed commands.

## Where things live

| What | Where |
|---|---|
| Messages and bindings | `$XDG_STATE_HOME/bellhop/bellhop.sqlite` |
| Agents and settings | `$XDG_CONFIG_HOME/bellhop/config.toml` ([Agents](agents.md)) |
| Endpoint sockets | `$BELLHOP_RUNTIME_DIR`, else `runtime_dir` in the config, else `/tmp/bellhop-<uid>` (mode 0700) |
| Background drainer log | `$XDG_STATE_HOME/bellhop/drain.log` |

Messages are unauthenticated: everything runs as one user and sender names are self-asserted.
