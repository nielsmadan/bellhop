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

In a scratch clone of bellhop, edit `tests/smoke/test_claude.py` (and `test_codex.py`,
`test_pi.py`): add your unattended-mode flags to the `extra` list of each `run.launch(...)`
call, before any `--` already in that list, so they reach the harness ahead of the prompt.
Then, in that clone:

```sh
just smoke
```

Expect the delivery cells to pass; the permission-dialog cells will report that no dialog opened.

## 3. Where `/tmp/bellhop-501` came from

Found: two unit tests in `tests/test_cli.py` (`sessions` checking each endpoint session's route)
resolved the default runtime dir before the test fixture gave every test a private one; it was
created on 2026-11-14 at 10:01:47 local time during Task B-4. It is empty and mode 0700. Since
commit `e1f48a1` every test gets its own runtime dir and fails if it reaches `/tmp/bellhop-<uid>`.
Remove it yourself if no bellhop session of yours uses it:

```sh
ls -la /tmp/bellhop-501          # empty means unused
rmdir /tmp/bellhop-501
```
