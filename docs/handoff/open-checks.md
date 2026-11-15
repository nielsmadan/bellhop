# Checks this run could not do

**What to do:** run each check when its precondition holds, from a bellhop checkout, and keep the
output. Each says why it was not run.

## 1. The smoke suite inside nono

Not run: it needs the grants in [nono-grants.md](nono-grants.md), which only you can add.

After adding them, from a sandboxed session of each profile (`claude-local`, then `codex-local`,
then `pi-local`):

```sh
just smoke
bellhop doctor
```

Expect every smoke cell to pass and `doctor` to report every check `ok`. A failure that names a
path: `nono why --self --path <path> --op <op>` tells you which grant is missing.

## 2. The smoke suite against a session started with permission checks off

Not run: this run never starts an agent with permission checks off. bellhop's routes do not depend
on the permission mode, but the dialog cells (S3, S3b) behave differently when no dialog ever
opens.

**This check is your call.** It runs real Claude, Codex and Pi agents with permission checks off
and with your real credentials (the smoke helper launches them through `sops-exec` with
`AGENT_REQUIRE_SECRETS=1`), with no approval step, in `.cache/smoke/<run>/work` directories only.

In a scratch clone of bellhop, change the permission arguments in
`src/bellhop/testing/agents.py` (`launch_argv`): **replace** Claude's `--permission-mode default`
and Codex's `-a on-request -s read-only` with your unattended mode's flags, rather than adding
flags after them. Then, in that clone:

```sh
just smoke
```

Expect the delivery cells to pass; the permission-dialog cells will report that no dialog opened.

## 3. Where `/tmp/bellhop-501` came from

Found: two unit tests in `tests/test_cli.py` (`sessions` checking each endpoint session's route)
resolved the default runtime dir before the test fixture gave every test a private one; it was
created on 2026-11-14 at 10:01:47 local time. It is empty and mode 0700. Every test now gets its
own runtime dir and fails if it reaches `/tmp/bellhop-<uid>`.
Remove it yourself if no bellhop session of yours uses it:

```sh
ls -la /tmp/bellhop-501          # empty means unused
rmdir /tmp/bellhop-501
```
