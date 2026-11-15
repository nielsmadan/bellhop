import re

import pytest

from bellhop import readiness
from bellhop.models import Harness
from bellhop.readiness import Screen, classify
from bellhop.testing import screens
from test_testing_screens import CLAUDE_READY, CODEX_HOOKS, CODEX_READY, PI_READY

SAMPLES: list[tuple[Harness, str]] = [
    ("claude", CLAUDE_READY),
    ("codex", CODEX_READY),
    ("pi", PI_READY),
    ("codex", CODEX_HOOKS),
    ("claude", "Do you trust the files in this folder?\n❯ 1. Yes, proceed\n"),
    ("claude", "Choose the text style that looks best\n"),
    ("codex", "Do you trust the contents of this directory?\n"),
    ("codex", "Working directory · resume\n  1. Use session directory\n"),
    ("claude", "Select login method:\n"),
    ("codex", "Sign in with ChatGPT\n"),
    ("pi", "No API key found for openrouter\n"),
    ("codex", CODEX_READY + "Waiting for startup\n"),
    ("claude", ""),
]


def test_ready_screens_are_ready() -> None:
    assert [classify(h, pane) for h, pane in SAMPLES[:3]] == [Screen("ready")] * 3


@pytest.mark.parametrize(
    ("harness", "pane", "expected"),
    [
        ("codex", CODEX_HOOKS, Screen("dialog", "hooks-review")),
        ("claude", "Select login method:\n", Screen("auth", "Select login method")),
        ("claude", "Do you want to proceed?\n", Screen("permission", "permission")),
        (
            "codex",
            "Would you like to run the following command?\n",
            Screen("permission", "permission"),
        ),
        ("codex", CODEX_READY + "Waiting for startup\n", Screen("starting")),
        ("pi", "", Screen("starting")),
    ],
)
def test_screens_are_classified(harness: Harness, pane: str, expected: Screen) -> None:
    assert classify(harness, pane) == expected


@pytest.mark.parametrize(("harness", "pane"), SAMPLES)
def test_the_launcher_reads_startup_screens_as_the_smoke_helper_does(
    harness: Harness, pane: str
) -> None:
    product = classify(harness, pane)
    smoke = screens.classify(harness, pane)

    assert (product.state, product.detail) == (smoke.state, smoke.detail)


def shape(pattern: re.Pattern[str]) -> tuple[str, int]:
    return pattern.pattern, pattern.flags


def test_the_launcher_and_the_smoke_helper_share_every_startup_pattern() -> None:
    harnesses: tuple[Harness, ...] = ("claude", "codex", "pi")
    product = {
        h: (
            [(label, shape(p)) for label, p in readiness.DIALOGS[h]],
            shape(readiness.AUTH[h]),
            [shape(p) for p in readiness.READY[h]],
            shape(readiness.STARTING[h]) if h in readiness.STARTING else None,
        )
        for h in harnesses
    }
    smoke = {
        h: (
            [(d.label, shape(d.pattern)) for d in screens.DIALOGS[h] if d.label != "permission"],
            shape(screens.AUTH[h]),
            [shape(p) for p in screens.READY[h]],
            shape(screens.STARTING[h]) if h in screens.STARTING else None,
        )
        for h in harnesses
    }

    assert product == smoke
