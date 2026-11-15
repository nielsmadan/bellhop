# 0004 — Codex `now` through `turn/steer`

**Status:** accepted; supersedes the Codex `now` route in [ADR 0002](0002-delivery-routes.md)

**Recorded:** 2026-11-14

## Context

ADR 0002 degraded Codex `now` to `idle` because S3b (a question dialog open while the push
arrives) could not be measured: in its default mode Codex opened no dialog. Codex 0.167.1 has an
under-development feature, `default_mode_request_user_input`, that lets `request_user_input` open
its question dialog in the default mode. Task B5 enabled it in an isolated smoke `CODEX_HOME` only.

## Measurement (Task B5, 2026-11-14)

Codex 0.167.1 attached to an isolated daemon, model and effort as in the smoke helper, three
repetitions. Each repetition asked Codex to call `request_user_input` with the options Red and
Blue, waited for the dialog (`Question 1/1 (1 unanswered)`, `1. Red`, `2. Blue`), then sent
`turn/steer` for the running turn over the daemon's socket and waited 15 s.

| Rep | `turn/steer` | Dialog still open after 15 s | Dialog answered |
|---|---|---|---|
| 1 | accepted | yes | no |
| 2 | accepted | yes | no |
| 3 | accepted | yes | no |

S3 (a command-approval prompt open) passed in ADR 0002's spike and again through bellhop in the
B5 smoke: the prompt stayed open, the command did not run, and the message waited.

## Decision

Codex `now` uses `turn/steer` on the same app server as `idle`: a `now` push to a thread with a
running turn steers into it; a turn still starting is `busy`; an idle thread gets `turn/start`. The
Codex route reports `can_interrupt`, so a Codex `now` message is no longer `degraded_from: "now"`.

A thread whose status carries `waitingOnApproval` or `waitingOnUserInput` (an approval prompt or a
question dialog is open) is `busy` for every push, `now` included. The steer itself was safe, but
the B5 smoke run `20261114-120726-d991` showed Codex drops a queued steer when the user then
interrupts the turn: in the `s3-now` cell and in both S3b repetitions that opened a dialog, the
steer was accepted, the dialog was escaped (`turn_aborted`), and the steered text never reached the
rollout, so the message stayed `sent` and unconfirmed. Waiting until the dialog closes delivers it
either way: by `turn/steer` if the turn goes on, by `turn/start` once the thread is idle.

## Consequences

- ADR 0002's note holds: a Codex `now` push can displace the running turn's own final answer.
- The S3b result depends on `request_user_input` keeping its dialog open while a steer is queued;
  a later Codex that changes that needs the measurement again (`tests/smoke/test_codex.py`, cell
  `s3b`).
