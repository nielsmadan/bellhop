# Agents: configuring, launching and waking

An agent is a name for one long-lived coding-agent session. A session gets the name with
`bellhop bind <agent>`, or bellhop starts one with `bellhop launch <agent>` or
`bellhop send <agent> TEXT --wake`. Agents live in `$XDG_CONFIG_HOME/bellhop/config.toml`. A name
uses lowercase letters, digits, `_` and `-`, starts with a letter or digit, and is at most 64
characters.

## Config

```toml
tmux_socket = "fleet"            # optional; "" (the default) is the default tmux server,
                                 # a name is `tmux -L <name>`, an absolute path `tmux -S <path>`;
                                 # bellhop never follows the caller's $TMUX
runtime_dir = "/tmp/bellhop-501" # optional; where endpoint sockets live

[agents.enki]
harness = "claude"
cwd = "~/fleet/work/slimemold"
command = ["/opt/ac/bin/fleet-claude"]
env = { CLAUDE_CONFIG_DIR = "~/fleet/harness/claude" }

[agents.nanna]
harness = "codex"
cwd = "~/fleet/work/ziggurat"
command = ["/opt/ac/bin/fleet-codex"]
server_command = ["/opt/ac/bin/fleet-codex", "app-server"]
env = { CODEX_HOME = "~/fleet/harness/codex" }

[agents.utu]
harness = "pi"
cwd = "~/fleet/work/tablets"
command = ["/opt/ac/bin/fleet-pi"]
env = { PI_CODING_AGENT_DIR = "~/fleet/harness/pi" }
```

| Key | Meaning |
|---|---|
| `harness` | `claude`, `codex` or `pi`; required |
| `cwd` | the directory the agent starts in; `~` is expanded; default `$HOME` |
| `command` | the argv that starts the agent, never a shell string; usually a wrapper script. Its first element is a program, not `NAME=value`. An agent without `command` can be bound but not started |
| `env` | variables added to the agent's environment; `~` is expanded. The agent's homes (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `PI_CODING_AGENT_DIR`) belong here: bellhop finds the agent's sockets, rollouts and session files from them, whatever the sender's own environment says |
| `server_command` | Codex only: the argv that starts its app server; default `codex app-server`. Like `command`, a wrapper here should `exec` the server; bellhop runs it in a process group of its own and stops that whole group when the TUI exits, so a wrapper that does not `exec` is stopped with its server |

## What bellhop runs

bellhop starts the agent in a new detached tmux session on the configured server, named after
the agent (`enki`, or `enki-2` … `enki-9` when that name is taken), in `cwd`. It never runs a
shell: tmux gets the argv `/usr/bin/env -i NAME=value … <command> <appended arguments>`, so the
agent's environment is exactly:

- `HOME`, `USER`, `LOGNAME`, `PATH`, `SHELL`, `LANG`, `LC_*`, `TMPDIR`, `SOPS_SECRETS` and
  `SOPS_AGE_KEY_FILE` from the caller;
- bellhop's own locations from the caller (`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME`,
  `XDG_CACHE_HOME`, `BELLHOP_RUNTIME_DIR`, `BELLHOP_HOOKLINESINKER`), so the agent's bellhop and
  hooks use the same store;
- `TERM=tmux-256color`;
- the agent's `env`.

Nothing else reaches it: not the tmux server's environment, not the caller's `CLAUDE*`, `CODEX*`
or `PI_*` variables, messaging sockets or tokens, and no API key. Credentials come from the
wrapper (for example through `sops exec-env`), never from `env`: the `NAME=value` pairs are
arguments of `/usr/bin/env`, and tmux keeps them as the pane's start command for as long as the
pane exists.

The first message (the `--prompt` of `launch`, or the message `send --wake` was given) is passed
as the harness's prompt argument: its envelope, `[bellhop <id> from <sender>] <text>`, and the
`run bellhop inbox` line when hold messages are waiting. Like any argument it is visible in the
process list for the life of the agent, so it is not the place for secrets.

## What the `command` wrapper must do

For every harness, the wrapper adds its own flags first, then passes bellhop's arguments on
unchanged as the last ones (`exec real-binary --own-flags "$@"`), and `exec`s the harness so the
harness is the process hooklinesinker sees. It sets the model, permission mode and credentials;
bellhop adds none of those. It must not start the harness with permission checks off. The
agent's home must be past its first-start screens (see Failures).

| Harness | bellhop appends | The wrapper also |
|---|---|---|
| Claude | `--session-id <uuid> --mcp-config <bellhop-mcp.json> -- <prompt>` (no `--` and prompt without one) | keeps its own flags before `"$@"`, since `--` ends Claude's options; `bellhop` must be on `PATH`, because the MCP config starts `bellhop _relay` |
| Codex | bellhop runs `python -I -m bellhop _codex_session`, which starts `server_command --listen unix://<socket>` and then `command --remote unix://<socket> <prompt>`, both with the agent's environment | `server_command` must start `codex app-server` with the arguments it is given; `CODEX_HOME` belongs in `env`, since both processes need it |
| Pi | `--session-id <uuid> <prompt>` | `exec`s an executable named `pi` (hooklinesinker lists a Pi session only then); bellhop's Pi extension is installed in the agent's `PI_CODING_AGENT_DIR` |

`_codex_session` stops the app server when the Codex TUI exits (or when its tmux pane is closed)
and removes the socket; the app server's output goes to `$XDG_STATE_HOME/bellhop/codex-<agent>.log`,
started afresh with each launch. A Codex agent needs a first message (`launch --prompt`): hooklinesinker
lists a Codex thread only after its first turn, and bellhop identifies the new thread on its app
server by that message.

