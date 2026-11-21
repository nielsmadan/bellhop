# Documentation

The [README](../README.md) is the entry page and the CLI synopsis; the user manual is under
`user/`.

| I need to… | Start here |
| --- | --- |
| Install bellhop and send a first message | [Getting started](user/getting-started.md) |
| Send, read the inbox, understand receipts and exit codes | [Sending and reading](user/sending.md) |
| Know what bellhop does not do | [Known limits](reference/limits.md) |
| Teach an agent to use bellhop | [The skill](../skills/bellhop/SKILL.md) |
| Act on what the build left for the user (loadout, nono, hls, Codex daemon, open checks) | [Hand-off notes](handoff/) |
| Understand the accepted product and engineering choices | [Decisions](decisions/) |
| See how each harness's delivery route was measured and chosen | [Delivery routes](reference/routes.md), [ADR 0002](decisions/0002-delivery-routes.md) |
| Parse bellhop's `--json` output | [JSON output](user/json.md) |
| Configure agents and let bellhop start them | [Agents](user/agents.md), [ADR 0006](decisions/0006-launching-agents.md) |
| Work on the code | [AGENTS.md](../AGENTS.md) |
| Cut a release | [Releasing](release.md) |

## Maintenance

- Guides describe current behavior and follow code changes.
- `decisions/` records accepted choices. Supersede an ADR with a new record rather than rewriting it.
- Harvest useful knowledge from completed plans, specs, reviews and QA output, then remove them.
