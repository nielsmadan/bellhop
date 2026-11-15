# JSON output (schema_version 1)

Every command takes `--json` and then prints exactly one JSON object on stdout and nothing on
stderr. Keys are snake_case, times are ISO-8601 UTC (`2026-11-14T09:12:03Z`). The files in
[`tests/golden/`](../../tests/golden/) are the contract: the test suite compares the real output
with them, field for field.

## Envelopes

A successful command:

```json
{"schema_version": 1, "status": "ok", "warnings": [], "...": "the command's own fields"}
```

`warnings` lists problems that did not stop the command: hooklinesinker `problems`, skipped hls
records, unknown message ids for `status`, `cannot start the drainer: <reason>` (the command still
succeeds), and for `send --confirm` either `not confirmed within Ns` or `cannot confirm: no
transcript to read for this session` (returned at once).

A failed command:

```json
{
  "schema_version": 1,
  "status": "error",
  "exit_code": 3,
  "error": {"code": "target_ambiguous", "message": "…", "candidates": ["…"]},
  "message": {"…": "only when a message was stored before the failure"}
}
```

`error.candidates` is present for target and caller errors (possibly empty). `message` is present
whenever `send` had already stored the message: `push_rejected`, and an `interrupted` or
`internal_error` that happens after the store (during the push or the `--confirm` wait).

## The message object

Returned by `send` (`message`), and in lists by `status` and `inbox` (`messages`).

| Field | Meaning |
|---|---|
| `id` | `bh_` and 12 hex digits |
| `key` | the `--key` idempotency key, or null |
| `sender` | the self-asserted sender name (`--from`, else the caller's agent name, title or session id, else `unknown`) |
| `urgency` | `now`, `idle` or `hold` |
| `state` | see below |
| `degraded_from` | `now` when a `now` message went out as `idle` because the route cannot interrupt (by `send` or later by the drainer); the original urgency when `--or-hold` turned it into a `hold`; else null. Only `urgency: "hold"` means the message was held; a non-null `degraded_from` alone does not |
| `route` | the route that took the push (`claude-relay`, `codex-app-server`, `pi-extension`, …), or `none` (the string; `sessions[].route` uses `null` instead) |
| `detail` | the route's reply, `push raised <error>`, `outcome unknown`, `expired`, `session gone`, `session replaced in its process`, or null |
| `created_at` | when it was stored |
| `sent_at` | when the push was accepted; null unless `state` is `sent` or `confirmed` |
| `confirmed_at` | when the receiver's transcript showed it, or null |
| `target.kind` | `session` or `agent` |
| `target.agent` | the agent name for an agent target, else null |
| `target.session_id` | the session id: the target itself for a session target; for an agent target, the session it was live in when stored, then the session it was pushed to (null while the agent has no live session) |
| `target.harness` | `claude`, `codex` or `pi`, or null when unknown |
| `target.title` | the session's title when it was stored, or null |

States:

| `state` | Meaning |
|---|---|
| `pending` | stored, not pushed yet: a `hold`, a message waiting for the session to be idle, one behind an older pending message to the same session (under either its session id or its agent name), one to an agent with no live session, or one the route answered `busy` |
| `sending` | claimed for a push that is in flight |
| `sent` | the route accepted it, or the outcome is unknown (`detail` says which); never pushed again |
| `confirmed` | the envelope prefix `[bellhop <id> from` appeared in the receiver's transcript after the push |
| `read` | a `hold` taken by `bellhop inbox` |
| `failed` | rejected by the receiver, `expired` (a session-target push pending over 24 hours), `session gone`, or `session replaced in its process` (Pi `/new` and the like); a `hold` never fails this way and waits for `bellhop inbox` |

## Commands

| Command | Fields besides the envelope |
|---|---|
| `send` | `message` |
| `status <id>…` | `messages`: the known ids in the order asked; unknown ids go to `warnings` |
| `inbox` | `messages`: the `hold` items taken (or seen, with `--peek`) |
| `sessions` | `sessions`: `session_id`, `harness`, `title`, `cwd`, `phase` (`idle`, `working`, `permission`, `compacting`, `unknown`), `observed_at`, `agent` (bound name or null), `reachable`, `route` (or null), `pending` (pending messages for the session and its agent) |
| `whoami` | `session`: `session_id`, `harness`, `title`, `agent` |
| `agents` | `agents`: `agent`, `harness`, `configured`, `binding` (`state` `live`/`failed`, and `starting` once `bellhop launch` exists; `session_id`, `cause`, `detail`) or null; every configured agent and every bound name |
| `bind <agent>` | `agent`: the same shape as one `agents` entry |
| `integration show pi` | `integration`: `harness`, `file` (the packaged extension), `install_dir` (where Pi loads extensions), `lines` (the text output) |
| `integration export pi --to DIR` | `integration`: `harness`, `file` (the absolute path of the copy) |
| `doctor` | `checks`: `name`, `ok`, `detail`; `pruned`: `{"messages": n, "sockets": n}` with `--prune`, else null. When a check failed, the same two fields ride on a `doctor_failed` error |

## Error codes

Nothing is stored when one of these is returned (except `interrupted` and `internal_error` after
`send` stored the message, which carry it):

| `code` | Exit | When |
|---|---|---|
| `usage` | 2 | bad arguments, an empty body, a body over 8,000 characters (on stdin too), an invalid agent name |
| `invalid_config` | 2 | `config.toml` cannot be read or has a wrong key or value |
| `target_not_found` | 3 | no agent or live session matches; `candidates` holds close names |
| `target_ambiguous` | 3 | an id prefix or a title matches more than one live session; `candidates` lists them |
| `caller_unknown` | 3 | `whoami`, `inbox` or `bind` cannot place the caller in exactly one live session |
| `no_route` | 4 | the target is live but has no usable route (and no `--or-hold`), or `--wake` was asked for an agent that is not running |
| `hls_unavailable` | 5 | hooklinesinker is missing, gave no answer within 5 s (a first attempt of up to 4 s and a retry in the rest), failed, or speaks another protocol |
| `invalid_environment` | 1 | a relative `XDG_*` or `BELLHOP_RUNTIME_DIR` value |
| `runtime_dir_unsafe` | 1 | the runtime directory is not a directory owned by the caller with mode 0700 (`send` to a Pi session and `doctor`; `sessions`, and `send --or-hold`, report it as a warning and carry on with `reachable: false` or a stored `hold`; a `hold` never checks it), or a socket path is too long |
| `store_error` | 1 | the store cannot be opened, or has a newer schema than this bellhop |
| `binding_conflict` | 1 | `bind` to a name a live process of another session holds |
| `doctor_failed` | 1 | `doctor` found a failed check; the error also carries `checks` and `pruned` |
| `export_failed` | 1 | `integration export` could not create or write the copy |
| `internal_error` | 1 | an unexpected failure |
| `interrupted` | 130 | Ctrl-C |

Stored, then failed (the error carries `message`):

| `code` | Exit | When |
|---|---|---|
| `push_rejected` | 1 | the receiver's route answered `error` |
| `launch_failed` | 1 | reserved for `bellhop launch` (Task B7) |
