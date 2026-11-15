# Loadout source entries for bellhop

**What to do:** add these entries to your global loadout source, then `loadout sync`. Nothing here
has been applied. Formats are from the loadout repository's docs (paths cited); `<source>` is your
global loadout source directory.

## 1. The Claude relay (MCP server)

Equivalent of `claude mcp add --scope user bellhop -- bellhop _relay`. In `<source>/mcp.toml`
(format: loadout `docs/reference/servers.md`, "The input is `<source>/mcp.toml`"):

```toml
[bellhop]
transport = "stdio"
command = "bellhop"
args = ["_relay"]
```

Loadout writes it to `${CLAUDE_CONFIG_DIR:-~}/.claude.json` → `mcpServers`, the same key `claude
mcp add --scope user` writes, as `{"type": "stdio", "command": "bellhop", "args": ["_relay"]}`.

**Check before syncing:** the `mcp` slice is automatic: a server in `mcp.toml` goes to every
harness the source configures, not only Claude (`servers.md`, "`mcp` is **automatic**"). In a
Codex or Pi session the relay finds no Claude messaging channel and does not listen, so it does
nothing but costs one idle process per session. Loadout's only opt-out is per harness and turns off
every server for that harness (`mcp = false` in the harness block). Decide which you prefer; there
is no per-server harness filter.

`bellhop` must be on the `PATH` Claude starts MCP servers with. Restart running Claude sessions
after syncing.

## 2. The Pi extension

Loadout has no extension slice; a loose `.ts` file goes through module-config, which copies
`<source>/module-config/<harness>/<path>` to `<harness config dir>/<path>` unchanged (loadout
`docs/reference/module-config.md`). Copy the packaged file into your source:

```sh
bellhop integration export pi --to <source>/module-config/pi/extensions
```

It lands at `${PI_CODING_AGENT_DIR:-~/.pi/agent}/extensions/bellhop.ts`, where Pi loads it.

**Unconfirmed:** loadout documents and tests module-config `.ts` files only for OpenCode plugins;
a Pi `extensions/*.ts` file follows from the same mechanism but has no example in loadout. Do not
use the name `extensions/loadout-hooks.ts` (loadout's hooks slice writes it). Re-run the export
after every bellhop upgrade: an extension on an older wire is not reachable.

## 3. The skill

A skill is a folder holding `SKILL.md` in `<source>/skills/` (loadout
`docs/user/reference/configuration.md`, "That is the only skill form"). Copy bellhop's:

```sh
cp -R <bellhop checkout>/skills/bellhop <source>/skills/bellhop
```

It reaches Claude (`~/.claude/skills`), Codex (`~/.codex/skills`) and Pi (`~/.pi/agent/skills`).
To narrow it, add to the manifest:

```toml
[skills.bellhop]
agents = ["claude", "codex", "pi"]
```

## 4. Allow `bellhop inbox`

A pushed message with held messages behind it tells the agent to run `bellhop inbox`. Add
`bellhop inbox` (and `bellhop send` if agents should reply) to each harness's allowed commands in
your loadout permissions. The format is loadout's `permissions.toml`; this note does not give the
entry because the format was not checked for this run.
