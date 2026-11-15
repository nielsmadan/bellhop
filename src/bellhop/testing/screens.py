import re
from dataclasses import dataclass
from typing import Literal

type Agent = Literal["claude", "codex", "pi"]
type ScreenState = Literal["ready", "dialog", "auth", "starting"]


@dataclass(frozen=True)
class Dialog:
    label: str
    pattern: re.Pattern[str]
    keys: tuple[str, ...]


@dataclass(frozen=True)
class Screen:
    state: ScreenState
    detail: str = ""
    keys: tuple[str, ...] = ()


DIALOGS: dict[Agent, tuple[Dialog, ...]] = {
    "claude": (
        Dialog(
            "trust-folder",
            re.compile(r"trust (the files in )?this folder", re.IGNORECASE),
            ("Enter",),
        ),
        Dialog("theme", re.compile(r"Choose the text style", re.IGNORECASE), ("Enter",)),
        Dialog(
            "dev-channels",
            re.compile(
                r"Loading development channels.*1\. I am using this for local development",
                re.DOTALL,
            ),
            ("Enter",),
        ),
    ),
    "codex": (
        Dialog(
            "resume-directory",
            re.compile(r"Working directory · resume.*1\. Use session directory", re.DOTALL),
            ("Enter",),
        ),
        Dialog(
            "hooks-review",
            re.compile(r"Hooks need review.*2\. Trust all and continue", re.DOTALL),
            ("Down", "Enter"),
        ),
        Dialog(
            "trust-dir",
            re.compile(
                r"(trust the contents of this directory|allow Codex to work in this folder)",
                re.IGNORECASE,
            ),
            ("Enter",),
        ),
        Dialog(
            "model-nudge",
            re.compile(r"(Try new model|Choose how you'd like Codex to proceed)", re.IGNORECASE),
            ("Escape",),
        ),
    ),
    "pi": (),
}

AUTH: dict[Agent, re.Pattern[str]] = {
    "claude": re.compile(
        r"(Select login method|Please run /login|Invalid API key|OAuth token (has )?expired)",
        re.IGNORECASE,
    ),
    "codex": re.compile(
        r"(Sign in with ChatGPT|Provide your own API key|Please run `?codex login)", re.IGNORECASE
    ),
    "pi": re.compile(r"No API key (found|configured)", re.IGNORECASE),
}

READY: dict[Agent, tuple[re.Pattern[str], ...]] = {
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

STARTING: dict[Agent, re.Pattern[str]] = {
    "codex": re.compile(r"Waiting for startup|OpenAI Codex \(v[\d.]+\)\s+loading"),
}


def classify(agent: Agent, pane: str) -> Screen:
    auth = AUTH[agent].search(pane)
    if auth:
        return Screen("auth", auth.group(0))
    dialog = next((d for d in DIALOGS[agent] if d.pattern.search(pane)), None)
    if dialog:
        return Screen("dialog", dialog.label, dialog.keys)
    starting = STARTING.get(agent)
    if starting and starting.search(pane):
        return Screen("starting")
    if all(pattern.search(pane) for pattern in READY[agent]):
        return Screen("ready")
    return Screen("starting")
