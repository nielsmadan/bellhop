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
transcript to read for this session` (returned at once). A `send --wake` that starts or queues
behind a start warns `--confirm does not wait for a message that starts its agent` when it was
given `--confirm`: this is routine for a wake, and the message is confirmed later like any other
(`bellhop status <id>`). A wake that cannot start the agent warns
`cannot launch '<agent>': <cause>; the message waits`. A `send` without `--wake` to a configured
agent that is not running warns `'<agent>' is not running; the message waits (use --wake to start
it)`. A `now` to a live session that is stored `pending` without a `degraded_from` warns `the now message waits:
<why>` (the route's answer, or `behind an older now message to the same session`). A `--key` (of `send`, or of `launch` with its `--prompt`) that
is already stored for another target or text returns the stored message and warns `key '<key>' is
already stored for <id> with another target or text`. `status` warns `cannot recheck transcripts:
<reason>` when it cannot read the live sessions, and then reports the stored states.

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

`error.candidates` is present for target and caller errors (possibly empty); without `--json` the
same candidates follow the error line on stderr, one per line. `message` is present
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
| `route` | the route that took the push (`claude-relay`, `codex-app-server`, `pi-extension`, …), `launch` for the first message of a start (passed as the agent's prompt argument), or `none` (the string; `sessions[].route` uses `null` instead) |
| `detail` | the route's reply, `push raised <error>`, `outcome unknown`, `expired`, `session gone`, `session replaced in its process`, `prompt passed to tmux <target>` (a first message while its agent starts), `started with this prompt`, `start failed: …`, `outcome unknown; start failed: …`, `start abandoned: …`, `outcome unknown; start abandoned: …`, or null |
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
| `pending` | stored, not pushed yet: a `hold`, a message waiting for the session to be idle, an `idle` behind an older pending message to the same session or a `now` behind an older pending `now` (under either its session id or its agent name), one to an agent with no live session, one the route answered `busy`, or the first message of a start (route `launch`) until the agent is ready |
| `sending` | claimed for a push that is in flight |
| `sent` | the route accepted it, or the outcome is unknown (`detail` says which); never pushed again |
| `confirmed` | the envelope prefix `[bellhop <id> from` appeared in the receiver's transcript after the push. bellhop keeps looking for a `sent` message for 10 minutes after it was sent, longer while its session is working (until 10 minutes after the session was last seen working), and never past 24 hours; `bellhop status` also looks again each time it is asked |
| `read` | a `hold` taken by `bellhop inbox` |
| `failed` | rejected by the receiver, `expired` (a session-target push pending over 24 hours), `session gone`, `session replaced in its process` (Pi `/new` and the like), the first message of a start that failed (`start failed: …`), or `start abandoned: …` when the agent's binding changed while it was starting; a `hold` never fails this way and waits for `bellhop inbox` |

## Commands

| Command | Fields besides the envelope |
|---|---|
| `send` | `message` |
| `status <id>…` | `messages`: the known ids in the order asked; unknown ids go to `warnings`. A `sent` message whose session is live is checked against its transcript first, so `status` can return it `confirmed` |
| `inbox` | `messages`: the `hold` items taken (or seen, with `--peek`), each with its text in `body` |
| `sessions` | `sessions`: `session_id`, `harness`, `title`, `cwd`, `phase` (`idle`, `working`, `permission`, `compacting`, `unknown`), `observed_at`, `agent` (bound name or null), `reachable`, `route` (or null), `reason` (why an unreachable session cannot be reached and what would fix it; null when it is reachable or was not checked), `pending` (pending messages for the session and its agent). Route checks stop after 5 s in total; the last rows then show `reachable: false` unchecked and a warning says how many |
| `whoami` | `session`: `session_id`, `harness`, `title`, `agent` |
| `agents` | `agents`: `agent`, `harness`, `configured`, `binding` (`state` `starting`/`live`/`failed`, `session_id`, `cause`, `detail`) or null; every configured agent and every bound name. A failed start's `detail` starts with `start failed:` and says how to attach |
| `bind <agent>` | `agent`: the same shape as one `agents` entry |
| `launch <agent>` | `agent`: the same shape as one `agents` entry; `message`: the `--prompt` message, or null without one. When `launch` started the agent: with `--no-wait` the binding is `starting` and the message `pending` with route `launch`; after a wait the binding is `live` and the message `sent` (detail `started with this prompt`), and it becomes `confirmed` once the drainer finds it in the new session's transcript. When the agent was already live, the prompt is an ordinary `idle` message |
| `integration show <claude\|codex\|pi\|skill>` | `integration`: `harness`, `file` (the packaged Pi extension, Claude MCP config or skill; null for codex, which needs nothing installed), `install_dir` (where Pi loads extensions; null otherwise), `lines` (the text output) |
| `integration export <claude\|pi\|skill> --to DIR` | `integration`: `harness`, `file` (the absolute path of the copy; for `skill` it is `DIR/bellhop/SKILL.md`) |
| `log [TEXT…] [-n N] [--errors]` | `file`: the trace file; `events`: the last N (default 40) that contain every TEXT, oldest first; with `--errors`, only those that record a failure. Each has `at` (UTC), `src` (`cli`, `drain`, `relay` or `codex`), `pid`, `event` and that event's own fields: `command` (`argv`, `cwd`, `caller`, `exit`, `code`, `error`, `ms`), `probe` (`session`, `harness`, `route`, `reachable`, `reason`), `push` (`message`, `session`, `route`, `interrupt`, `phase`, `outcome`, `detail`, `ms`), `seen` (`message`, `session`, `transcript`), `message` (the message object), `sessions` (`listed`, `reachable`), `start`, `exit`, `fail`, `error` and `hls` from the drainer, `listening` and `note` from the relay, `start`, `listening`, `stop` and `failed` from `bellhop codex`. An empty field is left out. The fields are for people and may grow |
| `doctor` | `checks`: `name`, `ok`, `detail`; `pruned`: `{"messages": n, "sockets": n}` with `--prune`, else null. When a check failed, the same two fields ride on a `doctor_failed` error; with `--prune` the checks are run again after pruning and the exit code follows that second run. Besides hooklinesinker, the directories, the store and pending work, `checks` has `socket paths` (fails when the longest endpoint socket name would not fit in the runtime directory), `sockets` (dead ones), the endpoints, `drainer` while one runs (fails when it cannot run hooklinesinker, with its pid: stop it; the next bellhop command starts a fresh one) and `fake routes` when `BELLHOP_FAKE_ROUTES` is set but ignored. `--prune` deletes `confirmed`, `read`, `failed` and `sent` messages older than 30 days and dead sockets; `pending` messages (to an agent that is not running, for example) and `hold` messages are kept |

## Error codes

Nothing is stored when one of these is returned (except `interrupted` and `internal_error` after
`send` stored the message, which carry it):

| `code` | Exit | When |
|---|---|---|
| `usage` | 2 | bad arguments (a TEXT starting with `-` needs `--` before it), an empty body, a body over 8,000 characters (on stdin too), a body, `--from` or `--cause` with a control character other than tab and newline (C1 included), stdin that is not UTF-8, an empty `--key`, an invalid agent name (lowercase letters, digits, `_` and `-`, at most 64 characters), a Codex `launch` without `--prompt`, `bind` to a name configured for another harness |
| `invalid_config` | 2 | `config.toml` cannot be read or has a wrong key or value; `launch` of an agent without `command`, one whose `cwd` is not a directory, with no tmux on `PATH` or at `/opt/homebrew/bin`, `/usr/local/bin` or `/usr/bin`, or with an environment variable name the agent cannot be given |
| `target_not_found` | 3 | no agent or live session matches; `candidates` holds close names |
| `target_ambiguous` | 3 | an id prefix or a title matches more than one live session; `candidates` lists them |
| `caller_unknown` | 3 | `whoami`, `inbox` or `bind` cannot place the caller in exactly one live session |
| `no_route` | 4 | the target is live but has no usable route (and no `--or-hold`); the message ends with the reason |
| `hls_unavailable` | 5 | hooklinesinker is missing, gave no answer within 5 s (a first attempt of up to 4 s and a retry in the rest), failed (the message quotes the first line of its output, at most 200 characters), or speaks another protocol |
| `invalid_environment` | 1 | a relative `XDG_*` or `BELLHOP_RUNTIME_DIR` value |
| `runtime_dir_unsafe` | 1 | the runtime directory is not a directory owned by the caller with mode 0700 (`send` to a Claude or Pi session and `doctor`; `sessions`, and `send --or-hold`, report it as a warning and carry on with `reachable: false` or a stored `hold`; a `hold` never checks it), or a socket path is too long; `launch` of a Codex agent when the runtime directory is unusable (nothing stored, nothing started) |
| `store_error` | 1 | the store cannot be opened, or has a newer schema than this bellhop |
| `binding_conflict` | 1 | `bind` to a name a live process of another session holds, or to an agent that is `starting` |
| `doctor_failed` | 1 | `doctor` found a failed check; the error also carries `checks` and `pruned` |
| `export_failed` | 1 | `integration export` could not create or write the copy |
| `launch_failed` | 1 | `launch` stopped before tmux opened a session: no free session name (`<agent>` to `<agent>-9` all exist), or tmux gave no answer or did not run. Nothing is stored and the binding is unchanged (`send --wake` stores the message `pending` with a warning instead) |
| `internal_error` | 1 | an unexpected failure |
| `interrupted` | 130 | Ctrl-C |

Stored, then failed (the error carries `message`):

| `code` | Exit | When |
|---|---|---|
| `push_rejected` | 1 | the receiver's route answered `error` (a `--key` replay of a message whose start failed answers `launch_failed` instead) |
| `launch_failed` | 1 | a start was attempted (tmux was asked to open the session) and failed. tmux refused the session: the message is `failed` with the same detail. The agent got its prompt but stopped at a screen, closed its pane or was not ready within 90 s (`launch` that waited): the message is `sent` with detail `outcome unknown; start failed: …`, since the agent may still act on it. `send --wake` to an agent whose last start failed: the new message is stored `failed` with that start's detail, and the error says to run `bellhop launch` |
