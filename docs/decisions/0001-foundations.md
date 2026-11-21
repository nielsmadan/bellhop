# 0001 — Foundations

**Status:** accepted

**Recorded:** 2026-11-13

## Context

bellhop delivers a short text into a coding-agent session (Claude Code, Codex, Pi) through that
harness's own in-process route, and can start a stopped agent. Other tools, callboard first, call
it to ring an agent. These are the product and engineering choices made before the first line of
code; later records refine them.

## Decision

### Product

- Sessions are addressed by session id. A title is an alias that resolves only when exactly one
  live session has it. A configured agent name maps to at most one live session.
- hooklinesinker (hls) is the only source of live sessions and their state. bellhop goes through
  its CLI (`sessions --json`, protocol 1) and never reads its state files. hls is one-way: it
  cannot inject. Its phase is "last event seen", not a heartbeat, so it is a hint, not proof of
  idleness.
- Three urgency levels: `now` (push immediately, mid-turn included), `idle` (deliver when the
  current turn ends; at once if already idle), `hold` (never pushed; read with `bellhop inbox`).
  The default is `idle`. bellhop installs no turn hooks of its own in the harnesses.
- A message is pushed at most once. A push whose outcome is not known is never repeated.
- Delivery is confirmed from the receiver's own transcript, never from the sender's reply.
- Messages are unauthenticated: everything runs as one user and sender names are self-asserted.
  The docs say so; there is no auth.
- Claude: never `crossSessionInbound: accept`, and never imitate another Claude session on the
  peer socket (partly lifted by [ADR 0008](0008-claude-socket.md), for a session whose own
  settings say `accept`). The inside piece is chosen by a spike among an MCP stdio relay, the same relay
  started from a mod or a `SessionStart` command hook, and a mod calling `$.prompt.submit`; the MCP
  relay is the default if the spike is inconclusive.
- Codex: reach the session through the app server it is attached to: the shared daemon when it
  runs, else a per-session server started by `bellhop launch`. A plain stand-alone Codex session is
  `hold`-only.
- Pi: an extension calling `sendUserMessage`.

### Engineering

- Python >= 3.13, uv, hatchling, `src/` layout, argparse, standard library only at runtime
  (`dependencies = []`, enforced by `tests/test_layering.py`). TypeScript only where a harness
  requires it (Pi extension, a Claude mod).
- State in `$XDG_STATE_HOME/bellhop` (default `~/.local/state/bellhop`), config in
  `$XDG_CONFIG_HOME/bellhop/config.toml`. A relative XDG value is refused. Files 0600, dirs 0700.
- Runtime sockets in `$BELLHOP_RUNTIME_DIR`, default `/tmp/bellhop-<uid>`, which must be a directory
  owned by the caller with mode 0700. Socket paths stay under 104 bytes. A listener binds a
  temporary name and renames it into place, and on exit unlinks the socket only if it is still its
  own inode.
- SQLite: WAL, `synchronous=NORMAL`, `foreign_keys=ON`, `isolation_level=None` with explicit
  `BEGIN IMMEDIATE`, connection `timeout=30`, 0600. Only SQL runs inside a write transaction. A
  `meta.schema_version` with numbered forward migrations; a newer schema is refused. Rows are
  ordered by an autoincrement `seq`, never by timestamp.
- No resident daemon. All waiting is done by one lazy, user-wide, detached drainer process guarded
  by one `flock`, so a caller never waits on it and cannot kill it.
- Deadlines nest: an endpoint reply 2 s; an hls call 5 s with one retry (superseded by
  [ADR 0003](0003-hls-deadline.md)); `bellhop send` returns
  within 10 s unless `--confirm` asks for longer.
- Exit codes: 0 ok; 1 failure; 2 usage; 3 target not found or ambiguous; 4 target has no usable
  route; 5 hls unavailable; 130 interrupted. A broken pipe is handled quietly.
- `--json` prints exactly one object on stdout with `schema_version`; errors carry
  `status: "error"`, `exit_code` and `error: {code, message}`. Keys are snake_case, times ISO-8601
  UTC, durations integer `_ms`. Diagnostics go to stderr with a `bellhop: ` prefix, in git style.
- No `logging` module, no comments or docstrings by default, small typed functions, internal
  modules never import the package root.
- Unit tests use fake hls, fake agents and fake routes and never spend inference credits. Live
  tests are marked `smoke` and run only through `just smoke`, against agents in isolated homes on a
  dedicated tmux server.

## Consequences

- bellhop installs as one user-level tool with no dependency tree.
- Without hls, bellhop cannot see any session: hls being unavailable is an error (exit 5), never an
  empty result.
- Each harness needs its own small inside piece; bellhop never types into a terminal.
