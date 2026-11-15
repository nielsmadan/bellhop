# AGENTS.md

This file is the canonical coding-agent guidance for bellhop.

## Project

bellhop is a Python 3.13 CLI that delivers a short text into a running Claude Code, Codex or Pi
session through that harness's own in-process route. hooklinesinker (hls) is its only source of
live sessions and their state. `README.md` defines the public CLI contract; the accepted
foundations are in [ADR 0001](docs/decisions/0001-foundations.md).

## Commands

Use `just` for repository tasks:

```sh
just check
just coverage
just build
just test
```

`just check` runs Ruff lint and formatting checks, Pylint cycle detection, strict mypy over `src`
and `tests`, and the test suite, including the layering contract in `tests/test_layering.py`. That
test machine-enforces the module dependency contract: layer order, per-module import allowances,
absolute internal imports, and a stdlib-only runtime. `just coverage` is the 80% branch-coverage
gate. `just check-static` is the lint, cycle and type pass without the suite. `just smoke` runs the
live tests (`-m smoke`), which are deselected everywhere else. `just setup` installs project
dependencies and repository hooks; it does not install `bellhop` globally. `just typescript`
(run by `test`, `check` and `coverage`) installs TypeScript, Pi's types and `@types/node` into
`.cache/typescript` for the Pi extension type-check; it needs npm and the network until that cache
is filled, and the type-check test skips only where node is missing.
`just package-check` builds the wheel (uv cache in `.cache/uv`) and checks it carries the Pi
extension and Claude's MCP config.

## Architecture

- `codes`, `errors`: error codes and their exit codes; `BellhopError` carries both, plus the
  `candidates`, the `stored` message and any extra JSON `fields` its error envelope prints.
- `models`: the shared types (`Session`, `Message`, `Binding`, `PushResult` and their literals)
  and `BELLHOP_FAKE_ROUTES`, which swaps every route for the JSON-configured fake route and every
  transcript reader for the plain-file reader (tests only).
- `paths`: XDG locations; a relative XDG value is refused.
- `jsonout`: the versioned JSON envelopes.
- `hls`: runs `hooklinesinker sessions --json` and maps protocol 1 records to `Session`. It never
  reads hls state files. An hls that cannot answer is `HlsUnavailable` (exit 5), never an empty
  list.
