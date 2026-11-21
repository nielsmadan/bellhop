# 0008 — Claude without a relay, when it accepts peer messages

**Status:** accepted; adds a second Claude route to [ADR 0002](0002-delivery-routes.md) and lifts
part of the Claude rule in [ADR 0001](0001-foundations.md)

**Recorded:** 2026-11-19

## Context

ADR 0001 ruled out `crossSessionInbound: accept` and writing to another session's messaging
socket, and ADR 0002 chose the MCP relay. The relay has to be installed in Claude (an MCP entry,
or `--mcp-config` on every start) before a session can be reached. For someone installing bellhop
that is a second install step in a second tool; a single Claude setting is easier to hand out.

Claude Code's `crossSessionInbound` user setting takes `accept`, `hold` and `refuse`. With
`accept`, Claude delivers every frame written to a session's messaging socket, from any process
running as the same user. Unset, a frame from a process that is not a Claude session was held for
the user's approval in every measured case.

## Decision

bellhop has a second Claude route, `claude-socket`, used only when both hold:

1. the session's relay does not answer (the relay stays the first choice), and
2. `settings.json` in that session's Claude config directory has `"crossSessionInbound":
   "accept"`.

Every Claude session gets one route object that holds both. It chooses once, the first time it
is asked, so finding a route reads nothing and the name it reports is the route its push takes;
with no relay answering and `accept` set it is the socket route even while that is unreachable.
Its `replaced` is true when either route says so, and its `obstacle` is the socket route's reason
when the settings say `accept`, else the relay's followed by the settings file that does not
accept.

The socket route reads `sessions/<pid>.json` in the config directory for the session the pid runs
now and its `messagingSocketPath`, and writes one frame there: an `auth` line with the session's
`peerToken` when its key file is readable, then the same `user` frame the relay writes
(`priority: "later"` at the boundary, `"next"` to interrupt). It checks all of this again on every
push, the setting included. It writes only to an absolute path named `<pid>.sock` whose registry
entry names the addressed session, so a push for a conversation that `/clear` replaced is not
delivered into the new one.

It trusts these files only where no one else could have put them: the config directory and its
`sessions` directory must belong to the user and not be group- or world-writable, and the socket
must be a socket the user owns in such a directory, the check the Codex route applies to an
app-server socket.

The config directory is a configured agent's `CLAUDE_CONFIG_DIR` (else `.claude` in its home);
for any other session the one its transcript lies in, else the caller's `$CLAUDE_CONFIG_DIR`, else
`~/.claude`. bellhop reads the setting and never writes it.

What ADR 0001 still rules out: bellhop asserts no sender session and no permission mode, never
sets `accept` itself, and does not use this route for a session whose settings do not say
`accept`.

## Measured (2026-11-21, Claude Code 2.1.330)

An isolated session with `accept` in its settings and no relay
(`tests/smoke/test_claude.py`), reported as `claude-socket` before and after: a push to an idle
session, one held to the turn boundary, one steered into a running turn, a `hold` read with
`bellhop inbox`, a draft left in the composer, a permission prompt and a question dialog left open
and unanswered, an 8,000-character body, and a `/clear` followed, every one confirmed from the
transcript. Claude's socket directory and the session's config directory passed the ownership
checks.

## Not measured

- A frame with no `auth` line. The smoke test asserts that its session has a key file, so it
  measures the frame with one; the branch that sends only the `user` frame is covered by unit
  tests against a fake socket.
- A wrong `peerToken`, and more than one key file for a pid (bellhop takes the first by name).
- Whether Claude keeps one socket across `/clear`. The frame names no session, so a `/clear`
  between the registry check and the write could deliver into the new conversation; the relay has
  the same window.

## Consequences

- `accept` is wider than bellhop. It also delivers native `SendMessage` from Claude sessions in
  another permission mode, and anything else a same-user process writes to the socket, without the
  review Claude does by default. The user docs say so next to the setting.
- bellhop decides from the user settings file alone. A session given `accept` only by `--settings`
  or a managed policy is not recognised, and a session whose effective value is stricter than the
  file (a policy, a repo's settings) is pushed to and may hold or refuse the message; it then
  stays `sent` and never reaches `confirmed`.
- The route relies on three more undocumented things in Claude Code: the registry's
  `messagingSocketPath`, the key file `sessions/<pid>.<hash>.key` with `peerToken`, and that an
  accepted peer frame is recorded with `origin.kind: "peer"`, which the transcript reader already
  recognises. `doctor` has a `claude-socket sessions` check: the messaging socket of each session that
  accepts peer messages.
- An unusable runtime directory no longer fails a `send` to a session this route reaches, since
  the route does not use that directory.
- The socket route cannot name a transcript. A session hooklinesinker lists without a transcript
  path is confirmed only through the relay.
- `bellhop whoami` and the child-token path are unchanged; only the relay knows the session it
  runs in.
