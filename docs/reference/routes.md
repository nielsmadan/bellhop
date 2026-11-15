# Delivery routes: measurements

Measured on 2026-11-13/14 on the development Mac (macOS 27.2) by a spike. The decision
built on these numbers is [ADR 0002](../decisions/0002-delivery-routes.md). The spike code is in
[`spikes/`](../../spikes/) and every raw record is in `spikes/results/` (`measure-main.jsonl`, plus
`measure-pi-fill.jsonl` for one Pi session re-run after a driver bug).

## Versions

| Component | Version |
|---|---|
| Claude Code | 2.1.325 (`haiku`, prompting mode `--permission-mode default`) |
| Codex CLI and TUI | 0.167.1 (`gpt-6-luna`, effort low, `-a on-request -s read-only`) |
| Codex app server, per-session (`codex app-server --listen`) | 0.167.1 |
| Codex shared daemon (isolated) | 0.167.1 for reps 1–2; 0.169.0 for rep 3 (it updated itself, see Known limits) |
| Pi | 1.5.2 on node 24 (`openrouter/deepseek/deepseek-v4.1-flash`, thinking off) |
| hooklinesinker | 1.5.1 |
| tmux | 3.8 |

Every agent ran in an isolated home under `.cache/smoke/<run>/` on `tmux -L bellhop-smoke`, launched
by `bellhop.testing` (no permission bypass flags anywhere). A one-rep pilot run before the
measurement found harness bugs (Claude mid-turn deliveries arrive as `attachment` rows; Codex needs
an explicit escalation request to open a permission prompt; a `/new` dialog on daemon-attached Codex)
and was superseded; its records are not counted.

## Method

Each candidate got three fresh sessions per repetition: one for S1, S2, S10 and S7 in that order,
one for S3 and one for S3b, so a dialog the test opens is never answered. Every push carries
`[bellhop <token> from spike] … reply with exactly ACK <token>`. Delivery is judged only from the
receiver's own transcript (Claude `projects/*.jsonl`, Codex `sessions/**/rollout-*.jsonl`, Pi
`sessions/**/*.jsonl`), never from the sender's reply.