- `config`, `runtime`: `config.toml` (agents) and the 0700 runtime directory for endpoint sockets.
- `store`: the SQLite message and binding store (WAL, `BEGIN IMMEDIATE` writes, numbered
  migrations; schema 3 records the transcript file a message's offset was taken in). Only SQL runs
  inside a write transaction.
- `identity`, `targets`: the calling session (`whoami`) and target resolution; bindings follow
  their process.
- `envelope`, `routes`, `transcripts`, `delivery`: the text a session receives, the `Route` and
  `TranscriptReader` protocols (with a fake route for tests), and the delivery rules: claim, push
  outside any transaction, record the outcome, confirm from the receiver's transcript.
  `routes/unix` is the endpoint-socket client (wire 2: `ping`, `deliver boundary|interrupt
  <session-id> <bytes>`, one reply line; connect, write and read share one deadline, 0.5 s for a
  ping and 2 s for a deliver; the wire is in `docs/reference/routes.md`); `routes/endpoint`
  (`EndpointRoute`) reaches an inside piece at `<runtime_dir>/<harness>-<pid>.sock` for every
  harness in `routes.ENDPOINTS` (Claude's relay, Pi's extension), sharing one pong per socket per
  second (`Pongs`) between `reachable`, `replaced` and the reader's transcript lookup. `Route.replaced` is true when
  the session's process now serves another session; the drainer then fails that session's
  messages. A route may raise `BellhopError` for an unusable runtime dir: `send` lets it fail the
  command unless `--or-hold` (a warning and a hold; a `hold` never probes), `sessions` reports it
  as a warning, the drainer per target.
  `transcripts/pi` reads Pi session files incrementally and counts the envelope prefix only at the
  start of an entry the session received as input. `routes/codex` talks JSON-RPC over a
  WebSocket on the app server's unix socket (`codex_rpc`, layer 1): the binding's
  `app_server_socket` (recorded by `bind` from the binding session's `CODEX_HOME`, or by `launch`
  for the agent's own server), else `$CODEX_HOME/app-server-control/app-server-control.sock` (the
  shared daemon; `CODEX_HOME` from a configured agent's `env` via `wiring.homes_env`), followed only to a
  socket the user owns in a private directory. `now` steers a running turn (`turn/steer`, ADR
  0004); a thread waiting on an approval or a question is busy for every push. `transcripts/codex` finds the rollout by `rollout-*-<thread-id>.jsonl` under
  `$CODEX_HOME/sessions`, only in the thread's UUIDv7 creation day and the days either side. A Codex binding is keyed by its
  thread: it fails while hls does not list the thread (or the app-server pid hls reports is gone)
  and comes back to `live` when hls lists the thread again.
- `wiring`: the one place that turns env and config into the route and reader setups
  (`wire(env, config) -> Wiring`), used by `cli/context` and the drainer. Both finders take the
  session and its live binding (`targets.binding_for`; the agent's binding for an agent target);
  the Codex route keys on it.
- `allowlist`: the environment variables a launched agent and the tmux client may inherit.
- `launch`, `readiness`, `tmux`, `codex_session`: starting agents (`bellhop launch`, `send
  --wake`). `launch` builds the child's argv and its allowlisted environment (`/usr/bin/env -i`,
  never a shell), decides per agent under a `flock` whether to start, queue or send, and checks a
  `starting` binding for the drainer (`start_checker`: the pane through `tmux`, startup screens
  through `readiness`, hls listing and the route; a Codex thread through its own app server's
  `thread/read` preview). `tmux` talks only to the configured server and never reuses a session
  name. `codex_session` is `bellhop _codex_session`, which runs a Codex app server and TUI and
  stops the server when the TUI exits. `readiness` mirrors `testing/screens` and is pinned to it
  by a parity test. `cli/waking` holds the CLI side; `store.begin_start`, `finish_start` and
  `fail_start` change the binding and the first message (route `launch`) together.
- `_integrations/pi/bellhop.ts`: the Pi extension, shipped in the wheel. `bellhop integration
  show pi` prints where it is and where Pi loads extensions; `export pi --to DIR` copies it.
  Its runtime-dir choice mirrors `runtime.runtime_dir` and is pinned by a parity test.
- `relay`: the Claude inside piece, `bellhop _relay`, an MCP stdio server with no tools. It
  names its socket after its parent Claude (an ancestor only when that ancestor's registry entry
  names the session the relay started in), reads its session and transcript from
  `$CLAUDE_CONFIG_DIR/sessions/<pid>.json` on every request, refuses another session's `deliver`,
  and forwards a frame (`priority: "later"` for a boundary, `"next"` for an interrupt) to the
  messaging socket and child token in its own environment. One relay per Claude holds
  `claude-<pid>.lock` (a second one waits and takes over); it removes only its own socket, on
  stdin EOF, SIGTERM, SIGHUP, SIGINT or when its parent goes. ADR 0005 records the Claude
  internals it relies on. `_integrations/claude/bellhop-mcp.json`
  is the MCP config `integration export claude` writes. `transcripts/claude` counts a received
  peer, channel or plugin entry (a `user` record or a `queued_command` attachment) whose text,
  after Claude's peer wrapper line, starts with the envelope prefix.
- `drain`: the user-wide background drainer (`bellhop _drain`, one `flock`), which pushes waiting
  messages, confirms sent ones and completes starts (`Deps.starts`, `launch.start_checker`; a
  start whose check keeps raising fails at its 90 s deadline; `store.abandon_starts` each tick).
  Its loop takes its clock, hls, routes, readers, process table and start check as arguments, and
  one failing target never stops the others. It is spawned with `python -I` from
  `/`, without the caller's session-identity variables, and logs to `drain.log` (rotated at 1 MiB).
  `follow_bindings` updates bindings in one write transaction for the drainer and the CLI.
- `cli`: `parsing`, `presentation`, `context` (lazy config, store, sessions, process table,
  bindings; reading them starts nothing) and one module per command family (`messages`,
  `launching`, `info`, `integration`, `doctor`), wired in `dispatch`; `command` parses and reports
  errors. `cli/__init__.py` holds only `main`, which imports lazily so `_relay`, `_drain` and
  `_codex_session` start without loading the command modules. `dispatch` starts the
  drainer (at most once per command, and not before `doctor`, which checks for it itself) before
  every command when the store has deliverable work (`store.DELIVERABLE`: messages to an agent without a
  live or starting binding are not), `send` and `bind` again after they add work; a failed start
  is a warning, never an error. Text output passes untrusted text through `escape_controls`.
- `testing`: the live-smoke isolation helper (also used by callboard's smoke tests). `SmokeRun`
  builds isolated homes under `.cache/smoke/<run>/`, installs an isolated hls consumer and hooks,
  launches Claude, Codex and Pi in prompting mode on `tmux -L bellhop-smoke` with a scrubbed
  environment, waits for readiness, and tears everything down.

Internal modules import concrete seams, never the package root. Keep the runtime standard-library
only (`dependencies = []`).

## Conventions

- Target Python 3.13, Ruff at 100 columns, strict mypy, at least 80% branch coverage.
- New behavior gets observable tests in the matching `tests/test_<module>.py` file. The autouse
  fixture in `tests/conftest.py` isolates XDG dirs, HOME and PATH under `tmp_path` and clears the
  harness session-id variables and `BELLHOP_FAKE_ROUTES`; the autouse `drainer_starts` fixture
  replaces the drainer start with a recorder. Shared helpers (`bell`, `make_message`,
  `make_session`, `check_golden`, the session ids and `NOW`) live in `conftest.py`, never in a test
  module.
- Unit tests use fakes (`tests/fakes/hooklinesinker`, the Codex app server in
  `tests/fake_app_server.py`) and never spend inference credits. Live tests
  are marked `smoke` and run only through `just smoke`.
- New code normally needs no comments or docstrings. Use names and small typed functions. No
  `logging` module.
- `--json` prints exactly one object on stdout with `schema_version`. Diagnostics go to stderr with
  a `bellhop: ` prefix.
- Error messages in git style: lowercase, no trailing period, clauses joined with `;`.
- Commits: `feat:` / `fix:` / `chore:`, no scope, no body.

Never run a release, publish, push, or install a global command unless the user explicitly asks.
