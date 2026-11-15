import contextlib
import json
import secrets
import socket
import sys
import time
from pathlib import Path

SPIKES = Path(__file__).resolve().parent
REPO = SPIKES.parent
RELAY = SPIKES / "claude_relay.py"
PI_EXTENSION = SPIKES / "pi-ext" / "bellhop-spike.ts"
PYTHON = sys.executable


def token() -> str:
    return "".join(secrets.choice("abcdef") for _ in range(8))


def envelope(tok: str, scenario: str) -> str:
    return f"[bellhop {tok} from spike] {scenario} test: reply with exactly ACK {tok} and nothing else."


def send(path: Path, header: str, body: str, timeout: float = 2.0) -> str:
    conn = socket.socket(socket.AF_UNIX)
    conn.settimeout(timeout)
    try:
        conn.connect(str(path))
        conn.sendall(f"{header}\n{body}".encode())
        with contextlib.suppress(OSError):
            conn.shutdown(socket.SHUT_WR)
        try:
            return conn.recv(4096).decode(errors="replace").strip() or "unknown no reply"
        except TimeoutError:
            return "unknown no reply within 2s"
    except OSError as error:
        return f"error {type(error).__name__}: {error}"
    finally:
        conn.close()


def wait_socket(path: Path, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and send(path, "ping", "", 1.0).startswith("ok"):
            return True
        time.sleep(0.5)
    return False


def jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def claude_transcripts(config_dir: Path) -> list[Path]:
    return sorted((config_dir / "projects").glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)


def claude_hits(path: Path, tok: str) -> dict[str, int]:
    hits: dict[str, int] = {}
    for row in jsonl(path):
        if tok in json.dumps(row):
            kind = row.get("type", "?")
            if kind == "user":
                content = (row.get("message") or {}).get("content")
                kind = "user-text" if isinstance(content, str) or "tool_result" not in json.dumps(content) else "user-tool-result"
            hits[kind] = hits.get(kind, 0) + 1
    return hits


def claude_acked(path: Path, tok: str) -> bool:
    return any(
        row.get("type") == "assistant" and f"ACK {tok}" in json.dumps(row)
        for row in jsonl(path)
    )


def codex_rollouts(codex_home: Path) -> list[Path]:
    return sorted((codex_home / "sessions").rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)


def codex_hits(path: Path, tok: str) -> dict[str, int]:
    hits: dict[str, int] = {}
    for row in jsonl(path):
        if tok in json.dumps(row):
            payload = row.get("payload") or {}
            kind = f"{row.get('type')}:{payload.get('type')}:{payload.get('role', '')}"
            hits[kind] = hits.get(kind, 0) + 1
    return hits


def pi_sessions(pi_dir: Path) -> list[Path]:
    return sorted((pi_dir / "sessions").rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)


def pi_hits(path: Path, tok: str) -> dict[str, int]:
    hits: dict[str, int] = {}
    for row in jsonl(path):
        if tok in json.dumps(row):
            message = row.get("message") or {}
            kind = f"{row.get('type')}:{message.get('role', '')}"
            hits[kind] = hits.get(kind, 0) + 1
    return hits
