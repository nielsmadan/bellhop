# bellhop

Deliver a short message into a running coding-agent session (Claude Code, Codex, Pi) through that
harness's own in-process route, at one of three urgency levels, and start a stopped agent when
asked.

- **Claude Code** through bellhop's relay, an MCP server you add once.
- **Codex** through the app server the session is attached to (the shared daemon a plain `codex`
  starts, or the one `bellhop launch` starts); a `codex --no-daemon` session has no route.
- **Pi** through bellhop's Pi extension.

bellhop learns which sessions are live from
[hooklinesinker](https://github.com/nielsmadan/hooklinesinker), confirms delivery from the
receiver's own transcript, and never types into a terminal. Messages are unauthenticated:
everything runs as one user, and sender names are self-asserted.

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

`<target>` is an agent name (configured or bound), an exact session id, a session-id prefix of 4+
characters that matches one live session, or a title that one live session has. Urgency: `now`
pushes at once, mid-turn included; `idle` (the default) delivers when the current turn ends;
`hold` is never pushed and is read with `bellhop inbox`.

## Documentation

| | |
|---|---|
| [Getting started](docs/user/getting-started.md) | install, connect each harness, first message |
| [Sending and reading](docs/user/sending.md) | targets, urgency, the inbox, receipts, exit codes |
| [Agents](docs/user/agents.md) | naming sessions, configuring agents, `launch` and `--wake` |
| [JSON output](docs/user/json.md) | every `--json` object and error code |
| [Known limits](docs/reference/limits.md) | what bellhop does not do, and what you see |
| [The bellhop skill](skills/bellhop/SKILL.md) | how an agent should use bellhop |

## Development

See [AGENTS.md](AGENTS.md) and the [documentation map](docs/overview.md).

## License

MIT
