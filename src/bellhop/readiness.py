import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from bellhop.models import Harness

type ScreenState = Literal["ready", "dialog", "auth", "permission", "starting"]


@dataclass(frozen=True)
class Screen:
    state: ScreenState
    detail: str = ""


DIALOGS: Mapping[Harness, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "claude": (
        ("trust-folder", re.compile(r"trust (the files in )?this folder", re.IGNORECASE)),
        ("theme", re.compile(r"Choose the text style", re.IGNORECASE)),
        (
            "dev-channels",
            re.compile(
                r"Loading development channels.*1\. I am using this for local development",
                re.DOTALL,
            ),
        ),
    ),
    "codex": (
        (
            "resume-directory",
            re.compile(r"Working directory · resume.*1\. Use session directory", re.DOTALL),
        ),
        ("hooks-review", re.compile(r"Hooks need review.*2\. Trust all and continue", re.DOTALL)),
        (
            "trust-dir",
            re.compile(
                r"(trust the contents of this directory|allow Codex to work in this folder)",
                re.IGNORECASE,
            ),
        ),
        (
            "model-nudge",
            re.compile(r"(Try new model|Choose how you'd like Codex to proceed)", re.IGNORECASE),
        ),
    ),
    "pi": (),
}

PERMISSION: Mapping[Harness, re.Pattern[str]] = {
    "claude": re.compile(r"Do you want to (proceed|make this edit|create)", re.IGNORECASE),
    "codex": re.compile(r"Would you like to (run|make)", re.IGNORECASE),
}

AUTH: Mapping[Harness, re.Pattern[str]] = {
    "claude": re.compile(
        r"(Select login method|Please run /login|Invalid API key|OAuth token (has )?expired)",
        re.IGNORECASE,
    ),
    "codex": re.compile(
        r"(Sign in with ChatGPT|Provide your own API key|Please run `?codex login)", re.IGNORECASE
    ),
    "pi": re.compile(r"No API key (found|configured)", re.IGNORECASE),
}

READY: Mapping[Harness, tuple[re.Pattern[str], ...]] = {
    "claude": (
        re.compile(r"^❯", re.MULTILINE),
        re.compile(r"(\? for shortcuts|permissions on|mode on)"),
    ),
    "codex": (
        re.compile(r"^› ", re.MULTILINE),
        re.compile(r"(\? for shortcuts|context left|Context \d+% used)"),
        re.compile(r"^\s*\S+ (?:minimal|low|medium|high|xhigh) · |Context \d+% used", re.MULTILINE),
    ),
    "pi": (re.compile(r"^─{20,}$", re.MULTILINE), re.compile(r"\S+ • thinking \w+")),
}

STARTING: Mapping[Harness, re.Pattern[str]] = {
    "codex": re.compile(r"Waiting for startup|OpenAI Codex \(v[\d.]+\)\s+loading"),
}


def classify(harness: Harness, pane: str) -> Screen:
    auth = AUTH[harness].search(pane)
    if auth:
        return Screen("auth", auth.group(0))
    dialog = next((label for label, pattern in DIALOGS[harness] if pattern.search(pane)), None)
    if dialog is not None:
        return Screen("dialog", dialog)
    permission = PERMISSION.get(harness)
    if permission is not None and permission.search(pane):
        return Screen("permission", "permission")
    starting = STARTING.get(harness)
    if starting is not None and starting.search(pane):
        return Screen("starting")
    if all(pattern.search(pane) for pattern in READY[harness]):
        return Screen("ready")
    return Screen("starting")
