# Known limits

What bellhop does not do, or does only partly, and what you see when you hit it.

## Messages

- **Messages are unauthenticated.** Everything runs as one user and sender names are
  self-asserted: any local process of yours can send as anyone.
- **A Claude receiver sees every message under Claude's own framing.** Claude shows a relayed
  message as "Another Claude session sent a message" with sender `unknown`. The real sender is in
  bellhop's envelope, `[bellhop <id> from <sender>]`, at the start of the text.
- **Agents need `bellhop inbox` in their allowed commands.** A pushed message that has hold
  messages behind it ends with "run `bellhop inbox`"; an agent whose allow-list lacks it asks for
  permission or cannot read them.
- **A wake's own message arrives first.** With `send --wake`, the message is the started agent's
  first prompt; messages to that agent that were already pending follow once the session is
  live, in their own order.
- **Held messages wait in `bellhop inbox` until the agent runs it.** This includes callboard's
  mail doorbells: `callboard read` does not clear them, so an agent that reads its mail through
  callboard still sees them in `bellhop inbox`, and every pushed message counts them in its
  "N more waiting" line.
- **A name stays bound until its session ends or another session binds it.** There is no unbind.
  One session can hold several names; `whoami` then reports one of them, and `bellhop inbox` takes
  the held messages for all of them.
- **Messages queued behind a failed start wait.** They stay `pending` until the agent is launched
  again or a session binds the name; messages to an agent do not expire.
- **A session-target message can wait 24 hours, then fails as `expired`.** This hits a Codex
  thread that hooklinesinker stops listing (for example after a daemon restart, until its next
  turn): a message to its session id stays `pending` until then.

## The drainer

- **A drainer that is stopped or replaced forgets which sessions it saw working.** A `sent` message
  older than 10 minutes to a session that is still working is then no longer watched by the
  drainer; `bellhop status <id>` still checks the transcript each time it is asked.

- **The drainer keeps the environment and the sandbox of whichever session first needed it, until
  it exits.** Any bellhop command that finds work can start it: a `send` from a sandboxed agent,
  a `status`, a `doctor`. It then delivers and confirms for every harness with that session's
  `PATH`, variables and sandbox. It finds `tmux` on its own `PATH`, else at
  `/opt/homebrew/bin/tmux`, `/usr/local/bin/tmux` or `/usr/bin/tmux`; it never runs a program named
  by the store or by a file in the state or runtime directory, and it finishes a start only on
  the tmux server `config.toml` names. A drainer that cannot run
  hooklinesinker shows as a failed `drainer` check in `bellhop doctor`, with its pid: stop it, and
  the next bellhop command starts a fresh one in that command's environment.

- **A hard link planted beside the store in the moment before SQLite opens it is not caught.**
  bellhop removes a planted `-wal` or `-shm` and creates each as a private file before SQLite opens
  the store, and SQLite refuses a symlinked one; a hard link made in between is written by SQLite.

## Claude Code

- **`/resume` inside a running Claude is not measured.** The binding of an agent may keep the old
  session id; `bellhop bind` from the resumed session fixes it.
- **An `idle` message can arrive while a question dialog is open.** hooklinesinker reports `idle`
  while Claude shows a question dialog; Claude holds the message and delivers it after the dialog
  is answered or dismissed.
- **A Claude session without the relay has no route.** `send` answers `no_route`, or stores a
  `hold` with `--or-hold`. A running Claude keeps the relay it started with until it is restarted.
- **The relay can take up to 10 s to start listening** when it is started through a wrapper that
  does not `exec` Claude.

## Codex

- **`codex --no-daemon` sessions have no route.** Only a Codex attached to an app server (the
  shared daemon, or the per-session server `bellhop launch` starts) can be reached.
- **A Codex thread whose TUI has quit stays loaded in the app server.** A message to it starts a
  turn that nobody is watching.
- **hooklinesinker lists a Codex thread only after its first turn**, so a Codex agent can be
  launched only with a first message (`launch --prompt`).
- **Each launched Codex agent keeps a small `bellhop _codex_session` process** (about 27 MB)
  that stops its app server when the TUI exits.

## Pi

- **hooklinesinker lists a Pi session only when Pi runs as an executable named `pi`.**
- **A Pi killed in the middle of a turn can leave its socket** in the runtime dir.
  `bellhop doctor` names it and `bellhop doctor --prune` removes it.

## Launching agents

- **The first message and the agent's `env` values are process arguments.** The prompt is
  visible in the process list for the life of the agent; `env` values are kept as tmux's pane
  start command. Keep secrets in the agent's `command` wrapper.
- **`SOPS_SECRETS` and `SOPS_AGE_KEY_FILE` reach every launched agent** (they are on the
  environment allowlist).
- **A tmux server inherits the sandbox of whatever starts it.** bellhop runs its tmux client with
  the umask of the command that called bellhop, so a server it starts gets your umask. If bellhop's
  `new-session` is the first call on a tmux socket and runs inside a sandboxed session, that
  server and every agent on it run inside that sandbox, and an agent `command` wrapper that
  applies its own sandbox may fail to start nested inside it. Start the fleet's tmux server
  yourself, outside any sandbox.
- **bellhop never answers a startup screen.** A trust, login, hook-review or similar screen fails
  the start with the command to attach; answer it once in the agent's home and launch again.
- **Restart a Codex or Pi agent after changing its homes.** bellhop reads an agent's `CODEX_HOME`
  and `PI_CODING_AGENT_DIR` from `config.toml` each time; a running agent keeps writing to its old
  home, so after an edit its messages stop being confirmed until it restarts. (Claude
  transcripts are found through hooklinesinker and the relay, not `CLAUDE_CONFIG_DIR`.)
- **A failed start is recognised by its `detail`**, which starts with `start failed:`; `send
  --wake` refuses such an agent until `bellhop launch` retries it.
