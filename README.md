# bellhop

Deliver a short message into a running coding-agent session (Claude Code, Codex, Pi) through that
harness's own in-process route, at one of three urgency levels, and start a stopped agent when
asked.

Status: early development. The store, the drainer and every command below work against
hooklinesinker. Pi sessions are reachable through bellhop's Pi extension, and Codex sessions
attached to an app server (the shared daemon, which a plain `codex` starts by default) through its
JSON-RPC socket; a stand-alone `codex --no-daemon` has no route. Claude sessions are reachable
through bellhop's relay, an MCP server the user adds once; a Claude session without it has no route
and `send` answers `no_route` (or stores a `hold` with `--or-hold`).

Messages are unauthenticated: everything runs as one user, and sender names are self-asserted.

## Requirements

- Python 3.13 or newer
- [hooklinesinker](https://github.com/nielsmadan/hooklinesinker) with its hooks installed for the
  agents you want to reach. bellhop asks it which sessions are live.

## Usage

```sh
bellhop send <target> [TEXT] [--urgency now|idle|hold] [--wake] [--or-hold] [--from NAME]
                             [--key KEY] [--confirm SECONDS] [--json]   # TEXT from stdin when omitted
bellhop status <message-id>... [--json]
bellhop inbox [--target TARGET] [--peek] [--json]   # hold items for the calling session
bellhop sessions [--json]                           # live Claude, Codex and Pi sessions
bellhop whoami [--json]
bellhop agents [--json]
bellhop bind <agent> [--json]                       # bind the calling session to a name
bellhop launch <agent> [--prompt TEXT] [--from NAME] [--key KEY] [--cause TEXT] [--no-wait] [--json]
bellhop integration show <claude|pi> [--json]       # what to install, and where
bellhop integration export <claude|pi> --to DIR     # copy the Pi extension or Claude's MCP config to DIR
bellhop doctor [--prune] [--json]
bellhop --version
```

`<target>` is, in this order: an agent name (configured or bound), an exact session id, a session-id
prefix of 4+ characters that matches one live session, or a title that one live session has.

Urgency: `now` pushes at once, mid-turn included; `idle` (the default) delivers when the current
turn ends; `hold` is never pushed and is read with `bellhop inbox`. Messages that have to wait are
delivered by a background drainer that bellhop starts on its own and that exits when nothing
deliverable is left; a message to an agent with no live session waits in the store, and binding
that agent starts the drainer again. `--wake` (and `bellhop launch`) start a configured agent that
is not running in a detached tmux session, with a scrubbed environment and the message as its
first prompt; [docs/user/agents.md](docs/user/agents.md) covers the config, the `command`
wrapper each harness needs, and what happens when a start fails. State lives in `$XDG_STATE_HOME/bellhop/bellhop.sqlite`;
agents are configured in `$XDG_CONFIG_HOME/bellhop/config.toml`. Every `--json` object is described in
[docs/user/json.md](docs/user/json.md).

Claude is reached through `bellhop _relay`, an MCP stdio server that Claude starts with each
session: `claude mcp add --scope user bellhop -- bellhop _relay` (or
`bellhop integration show claude` for the details). It listens on
`<runtime_dir>/claude-<pid>.sock` and hands each message to Claude through the messaging socket
Claude gives its own MCP children; it never uses another session's socket, never needs
`crossSessionInbound`, and bellhop never edits Claude's configuration. `bellhop` must be on the
PATH Claude starts MCP servers with, and a running Claude keeps the relay it started with until
it is restarted (after a bellhop upgrade, `doctor` names a relay on an older wire).

Pi is reached through bellhop's Pi extension: `bellhop integration export pi --to
~/.pi/agent/extensions` and Pi loads it at start. hooklinesinker 1.5.1 lists a Pi session only
when Pi runs through an executable named `pi`; `bellhop doctor` names an extension socket whose
session hooklinesinker does not list. Export the extension again after upgrading bellhop: an
extension that speaks an older wire is not reachable (`send` answers `no_route`), and `doctor`
names its socket.

Codex `now` steers the running turn (`turn/steer`); `idle` waits for the thread to go idle. bellhop
reads `CODEX_HOME` (default `~/.codex`) to find the daemon's socket and the rollouts; `bind` from a
Codex session records that session's socket, so an agent with its own `CODEX_HOME` stays reachable
from any sender.

bellhop runs the `hooklinesinker` named by `$BELLHOP_HOOKLINESINKER`, else
`$XDG_DATA_HOME/hooklinesinker/bin/hooklinesinker`, else the one on `PATH`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | failure (including a rejected push) |
| 2 | usage or invalid config |
| 3 | target not found or ambiguous; caller not in a known session |
| 4 | the target has no usable route |
| 5 | hooklinesinker unavailable |
| 130 | interrupted |

## Development

See [AGENTS.md](AGENTS.md) and the [documentation map](docs/overview.md).

## License

MIT