| Scenario | Set-up | Pass |
|---|---|---|
| S1 | idle session, `deliver boundary` | token arrives as a user entry within 90 s |
| S2-boundary | prompt runs `sleep 20`, push 8 s in with `deliver boundary` | delivered; timing recorded |
| S2-interrupt | same, with `deliver interrupt` | delivered; timing recorded |
| S3 | permission prompt open (Claude: unapproved `echo hi > perm.txt`; Codex: escalation request) | prompt still open 27 s after a boundary and an interrupt push, `perm.txt` absent |
| S3b | question dialog open (Claude `AskUserQuestion`; Codex asked for `request_user_input`) | dialog still open, unanswered, after both pushes |
| S7 | a typed draft left in the composer, then a push | delivered, draft still in the composer, draft never submitted |
| S10 | `/clear` (Claude) or `/new` (Codex, Pi) in the same process, then a push resolved the way bellhop resolves (Claude/Pi: the process's socket; Codex: the session hls reports for the work dir) | token lands in the new session's transcript |

Timing: `mid-turn` when the token entry precedes the running turn's final answer, `boundary` when it
follows it. A turn whose final answer never arrives also counts as `mid-turn`; on every Codex and Pi
S2-interrupt row that is what happened (the push displaced the turn's own answer), and for those
rows the mid-turn landing was confirmed by reading a transcript (`pi-ext-r2-a`; rollout
`01a1d71f…`: prompt 22:56:51, steer 22:56:58, then the ACK and no `DONE`). `busy_at_push` is false
on all six Pi S2 rows because the spike's busy pattern does not match Pi's screen, not because the
turn had ended.

## Candidates

| Name | Harness | What it is | `idle` push | `now` push |
|---|---|---|---|---|
| mcp | Claude | stdio MCP server (`--mcp-config`) that also listens on a unix socket and forwards a peer frame to `CLAUDE_CODE_MESSAGING_SOCKET` with `CLAUDE_CODE_MESSAGING_TOKEN` from its own environment | frame `priority: "later"` | frame `priority: "next"` |
| hook | Claude | the same relay, started detached by a `SessionStart` command hook | same | same |
| modrelay | Claude | the same relay, spawned by a mod with `$.process.spawn` | same | same |
| modsubmit | Claude | a mod spawns a listener and calls `$.prompt.submit({ text, asUser: true })` per line it prints | `$.prompt.submit` | `$.prompt.submit` (no other form exists) |
| daemon-start | Codex | TUI attached to the shared daemon; JSON-RPC over the daemon's socket | wait until the thread is idle, `turn/start` | `turn/steer` into the running turn, else `turn/start` |
| daemon-queue | Codex | TUI attached to the shared daemon; `codex queue --thread <id> --message` | `codex queue` | none |
| remote-start | Codex | `codex app-server --listen unix://…` per session, TUI `--remote` | as daemon-start | as daemon-start |
| ext | Pi | extension listening on a unix socket, calling `pi.sendUserMessage` | `deliverAs: "followUp"` | `deliverAs: "steer"` |

The Claude and Pi endpoints speak the endpoint-socket wire below.

### Endpoint sockets

- The inside piece listens on `<runtime_dir>/<harness>-<pid>.sock`, mode 0600, where `<pid>` is
  the agent process hooklinesinker reports. It binds a temporary name in the same directory,
  renames it into place, and on exit unlinks the socket only if it is still its own inode. The
  whole path must stay under 104 bytes.
- `<runtime_dir>` is `$BELLHOP_RUNTIME_DIR`, else `runtime_dir` in
  `$XDG_CONFIG_HOME/bellhop/config.toml`, else `/tmp/bellhop-<uid>`. It must be absolute, a
  directory owned by the user, mode 0700. A launcher has to pass `BELLHOP_RUNTIME_DIR` and
  `XDG_CONFIG_HOME` into the agent's environment when it sets them for bellhop.
- Wire version 2. The client writes one header line, then the UTF-8 envelope, shuts down its
  write half and reads one reply line. Connect, write and read share one deadline: 2 s for a
  `deliver`, 0.5 s for a `ping`.

| Header | Meaning |
|---|---|
| `ping` | answer `ok pong {"wire":2,"session":<id or null>,"transcript":<absolute path or null>}`; deliver nothing |
| `deliver boundary <session-id> <bytes>` | deliver at the turn boundary (at once when idle) |
| `deliver interrupt <session-id> <bytes>` | deliver now, mid-turn included |

Replies: `ok <detail>` (accepted), `busy <detail>` (nothing was queued; try later), `error
<detail>` (rejected). A refused connection is `busy`; no reply in time, or a connection dropped
after the write, is `unknown`; a write that did not finish delivered nothing and is `busy`.
`<bytes>` is the envelope's UTF-8 length; a body that does not match it gets `error truncated` and
is not delivered. An inside piece in another session answers `error session_mismatch
<current-session-id>` and delivers nothing. For a session target bellhop treats that like an
ended session: `send` finds no usable route before storing anything, and the drainer fails the
session's pending messages with `session replaced in its process`. For an agent target the
message stays `pending` and the agent is re-resolved on the next poll. The drainer asks the same
question of a session hls stopped listing while its process lives on, so messages to a session Pi
replaced with `/new` fail instead of waiting for the 24 h expiry. An inside piece that cannot name
its own session answers every `deliver` with `error session_mismatch unknown`, and a `ping` naming
no session counts as not reachable. A `deliver` header without a session id or byte count gets
`error wire_version`. bellhop records the transcript file with the message when it takes the
offset, and confirms in that file. A session whose `ping` reply lacks `"wire":2`, or names another
session or none, is not reachable. `doctor` names a socket on another wire with the fix (Pi:
export the integration again; Claude: restart that Claude; a newer wire: upgrade bellhop), and a
listed one that answers but cannot name its session. Within one command or drainer tick, bellhop asks each socket once a second at
most: reachability, replacement and the transcript path come from the same pong.

The Pi extension replies `ok followUp`, `ok steer`, `busy compacting`, `error empty message`,
`error truncated`, `error session_mismatch <id>`, `error wire_version` and `error unknown header
<h>`, and drops a
connection that sends nothing for 5 s or more than 64 KB. Its `transcript` is Pi's session file
(`--session-dir` and `PI_CODING_AGENT_SESSION_DIR` included). bellhop's Pi reader uses, in order:
hls's transcript path when it names a file, the file it found before for that session (while the
file exists), the extension's report, then the per-cwd directory under `PI_CODING_AGENT_DIR`.

## Results (k/n, three repetitions)

| Harness | Candidate | S1 | S2-boundary | S2-interrupt | S3 | S3b | S7 | S10 |
|---|---|---|---|---|---|---|---|---|
| Claude | mcp | 3/3 | 3/3 boundary | 3/3 mid-turn | 3/3 | 3/3 | 3/3 | 3/3 |
| Claude | hook | 3/3 | 3/3 boundary | 3/3 mid-turn | 3/3 | 3/3 | 3/3 | 3/3 |
| Claude | modrelay | 3/3 | 3/3 boundary | 3/3 mid-turn | 3/3 | 3/3 | 3/3 | 3/3 |
| Claude | modsubmit | 3/3 | 3/3 boundary | 3/3 boundary | 3/3 | 3/3 | 3/3 | 3/3 |
| Codex | daemon-start | 3/3 | 3/3 boundary | 3/3 mid-turn | 3/3 † | not measured ‡ | 3/3 | 0/3 |
| Codex | daemon-queue | 3/3 | 3/3 boundary | – | 3/3 | not measured | 3/3 | 0/3 |
| Codex | remote-start | 3/3 | 3/3 boundary | 3/3 mid-turn | 3/3 † | not measured | 3/3 | 0/3 |
| Pi | ext | 3/3 | 3/3 boundary | 3/3 mid-turn | n/a | n/a | 3/3 | 3/3 |

- S1 latency was at most 1.2 s for every candidate, which is the resolution of the 1 s transcript poll.
- S3 and S3b: no push reached the transcript while the dialog was open, on any candidate; nothing
  was approved, chosen or written.
- † On `daemon-start` and `remote-start` the S3 boundary push never left the spike's drainer: an open
  permission prompt keeps the thread active, so the drainer kept waiting (its reply was "pending").
  Only the `turn/steer` push reached the app server there, and it left the prompt open.
- Claude `priority: "next"` arrives inside the running turn at the next tool boundary, recorded as a
  `queue-operation` `remove` with reason `absorbed_mid_turn` and an `attachment` of type
  `queued_command`. `priority: "later"` waits for the turn to end and arrives as an ordinary user
  entry.
- Codex S3b is **not measured**, not `n/a`: asked to use `request_user_input`, Codex 0.167.1 in its
  default mode opened no question dialog, and plan or collaboration mode, where Codex exposes that
  tool, was not tried. A cell that was not measured is not a pass, so no Codex candidate clears the
  S3b gate and Codex `now` was degraded to `idle` until it was measured (ADR 0002). Pi has no
  permission prompts (S3) and no question dialog (S3b), so those are `n/a`.
- ‡ S3b was measured on 2026-11-14 through bellhop's own Codex route: 3/3 safe, with
  `default_mode_request_user_input` enabled in an isolated `CODEX_HOME`, and a `now` push during an
  open approval prompt (S3) left the prompt open. Codex `now` is `turn/steer` since
  [ADR 0004](../decisions/0004-codex-now.md).
- On S2-interrupt the interrupted turn never produced its final answer on Codex (6 of 6) or Pi
  (3 of 3); Claude finished every interrupted turn (12 of 12).
- Codex S10 fails on every route; see Known limits.

## Answers to the design questions

### 1. Claude inside piece

- (a) **MCP relay.** The stdio MCP child receives `CLAUDE_CODE_MESSAGING_SOCKET` and
  `CLAUDE_CODE_MESSAGING_TOKEN` in its environment, and a frame it writes there is delivered with
  no hold. With `priority: "next"` it lands mid-turn (S2-interrupt 3/3 mid-turn); with
  `priority: "later"` it waits for the turn boundary (S2-boundary 3/3 boundary). Not measured: the
  spike gave every relay a fixed socket name, so the planned `<runtime_dir>/claude-<pid>.sock`
  naming, and whether the relay's parent is the Claude pid hls reports under the user's launch
  wrapper, were measured later ("Claude relay, as built" below).
- (b) **The same relay started elsewhere.** Spawned by a mod through `$.process.spawn`, the child
  also gets both variables (`$.process.spawn` passes the host environment). Started by a
  `SessionStart` command hook, the child gets both too, and the hook must detach it; the detached
  relay then has parent pid 1 and outlives the session unless it watches the Claude pid itself (one
  leaked in an early probe). Both deliver exactly as (a).
- (c) **Mod calling `$.prompt.submit`.** Works, and only ever at the boundary: the API documents it
  as "a turn of its own, once the session is idle", and S2-interrupt came out boundary 3/3.

### 2. Codex

- A plain `codex` TUI attaches to a running daemon (`--no-daemon` opts out). With no daemon running,
  a plain `codex` starts one in its `CODEX_HOME`.
- `turn/start` over the daemon's socket reaches the visible TUI: the push and the reply render in
  the attached TUI. `codex queue --thread <id>` does too.
- `codex queue` to a stand-alone (`--no-daemon`) TUI's thread is accepted (`Queued message …`), does
  not appear in the running TUI, and is delivered and answered when the thread is reopened with
  `codex resume`.
- What hls reports:

  | Codex session | hls `process.pid` | hls `tmux.pane` | hls `session.id` |
  |---|---|---|---|
  | stand-alone TUI | the TUI's pid | the TUI's pane | the thread id |
  | TUI attached to the shared daemon | the daemon's pid (shared by every attached TUI) | null | the thread id |
  | `--remote` TUI on a per-session server | the app server's pid | null | the thread id |

- On a shared server a thread is identified by its thread id, which is hls's `session.id`. In the one
  probe that looked, each server listed an extra loaded thread beside the TUI's
  (`thread/loaded/list`), so bellhop must take the id from hls, not from the loaded list.
- The `--remote` thread's `cwd` is the app server's working directory, not the TUI's; a per-session
  server must be started in the session's directory.
- The daemon's socket is `/private/tmp/codex-daemon-<uid>/<sha256 of $CODEX_HOME/app-server-control/app-server-control.sock>`,
  and `$CODEX_HOME/app-server-control/app-server-control.sock` is a link to it
  (`codex app-server daemon version` prints it as `socketPath`).
- A per-session server started with `codex app-server --listen unix://<path>` does the same: its
  real socket is `/private/tmp/codex-daemon-<uid>/<sha256 of the resolved <path>>`, and `<path>` is
  only a link to it. The runtime directory's 0700 check therefore does not cover where the socket
  lives, and cleanup must follow the link. Three such sockets from the measurement run were left in
  that directory and removed afterwards by matching exactly those hashes.

- **S3b, measured on 2026-11-14** with `default_mode_request_user_input` enabled in an isolated
  `CODEX_HOME`: the `request_user_input` dialog stayed open and unanswered after a mid-turn
  `turn/steer`, 3/3 ([ADR 0004](../decisions/0004-codex-now.md)).
- A Codex session is listed by hooklinesinker only after its first turn. The control socket link
  under a long `CODEX_HOME` path can exceed the 104-byte socket limit; bellhop connects through
  the link's resolved target, and only when that target is a socket owned by the user in a
  directory nobody else can write to.
- bellhop's Codex route, as built: `send` asks the app server once (`thread/loaded/list`,
  then `thread/read` and, for an active thread, `thread/turns/list`) and pushes on a second
  connection. An idle thread gets `turn/start`; a running turn gets `turn/steer` with its
  `expectedTurnId` for a `now` message and `busy` otherwise; a thread that is active with no turn
  listed yet is `busy` ("a turn is starting"); a thread waiting on an approval or a question
  (`activeFlags`) is `busy` for every push, because Codex drops a steer queued behind a dialog the
  user then escapes (ADR 0004). A refused `turn/steer` (the turn ended in between)
  is `busy`, so the message is tried again. Replies are capped at 4 MiB. Only a thread whose
  status is `idle` takes `turn/start`: `notLoaded` (the server would have to load the thread
  itself), `systemError`, and any status bellhop does not know or cannot read are `busy`. bellhop
  never answers a request the server sends it (an approval or a question meant for the TUI); it
  leaves it unanswered and the connection ends at its deadline.
