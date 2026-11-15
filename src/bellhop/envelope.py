import re
import unicodedata

from bellhop.codes import USAGE
from bellhop.errors import BellhopError

BODY_LIMIT = 8000
SENDER_LIMIT = 80
UNSAFE_SENDER = re.compile(r"[\x00-\x1f\x7f-\x9f\]\[]+")


def check_controls(text: str, what: str) -> str:
    control = next((c for c in text if c not in "\t\n" and unicodedata.category(c) == "Cc"), None)
    if control is not None:
        raise BellhopError(
            f"{what} contains the control character U+{ord(control):04X}; "
            "only tab and newline are allowed",
            code=USAGE,
        )
    return text


def check_body(body: str) -> str:
    if not body.strip():
        raise BellhopError("message body is empty", code=USAGE)
    check_controls(body, "message body")
    if not body.isascii() and any(unicodedata.category(c) == "Cs" for c in body):
        raise BellhopError("message body is not valid UTF-8 text", code=USAGE)
    if len(body) > BODY_LIMIT:
        raise BellhopError(
            f"message body is {len(body)} characters; the limit is {BODY_LIMIT}", code=USAGE
        )
    return body


def clean_sender(name: str) -> str:
    cleaned = UNSAFE_SENDER.sub(" ", name).strip()
    return " ".join(cleaned.split())[:SENDER_LIMIT] or "unknown"


def prefix(message_id: str) -> str:
    return f"[bellhop {message_id} from"


def render(message_id: str, sender: str, body: str, waiting: int) -> str:
    text = f"{prefix(message_id)} {sender}] {body}"
    if waiting:
        return f"{text}\n(bellhop: {waiting} more waiting; run `bellhop inbox`)"
    return text
