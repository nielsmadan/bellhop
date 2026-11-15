# Proposals for hooklinesinker

**What to do:** take each section to hooklinesinker as an issue or change. Each lists what bellhop
saw (hls 1.5.1 and Codex 0.167.1, measured with bellhop on 2026-11-13 and 2026-11-14) and the smallest change
that would fix it. None is applied; bellhop works around each as described in
[Known limits](../reference/limits.md).

## 1. Codex sessions have no title

**Evidence.** hls maps a title only for Claude, Kimi and Pi (`session_title` in the hook
payload). Its Codex mapping sets model, permission mode and the interrupt problem, and no title
(`src/normalize.rs`, the `Agent::Codex => Details { … }` arm). `bellhop sessions` therefore shows
an empty title for every Codex session, and a Codex session cannot be addressed by title.

**Smallest change.** When a Codex `UserPromptSubmit` arrives for a session that has no title yet,
set the title to the prompt's first line, trimmed to a short length (Codex's own app server calls
this the thread's `preview`). Later prompts keep it.

## 2. Pi sessions not started as an executable named `pi` are dropped

**Evidence.** Pi runs as `node` and retitles its process `pi`; hls cannot identify the process,
records `process: null`, and `sessions --json` leaves the record out. Started through an
executable literally named `pi` (a link to `node`), it is listed with its pid and pane
(bellhop's `docs/reference/routes.md`, "hls 1.5.1 does not list Pi sessions launched the usual
way"; ADR 0002).

**Smallest change.** Let the Pi integration report its own pid (`process.pid` is known inside the
extension) in the hook payload, and have hls use a reported pid when its process-name match finds
none.

## 3. Codex `/new` is not followed

**Evidence.** After `/new` in a Codex TUI, hls kept reporting the old thread as the session (the
new thread had no turn yet), so a message resolved through hls landed in the old thread, which the
TUI no longer shows (routes.md, "Codex S10"; ADR 0002). Whether the new thread's first turn
updates hls was not measured.

**Smallest change.** Extend the rule hls applies after Claude's `/clear` (`superseded_session` in
`src/state.rs`) to a Codex `SessionStart` for a new thread id from the same TUI process, so the
new thread replaces the old session.

## 4. Shared-daemon threads: `process: null`, and dropped after a daemon restart

**Evidence.** For a thread on the shared Codex daemon hls 1.5.1 reports `process: null`; after
`codex app-server daemon restart` it stops listing the daemon's threads, and lists a thread again
only after its next turn (routes.md, "hls 1.5.1 reports `process: null` for a thread on the
shared daemon"; measured on 2026-11-14 on an isolated daemon). bellhop's binding for such an agent
fails ("thread no longer listed") until the next turn, and a message to the session id waits.

**Smallest change.** Record the daemon's pid as the thread's process (the TUI's hook knows which
app server it is attached to), and keep listing a thread across a daemon restart while its TUI
process is alive.

## 5. A Codex thread is listed only after its first turn

**Evidence.** A Codex session appears in `sessions --json` only once its first turn has started
(routes.md, "A Codex session is listed by hooklinesinker only after its first turn"; on 2026-11-14
a launched Codex was listed while its first turn was still running). An idle new Codex session
cannot be reached until someone types into it, and `bellhop launch` needs a first prompt for a
Codex agent.

**Smallest change.** Record the session at Codex's `SessionStart` hook, as hls does for Claude
and Pi.

## 6. A Claude question dialog is reported as `idle`

**Evidence.** With an `AskUserQuestion` dialog open, hls reported `idle`; with a permission prompt
open it reported `permission` (routes.md, Claude relay "Dialogs"). An `idle` message can therefore
be pushed while the user is answering the dialog; Claude holds it and delivers it after the dialog
closes.

**Smallest change.** Map the hook event Claude emits when it asks the user a question (the
`PreToolUse` of `AskUserQuestion`) to a `question` phase (or to `permission`), cleared by the next
`PostToolUse` or `Stop`.
