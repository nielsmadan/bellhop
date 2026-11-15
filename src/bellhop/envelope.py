import re

from bellhop.codes import USAGE
from bellhop.errors import BellhopError

BODY_LIMIT = 8000
SENDER_LIMIT = 80
UNSAFE_SENDER = re.compile(r"[\x00-\x1f\x7f\]\[]+")


def check_body(body: str) -> str:
    if not body.strip():
        raise BellhopError("message body is empty", code=USAGE)
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