- Within one second, Codex routes share one memo per app-server socket (`routes.codex.Probes`):
  the loaded list is read once per socket, a thread the daemon has not loaded is answered without
  a probe, and a failed probe answers every thread on that socket, so a daemon that accepts but
  never answers costs one 0.5 s probe per socket per second. Each loaded thread's turn is read
  once per second on a connection of its own. `sessions --json` stops checking routes after 5 s in
  total; the warning says how many of the last rows were not checked and show
  `reachable: false`.
- `bind` from a Codex session records `$CODEX_HOME/app-server-control/app-server-control.sock`
  from the binding session's own environment, so a sender with another `CODEX_HOME` still reaches
  that agent. For any other binding of an agent in `config.toml`, the agent's `env` decides
  (`CODEX_HOME`, else `~/.codex` under `HOME`), whatever the sender's environment says;
  session targets and agents only bound take it from the session's transcript path as
  hooklinesinker reports it (`<CODEX_HOME>/sessions/…`; Pi's `<PI_CODING_AGENT_DIR>/sessions/…`
  likewise), so the drainer uses the session's own home whatever it was started with; only
  without a transcript path does it fall back to its own `CODEX_HOME` (default `~/.codex`).
- hls 1.5.1 reports `process: null` for a thread on the shared daemon (not the daemon's pid as
  the spike's table says for the research harness) and stops listing the daemon's threads when the
  daemon restarts; it lists a thread again after its next turn. A Codex binding therefore fails
  ("thread no longer listed") when hls drops the thread or the app-server pid hls reports is gone,
  and comes back to `live` when hls lists the same thread again with a live (or no) pid. Measured
  on 2026-11-14 with `codex app-server daemon restart` on an isolated daemon: the TUI kept working, the
  binding failed, went `live` after one turn, and a message to the agent was confirmed.

### 3. Pi

`pi.sendUserMessage(text, { deliverAs: "followUp" })` waits for the turn boundary (3/3) and starts a
turn when idle (S1 3/3). `deliverAs: "steer"` lands mid-turn (3/3). After `/new` the extension gets
`session_shutdown` and `session_start` in the same process, and the next push lands in the new
session (S10 3/3).

### 4. Identity for `bellhop whoami`

| Harness | Variable in a command the session runs | Follows `/clear` or `/new` |
|---|---|---|
| Claude | `CLAUDE_CODE_SESSION_ID` (in Bash tool and hook processes) | yes: after `/clear` a hook saw the new id, equal to the hook input's `session_id` |
| Codex | `CODEX_THREAD_ID` (equal to hls's `session.id`) | not verified: the prompt typed after `/new` was swallowed by the welcome screen |
| Pi | `PI_SESSION_ID` (seen in Pi's tool shell; its source in the bundle was not located) | yes: after `/new` it printed the new session id |

Walking the process tree up to a pid hls reports also works for Claude, stand-alone Codex and Pi,
but not for app-server Codex sessions, whose hls pid is the server's.

### 5. Launch with an initial prompt

`claude "<prompt>"`, `codex "<prompt>"` and `pi "<prompt>"` each ran the prompt at startup in a
trusted isolated home (the smoke test launches all three this way). Startup dialogs seen:

- Claude: none, with `hasCompletedOnboarding` and the work dir's trust pre-set in
  `$CLAUDE_CONFIG_DIR/.claude.json`.
- Codex: "Hooks need review" whenever hooks are new (here hls's); `codex resume` adds a "Working
  directory · resume" choice when the current directory differs from the session's.
- Pi: none (tmux `extended-keys` warnings only).

Claude's `--mcp-config` takes a list of values and swallowed a trailing prompt as a second config
file; end the list (another flag, or `--`) before the prompt.

### Claude relay, as built

Measured on an isolated Claude launched through `sops exec-env` (the user's wrapper), with a probe
relay and then bellhop's own:

- **Socket naming.** The relay's parent is the `claude` process (chain: relay, `claude`, `sops`,
  `tmux`), and that pid is the one hls records for the session. The relay names its socket after
  its parent when the parent has `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`. It takes an ancestor
  further up (up to four levels, one `ps -A` read) only when that ancestor's entry names the
  session the relay started in (`CLAUDE_CODE_SESSION_ID`), so a Claude started from inside
  another Claude's shell never claims the outer Claude's socket. It first waits up to 10 s for its
  parent's entry, which Claude writes late, then looks at the ancestors once, then uses its parent.
- **What the relay knows.** `CLAUDE_CODE_SESSION_ID` in the relay's environment stays the id the
  session started with. The parent's registry entry follows the session: `sessionId` changed on
  `/clear` (the relay was not restarted) and stayed the same across `/compact`. The relay reads
  `sessionId` and `cwd` from that entry on every request (never its socket path, token or key
  files), reports the session and `$CLAUDE_CONFIG_DIR/projects/<cwd with every non-alphanumeric
  as ->/<session>.jsonl` in its pong, and refuses a `deliver` for another session.
- **Delivery.** Claude's messaging socket sends no reply to a frame. A boundary frame
  (`priority: "later"`) is held by Claude until the turn ends; an interrupt (`"next"`) is absorbed
  mid-turn as a `queued_command` attachment. The route queues until idle (`queues_until_idle`) and
  cannot tell busy (`busy()` is `None`), so the drainer pushes without waiting and never queues
  twice.
- **Dialogs.** With a permission prompt or a question dialog open, both a `later` and a `next`
  frame were only enqueued (`queue-operation` `enqueue`); neither dialog was answered, and after
  Escape both were delivered and answered. Claude does not drop them, so the relay sends during a
  dialog. hls reported `permission` for the open permission prompt but `idle` for the open
  question dialog.
- **Lifetime.** Claude ended the probe relay without closing its stdin, which left the socket
  behind; bellhop's relay also cleans up on SIGTERM, SIGHUP and SIGINT and when its parent pid
  changes. A relay killed with SIGKILL leaves its socket and lock; `doctor` names the dead socket.
  A second relay in the same Claude (the server configured twice, or a reconnect while the old
  one still runs) finds the lock held, says so once on stderr, and takes the socket over when the
  first one goes.
- **Relay replies and limits.** `ok later`, `ok next`, `busy claude's messaging socket: <reason>`
  (Claude's socket refused or the write did not finish within 1 s, so nothing was delivered),
  and the same `error …` replies as the Pi extension, with `error session_mismatch unknown` when
  the registry names no session. A request gets 5 s in total and 64 KB; more is dropped with no
  reply. At most 16 requests are handled at once, and a caller beyond that gets
  `busy relay is at capacity` at once. An
  `accept` error never stops the relay serving.
- **Reader path order.** The Claude reader uses hls's `transcriptPath` when it names a file, then
  the file it found before for that session (while it exists), then the path the relay reports.
- **Measured on 2026-11-14:** a child of Claude's Bash tool inherits `CLAUDE_CODE_MESSAGING_SOCKET` and
  `CLAUDE_CODE_MESSAGING_TOKEN`, and the socket is named after the owning Claude's pid; the relay
  refuses a channel not named after the Claude it chose (ADR 0005). A body of 8000 three-byte
  characters is delivered and confirmed whole. A relay at its 16-request cap answers
  `busy relay is at capacity`; a request whose answer could no longer reach the client in time is
  answered `busy` without forwarding (the relay's budget counts from `accept`, 1.5 s).
- **Not measured:** `/resume` inside a running session (does the registry follow the resumed
  session id, and does the transcript stay in the same project directory?). The relay reads the
  registry on every request, so it follows whatever Claude writes there.
- **Transcript.** A delivery is a `user` record with `origin.kind: "peer"` whose text is Claude's
  wrapper line ("Another Claude session sent a message:") followed by the envelope, or a
  `queued_command` attachment with that origin whose `prompt` is the envelope. The
  `queue-operation` `enqueue` written while a dialog is open is not a delivery.

## Known limits found

- **After a Codex TUI quits, its thread stays loaded in the daemon and listed by hls**, and a message
  to it runs as a turn with no TUI attached; a session target hls has dropped can wait for the
  24 h expiry while the daemon lives on. Measured on 2026-11-14 on an isolated daemon (Codex 0.167.1, hls 1.5.1):
  after a TUI quit, the daemon kept its thread loaded and hls kept listing it (`idle`), so a
  message to it is delivered into the daemon and runs as a turn with no TUI attached; after a
  daemon restart, `thread/loaded/list` still listed every thread (the quit one too) while hls
  listed none until a thread's next turn. Neither answer tells a quit thread from a live one, so
  bellhop changes nothing here.

- **hls 1.5.1 does not list Pi sessions launched the usual way.** Pi runs as `node` and retitles its
  process `pi`, so hls cannot identify the process, records `process: null`, and `sessions --json`
  leaves the record out. Launched through an executable literally named `pi` (a link to `node`), it
  is listed with pid and pane; the smoke helper does this.
- **Codex S10.** After `/new`, hls kept reporting the old thread (the new thread had had no turn yet;
  whether a turn would update hls was not measured), so a push resolved through hls landed in the
  old thread, which the TUI no longer shows. On the
  daemon, `/new` also opens a "Where should the new conversation run?" worktree choice.
- **The shared Codex daemon updates and restarts itself.** The isolated daemon's updater installed
  0.169.0 five to seven minutes after start (in both runs that used the daemon), asked the daemon to restart with a 60 s grace and forced it
  at the end; stopping the updater process did not prevent this (it was respawned).
- **Claude start-up under concurrency.** In the pilot, four of twelve Claude sessions launched at
  the same moment exited before becoming ready (cause not found; the pane was gone); started one at
  a time in the measurement, none did.