## Launching and waking

`bellhop launch <agent> [--prompt TEXT] [--from NAME] [--key KEY] [--cause TEXT] [--no-wait]`
and `bellhop send <agent> TEXT --wake`, under one lock per agent:

- The agent's binding is `live`: nothing is started. `launch --prompt` sends the prompt as an
  `idle` message; `send --wake` is a plain send.
- The binding is `starting`: the message is stored `pending` behind the start and delivered, in
  order, once the agent is live. Messages sent close together make one start. `launch` starts
  the agent again when a binding has been `starting` for more than two minutes; the old start's
  first message is then settled like an abandoned one (below) and the new start has its own.
- No binding, a binding whose process is gone, or a failed start that `launch` retries: bellhop
  writes a `starting` binding (with `cause`: `--cause`, `launch`, or `send --wake from <sender>`)
  and the first message (route `launch`, state `pending`) in one step, then opens the tmux
  session.
- A failed start: `send --wake` answers `launch_failed` at once with the failure's detail and
  stores its message `failed`; `launch` clears it and starts again.
- A `--wake` that cannot start the agent stores the message `pending` with a warning naming the
  cause, and leaves the binding alone, so a later wake tries again: no `command`, a `cwd` that is
  not a directory, no tmux (on `PATH`, else at `/opt/homebrew/bin/tmux`, `/usr/local/bin/tmux` or
  `/usr/bin/tmux`), an unusable runtime dir (Codex), an environment variable
  name the child cannot be given, or tmux failing before it opened a session (no answer, no free
  session name). `launch` of such an agent fails with that cause's error code (`invalid_config`
  for the configuration causes and no tmux, `launch_failed` when tmux fails before it
  opens a session, `runtime_dir_unsafe` for the runtime
  dir) and stores nothing. A `start failed:` binding, and a stored `failed` or `outcome unknown`
  first message, happen only once tmux was asked to open the session.
- A `hold` message never starts an agent, and `--wake` does nothing for a session target.
  `--confirm` does not wait for a message that starts its agent (a warning says so).

The wake's own message is the start prompt, so it arrives first; messages to the agent that were
already pending follow, in their own order, once the session is live. Messages queued behind a
start that then fails stay `pending` until the agent is launched again or a session binds the
name; messages to an agent do not expire.

`bellhop bind <agent>` takes the same lock and refuses (`binding_conflict`) while the agent is
`starting`: wait for it to go live, or `launch` it again.

The tmux server a start used is recorded with its pane as tmux reports its socket's absolute path
(`tmux_target`, e.g. `-S /private/tmp/tmux-501/fleet enki:%0`), and the readiness check and the
attach hint always use that server, whatever `tmux_socket` or `TMUX_TMPDIR` say later. The tmux
client itself gets only `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_*`, `TMPDIR`,
`TERM` and `TMUX_TMPDIR`, so a server it starts holds none of the caller's session, key or
bellhop variables. With an empty `tmux_socket` and no tmux server running, bellhop's
`new-session` starts your default server with that small environment. The drainer re-reads `config.toml` whenever the file changes. An agent's homes come from the
config as it is now, so restart a running agent after changing its `env` homes.

`send --wake` and `launch --no-wait` return as soon as the tmux session is open. `launch` waits up
to 95 s for the drainer to finish the start. The drainer checks each `starting` binding every
second; it is ready when hooklinesinker lists its session (Claude and Pi: the pre-assigned session
id; Codex: the thread on its own app server whose first message is the prompt) and its route
answers (the relay, the extension or the app server). The binding then goes `live` with the
session's process, the first message becomes `sent` (detail `started with this prompt`), and the
queue drains. The drainer then confirms the first message like any other, from the new
session's transcript, so it reaches `confirmed`.

## Failures

A start fails when:

- the pane shows a login screen, or a first-start screen bellhop never answers: Claude's folder
  trust, theme or development-channels screen; Codex's hook review, directory trust, model
  notice or resume-directory choice. The binding's `detail` says which and how to attach, for
  example `start failed: stopped at the trust-folder screen; attach with: tmux -S
  /private/tmp/tmux-501/fleet attach -t =enki`;
- it is not ready after 90 s (a permission prompt left open counts as not ready);
- its tmux pane closes first, or tmux refuses the session (or does not answer for 90 s);
- hooklinesinker stays unavailable past the 90 s.

A `new-session` that gives no answer within 5 s may still have started: bellhop looks the session
up by name on that server and carries on with its pane if it is there; if not, the start fails
with the attach hint and its first message is `sent` with `outcome unknown`.

A screen only fails a start while hooklinesinker does not yet list the session: once it does, the
text in the pane is the agent's own conversation, which can quote any of these phrases.

Its first message is `failed` when tmux refused the session. Once the prompt was passed to tmux,
the agent may already hold it and act on it after the screen is answered, so the message is `sent`
with detail `outcome unknown; start failed: …` rather than `failed`. The same rule settles the
first message of a start that was abandoned (its binding replaced, or a stale start launched
again): `sent` with `outcome unknown; start abandoned: …` once it reached tmux, else `failed`.

The tmux session is left as it is for inspection; bellhop never stops or restarts a session it
started. Answer the screen once in the agent's home (or prepare the home so it does not appear),
then `bellhop launch <agent>` again.

Known limits: `/resume` inside a running Claude is not followed by its binding, and a message to a
session target (rather than an agent) that waits more than 24 hours fails as `expired`.
