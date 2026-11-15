# 0003 — One hooklinesinker budget

**Status:** accepted; supersedes the hls deadline in [ADR 0001](0001-foundations.md)

**Recorded:** 2026-11-14

## Context

ADR 0001 gave an hls call "5 s with one retry". Read as 5 s per attempt, an hls call alone could
spend 10 s, all of `bellhop send`'s budget. The first fix split one 5 s budget evenly, which
made an hls that answers in 2.5–5 s fail both attempts where it used to succeed.

## Decision

An hls call gets 5 s in total. The first attempt may use up to 4 s; the retry gets whatever is left.
The retry exists for a single invocation that hung; a merely slow hls will be slow again, so the
first attempt gets most of the time. The `bellhop send` budget stays 10 s: hls 5 s, `ps` 1.5 s, one
route probe 0.5 s (a Pi ping, or one Codex app-server exchange that answers both "is the thread
loaded" and "is a turn running"), the push 2 s, which leaves 1 s for start-up, SQLite and the
transcript lookup. The Pi transcript lookup reuses the probe's pong, so it adds no ping.

## Consequences

- An hls slower than 4 s fails the call after 5 s (exit 5); the drainer backs off and tries again.
- An hls that exits non-zero or cannot run is reported at once; only a timeout is retried.
