from bellhop.testing.screens import Screen, classify

CLAUDE_READY = """ ▐▛███▛█   Claude Code v2.1.325
────────────────────────────────
❯ Try "fix lint errors"
────────────────────────────────
  ⏸ manual mode on · ? for shortcuts · ← for agents
"""

CODEX_READY = """  >_ OpenAI Codex (v0.167.1)
› Ask Codex to do anything
  GPT-6-Luna low · /work/probe-codex · Context 100% used
  ← for agents · ? for shortcuts
"""

PI_READY = """────────────────────────────────────────────────
/work/probe-pi (main)
0.0%/1.0M (auto)            (openrouter) deepseek/deepseek-v4.1-flash • thinking off
"""

CODEX_HOOKS = """  Hooks need review
  12 hooks are new or changed.
› 1. Review hooks
  2. Trust all and continue
  3. Continue without trusting (hooks won't run)
"""


def test_ready_screens_are_recognised() -> None:
    assert classify("claude", CLAUDE_READY) == Screen("ready")
    assert classify("codex", CODEX_READY) == Screen("ready")
    assert classify("pi", PI_READY) == Screen("ready")


def test_codex_hooks_review_is_answered_with_trust_all() -> None:
    assert classify("codex", CODEX_HOOKS) == Screen("dialog", "hooks-review", ("Down", "Enter"))


def test_claude_trust_dialog_is_answered_with_enter() -> None:
    pane = "Do you trust the files in this folder?\n❯ 1. Yes, proceed\n"
    assert classify("claude", pane) == Screen("dialog", "trust-folder", ("Enter",))


def test_login_screens_are_auth() -> None:
    assert classify("claude", "Select login method:\n") == Screen("auth", "Select login method")
    assert classify("codex", "Sign in with ChatGPT\n").state == "auth"
    assert classify("pi", "No API key found for openrouter\n").state == "auth"


def test_codex_startup_placeholder_is_still_starting() -> None:
    assert classify("codex", CODEX_READY + "Waiting for startup\n") == Screen("starting")


def test_a_blank_pane_is_starting() -> None:
    assert classify("claude", "") == Screen("starting")
