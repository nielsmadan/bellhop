# Proposed update to the Mac Studio re-test plan

**What to do:** review this diff against
`/Users/nielsmadan/wrksp/_scratch/agent-comms/docs/studio-retest.md` and apply what you agree with.
It is not applied, long unchanged table rows are shortened to `...`, and the hunk line numbers
are approximate, so apply it by hand rather than with `git apply`. It points the re-test at bellhop and callboard, which now are the delivery
layer the plan names, and drops the parts bellhop made obsolete.

What changed since the plan was written:

- The delivery layer is bellhop (routes: Claude MCP relay, Codex app server, Pi extension) and
  callboard (the inbox/forum store and doorbells), not `agent-send`.
- Launch with an initial prompt (H3) is built and smoke-tested (`bellhop launch`, `send --wake`).
- bellhop never uses Claude `SendMessage` or `crossSessionInbound`, so H4 no longer decides
  anything.
- bellhop's and callboard's own smoke suites (`just smoke`) are the per-route checks; the research
  harness's matrix stays as the baseline.

```diff
--- a/docs/studio-retest.md
+++ b/docs/studio-retest.md
@@ -1,6 +1,7 @@
 # Mac Studio re-test plan
 
-What to re-measure on the Mac Studio (M5 Max, 128 GB) before the fleet relies on the delivery layer recommended in
-`REPORT.md` (tmux, one native route per agent, `agent-send` with a name registry). Based on REPORT §10.9, adjusted
-for the fleet decisions in `~/wrksp/notes/work/factory/spec.md`:
+What to re-measure on the Mac Studio (M5 Max, 128 GB) before the fleet relies on its delivery layer: bellhop
+(`~/wrksp/bellhop`: one native route per agent, agents by name, `bellhop launch`) and callboard (`~/wrksp/callboard`:
+the durable inbox and forum, pinging through bellhop). Based on REPORT §10.9, adjusted for the fleet decisions in
+`~/wrksp/notes/work/factory/spec.md`:
@@ -19,6 +20,9 @@
 4. Run the coordinator unsandboxed (R21) from a terminal that is not one of the fleet's sessions. AC power;
    `caffeinate -i` around long batches.
 5. Prerequisites as in REPORT §10.7: python3, tmux, bun (Claude channel server), node 24 (Pi).
+6. Install bellhop and callboard from the commits their final reports name, hooklinesinker with its hooks for
+   Claude, Codex and Pi, the bellhop relay (`claude mcp add --scope user bellhop -- bellhop _relay`) and Pi extension
+   (`bellhop integration export pi --to ~/.pi/agent/extensions`), and the bellhop skill (bellhop `docs/handoff/`).
 
 ## Harness changes to build first
 
@@ -27,8 +31,8 @@
 | H1 | `--config real`: ... | ... | ... |
 | H2 | `scale --n N --mix claude,codex,pi`: parameterise `scale.MIX` (fixed at 8 today). | Target is ~30 sessions. | `scale --n 30` runs and writes the SCALE records and JSON. |
-| H3 | S13 launch-with-initial-prompt: start a fresh TUI with an initial prompt (Claude `claude "<prompt>"`, Codex `codex --remote <sock> "<prompt>"`, Pi `pi "<prompt>"`), then ring a doorbell at fixed offsets: before ready, at ready, during the initial turn, after it. | Sends during startup were never measured (REPORT §10.7, R14). | Per agent and offset: delivered / lost / duplicated, and whether the initial prompt itself ran. |
-| H4 | S14 Claude `SendMessage` across permission classes: ... | Only bypass↔bypass was measured (S9 pilot `20261027-171554-6acd`). | A table of delivered / held per pair, from the receiver's transcript. |
+| H3 | S13 through bellhop: `bellhop send <agent> --wake` to a stopped agent configured with the fleet's real wrapper, then more sends at fixed offsets (during start, during the first turn, after it). bellhop's `tests/smoke/test_wake.py` does this on isolated homes. | bellhop measured it on isolated homes only. | Per agent: the first message runs, later ones arrive in order once live, none duplicated. |
+| H4 | dropped: bellhop never uses Claude `SendMessage` or `crossSessionInbound`. | | |
 | H5 | S15 doorbell: ... | ... | ... |
 | H6 | Soak: ... | ... | ... |
@@ -38,11 +42,13 @@
 1. **Baseline on the Studio** (isolated homes, as measured here): shortlist routes only, so results compare
    directly with REPORT §3.
    ```sh
    uv run agentcomms matrix --stage shortlist --agents claude,codex,pi \
      --adapters claude-channel,claude-uds-child,codex-remote-start,pi-ext --reps 5
    ```
-   Add `opencode-server-queue` with `--agents ...,opencode` if OpenCode stays in.
+   Then bellhop's and callboard's own smoke suites, which run the routes the fleet uses:
+   `just smoke` in each checkout (isolated homes, `tmux -L bellhop-smoke` / `-L callboard-smoke`).
 2. **Real config (H1):** the same cells with `--config real`. Any verdict that differs from step 1 is a config effect;
    find the hook, MCP server or setting responsible before going on.
@@ -50,9 +56,9 @@
-4. **Cross-class `SendMessage` (H4)** and **doorbells (H5)** on real config.
-5. **Launch-with-initial-prompt (H3).** Codex: confirm `--remote` with an initial prompt creates the thread, and that
-   the registry can read the thread id afterwards (REPORT §10.7 "Codex thread id").
+4. **Doorbells (H5)** on real config, through callboard.
+5. **Wake (H3)** on real config. Codex: bellhop finds the thread by its first message on the agent's own app server;
+   confirm the same on real config.
 6. **Scale (H2):** ...
 7. **Soak (H6)** overnight.
-8. **`agent-send` itself** once built: its readiness check, registry, Codex spool and drainer (REPORT §10.8), run
-   through the same scenarios as an adapter.
+8. **bellhop's drainer under load:** 30 receivers with messages pending at once; `bellhop doctor` clean afterwards,
+   `drain.log` free of errors, every message `confirmed` or explained.
 9. Optional: S5 on `codex-queue` (lost its S5 coverage to R47), only if it is still a candidate.
@@ -64,8 +70,8 @@
-- Claude peer keys (`<config>/sessions/*.key`) and every harness socket are readable by every agent: with one user,
-  any agent can message any session, including the human's. Decide `crossSessionInbound` per session class with
-  that in mind (R26: `accept` takes frames with no token).
+- bellhop's runtime dir (`/tmp/bellhop-<uid>`, 0700) and every harness socket are reachable by every agent: with one
+  user, any agent can message any session, including the human's (bellhop messages are unauthenticated by design).
+  Leave `crossSessionInbound` unset; bellhop does not need it.
@@ -80,5 +86,4 @@
 - Any core route below 5/5 on S1/S2 or losing S7 drafts on real config.
 - Doorbells lost or duplicated during startup (H3) on the route a wake-up uses.
-- Cross-class `SendMessage` held in a direction the fleet needs (H4).
 - Rate-limit or token-refresh failures at 30 sessions with shared auth.
```
