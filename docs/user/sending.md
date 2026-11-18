# Sending and reading messages

```text
bellhop send <target> [TEXT] [--urgency now|idle|hold] [--from NAME] [--key KEY]
                             [--confirm SECONDS] [--or-hold] [--wake] [--json]
```

TEXT is read from stdin when it is left out (8,000 characters at most). Put `--` before a TEXT
that starts with `-`: `bellhop send enki -- "-x is ready"`. The text must be UTF-8 and may hold tab
and newline but no other control character (escape sequences, a carriage return on its own, NUL);
anything else is `usage` and names the character. A CRLF line ending is read as a newline.

## Targets

`<target>` resolves, in this order:

1. an agent name: configured in `config.toml` or bound with `bellhop bind` ([Agents](agents.md));
2. an exact session id;
3. a session-id prefix of 4 or more characters that matches one live session;
4. a title that exactly one live session has.

A Codex session's title is its thread name: the one Codex generated, or the one you gave it with
`/rename`.

No match, or more than one, is exit 3 with the candidates listed. `bellhop sessions` and
`bellhop agents` show what can be addressed.

## Urgency

| `--urgency` | What happens |
|---|---|
| `idle` (default) | delivered when the receiver's current turn ends; at once when it is idle |
| `now` | pushed at once, into a running turn too (Codex steers the turn; Claude and Pi take it at their next step); a route that cannot interrupt delivers it as `idle` and the receipt says `degraded from now` |
| `hold` | never pushed; kept until the receiver runs `bellhop inbox` |

Messages of one urgency to one receiver are delivered in the order they were sent. A `now` does
not wait behind an older `idle` or `hold`: it is pushed at once, and waits only behind an older
`now` to the same session; when a `now` has to wait after all, `send` warns `the now message
waits: <why>`. A message that has to wait is
delivered by a background drainer that bellhop starts by itself and that exits when nothing is
left to deliver.

```sh
bellhop send alpha "stop: the migration is running on prod" --urgency now
bellhop send beta "the build is green; ship when ready" --urgency hold --from enki
```

## What the receiver sees

```
[bellhop bh_242e6d6e0aab from enki] the build is green; ship when ready
```

When `hold` messages are waiting for it, a pushed message ends with
`(bellhop: N more waiting; run `bellhop inbox`)`. The receiver reads them with:

```sh
bellhop inbox          # takes them (state read)
bellhop inbox --peek   # shows them and leaves them
```

`bellhop inbox` reads the messages for the session it runs in and for the agent name bound to it;
`--target` reads another target's.

## Receipts

`send` prints the message id and its state; `bellhop status <id>…` shows them later.

| State | Meaning |
|---|---|
| `pending` | stored, not pushed yet |
| `sending` | a push is in flight |
| `sent` | the route took it, or the outcome is unknown (`detail` says which); never pushed again. bellhop keeps checking for `confirmed` while the receiver works and for 10 minutes after; `bellhop status` checks again when asked |
| `confirmed` | it appeared in the receiver's own transcript |
| `read` | a `hold` taken by `bellhop inbox` |
| `failed` | not delivered; `detail` says why |

A message is pushed at most once: bellhop never repeats a push whose outcome it does not know.

- `--confirm SECONDS` waits for `confirmed`, and warns when it does not come in time.
- `--key KEY` makes a send idempotent: a second send with the same key returns the stored message
  and pushes nothing. When that second send names another target or text, it still returns the
  stored message and warns `key '<KEY>' is already stored for <id> with another target or text`.
  An empty key is `usage`.
- `--or-hold` stores a `hold` when the receiver is live but has no usable route, instead of
  failing with `no_route` (exit 4).
- `--from NAME` sets the sender; by default it is the caller's bound agent name, else its session
  title or id, else `unknown`. Sender names are self-asserted. A `--from` with a control character
  other than tab and newline is `usage`; a default sender has its control characters replaced.
- `--wake` starts a configured agent that is not running ([Agents](agents.md)).

## Other commands

```sh
bellhop sessions              # live sessions and their phase (--json adds reachable and route)
bellhop whoami                # the session this command runs in
bellhop agents                # configured and bound agents
bellhop bind enki             # give this session the agent name enki
bellhop status bh_242e6d6e0aab
bellhop doctor                # checks; --prune deletes finished messages over 30 days old
```

Every command takes `--json` and then prints exactly one JSON object
([JSON output](json.md)). Diagnostics go to stderr, prefixed `bellhop:`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | failure (including a rejected push or a failed launch) |
| 2 | usage or invalid config |
| 3 | target not found or ambiguous; caller not in a known session |
| 4 | the target has no usable route |
| 5 | hooklinesinker unavailable |
| 130 | interrupted |
