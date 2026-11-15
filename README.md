# bellhop

Deliver a short message into a running coding-agent session (Claude Code, Codex, Pi) through that
harness's own in-process route, at one of three urgency levels, and start a stopped agent when
asked.

Status: early development. Only `bellhop sessions` and `bellhop --version` work so far.

## Requirements

- Python 3.13 or newer
- [hooklinesinker](https://github.com/nielsmadan/hooklinesinker) with its hooks installed for the
  agents you want to reach. bellhop asks it which sessions are live.

## Usage

```sh
bellhop sessions          # live Claude, Codex and Pi sessions: short id, harness, phase, title
bellhop sessions --json   # the same as one JSON object (schema_version 1)
bellhop --version
```

bellhop runs the `hooklinesinker` named by `$BELLHOP_HOOKLINESINKER`, else
`$XDG_DATA_HOME/hooklinesinker/bin/hooklinesinker`, else the one on `PATH`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | failure |
| 2 | usage |
| 5 | hooklinesinker unavailable |
| 130 | interrupted |

## Development

See [AGENTS.md](AGENTS.md) and the [documentation map](docs/overview.md).

## License

MIT
