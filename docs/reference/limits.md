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
- **Messages queued behind a failed start wait.** They stay `pending` until the agent is launched
  again or a session binds the name; messages to an agent do not expire.
- **A session-target message can wait 24 hours, then fails as `expired`.** This hits a Codex
  thread that hooklinesinker stops listing (for example after a daemon restart, until its next
  turn): a message to its session id stays `pending` until then.

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
- **bellhop never answers a startup screen.** A trust, login, hook-review or similar screen fails
  the start with the command to attach; answer it once in the agent's home and launch again.
- **Restart a Codex or Pi agent after changing its homes.** bellhop reads an agent's `CODEX_HOME`
  and `PI_CODING_AGENT_DIR` from `config.toml` each time; a running agent keeps writing to its old
  home, so after an edit its messages stop being confirmed until it restarts. (Claude
  transcripts are found through hooklinesinker and the relay, not `CLAUDE_CONFIG_DIR`.)
- **A failed start is recognised by its `detail`**, which starts with `start failed:`; `send
  --wake` refuses such an agent until `bellhop launch` retries it.
