# 0005 — What the Claude relay relies on

**Status:** accepted

**Recorded:** 2026-11-14

## Context

ADR 0002 chose an MCP stdio relay as Claude's inside piece. Task B6 built it as `bellhop _relay`
and measured what it can know (`docs/reference/routes.md`, "Claude relay, as built in Task B6").
Wire version 2 asks the inside piece to name the session it is in, so a push addressed to a
session Claude has replaced with `/clear` is refused instead of landing in the new conversation.

## Decision

The relay relies on five things Claude Code does today, none of them a documented interface:

1. Claude gives each MCP stdio child `CLAUDE_CODE_MESSAGING_SOCKET` and
   `CLAUDE_CODE_MESSAGING_TOKEN`, and a frame `{"type":"auth","token":…}` followed by
   `{"type":"user","message":{"role":"user","content":…},"priority":"later"|"next",…}` written
   there is delivered (`later` at the turn boundary, `next` mid-turn), with no reply.
2. `$CLAUDE_CONFIG_DIR/sessions/<pid>.json` exists for the Claude process and holds `pid`,
   `sessionId` (following `/clear`) and `cwd`. The relay reads only those three fields, from its
   own Claude's entry; never another session's socket path, token or key file.
3. `CLAUDE_CODE_SESSION_ID` in the relay's environment is the session the relay started in. It is
   used only to recognise its own Claude among its ancestors.
4. A session's transcript is `$CLAUDE_CONFIG_DIR/projects/<cwd with every character outside
   A–Z, a–z, 0–9 replaced by ->/<sessionId>.jsonl` (used only when hls reports no transcript path).
5. A delivered frame is recorded as a `user` record or a `queued_command` attachment with
   `origin.kind: "peer"`, its text after the line "Another Claude session sent a message:".
6. The messaging socket is named `<pid>.sock` after the Claude process that owns it. The
   variables are inherited by a Claude's Bash-tool children (measured in Task B-6f in an isolated
   smoke Claude: both set, the socket named after an ancestor pid; values never printed), so a
   Claude started from another Claude's shell could pass the outer channel down. The relay
   therefore listens only when its socket's file name is `<chosen Claude's pid>.sock`.
7. Frame size: a body of bellhop's maximum length (8000 characters, all three-byte, about 24 KB
   in UTF-8 and twice that JSON-escaped) arrived whole and was confirmed (Task B-6f smoke cell
   `max-body`), so frames stay JSON-escaped.

## Consequences

- A Claude release that changes any of these breaks the Claude route quietly: the relay pongs
  `"session": null` and every Claude session is unreachable (`no_route`). `doctor` names a
  listed relay that cannot name its session; `tests/smoke/test_claude.py` is the check to rerun
  after a Claude upgrade.
- Long or non-ASCII working directories may be named differently by Claude (item 4); the reader
  then cannot confirm from the relay's path, and messages stay `sent` rather than being lost.
