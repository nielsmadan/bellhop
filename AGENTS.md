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
dependencies and repository hooks; it does not install `bellhop` globally.

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
  migrations). Only SQL runs inside a write transaction.
- `identity`, `targets`: the calling session (`whoami`) and target resolution; bindings follow
  their process.
- `envelope`, `routes`, `transcripts`, `delivery`: the text a session receives, the `Route` and
  `TranscriptReader` protocols (with a fake route for tests), and the delivery rules: claim, push
  outside any transaction, record the outcome, confirm from the receiver's transcript.
- `drain`: the user-wide background drainer (`bellhop _drain`, one `flock`), which pushes waiting
  messages and confirms sent ones. Its loop takes its clock, hls, routes, readers and process table
  as arguments, and one failing target never stops the others. It is spawned with `python -I` from
  `/`, without the caller's session-identity variables, and logs to `drain.log` (rotated at 1 MiB).
  `follow_bindings` updates bindings in one write transaction for the drainer and the CLI.
- `cli`: `parsing`, `presentation`, `context` (lazy config, store, sessions, process table,
  bindings; reading them starts nothing) and one module per command family (`messages`, `info`,
  `doctor`), wired in `dispatch` and `cli/__init__.py`. `dispatch` starts the drainer after a
  command when the store has work. Text output passes untrusted text through `escape_controls`.
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
- Unit tests use fakes (`tests/fakes/hooklinesinker`) and never spend inference credits. Live tests
  are marked `smoke` and run only through `just smoke`.
- New code normally needs no comments or docstrings. Use names and small typed functions. No
  `logging` module.
- `--json` prints exactly one object on stdout with `schema_version`. Diagnostics go to stderr with
  a `bellhop: ` prefix.
- Error messages in git style: lowercase, no trailing period, clauses joined with `;`.
- Commits: `feat:` / `fix:` / `chore:`, no scope, no body.

Never run a release, publish, push, or install a global command unless the user explicitly asks.
