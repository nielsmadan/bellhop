# bellhop

Deliver a short message into a running coding-agent session (Claude Code, Codex, Pi) through that
harness's own in-process route, at one of three urgency levels, and start a stopped agent when
asked.

Status: early development. The store, the drainer and every command below work against
hooklinesinker, except `send --wake`, which answers `no_route` (exit 4) until launching lands. The
per-harness delivery routes arrive in later releases, so until then a live session has no route
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
bellhop doctor [--prune] [--json]
bellhop --version
```

`<target>` is, in this order: an agent name (configured or bound), an exact session id, a session-id
prefix of 4+ characters that matches one live session, or a title that one live session has.

Urgency: `now` pushes at once, mid-turn included; `idle` (the default) delivers when the current
turn ends; `hold` is never pushed and is read with `bellhop inbox`. Messages that have to wait are
delivered by a background drainer that bellhop starts on its own and that exits when nothing is
left. State lives in `$XDG_STATE_HOME/bellhop/bellhop.sqlite`; agents are configured in
`$XDG_CONFIG_HOME/bellhop/config.toml`. Every `--json` object is described in
[docs/user/json.md](docs/user/json.md).

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
