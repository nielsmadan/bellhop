import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

SERVER_INFO = {"name": "bellhop-spike-relay", "version": "0.1"}
CHILD_VARS = ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN")
PRIORITY = {"interrupt": "next", "boundary": "later"}


def log(line: str) -> None:
    path = os.environ.get("BELLHOP_SPIKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.time():.3f} {os.getpid()} {line}\n")


def forward(text: str, priority: str) -> str:
    sock_path = os.environ.get(CHILD_VARS[0])
    token = os.environ.get(CHILD_VARS[1])
    if not sock_path or not token:
        return "error no child messaging socket or token in this process"
    frame = {
        "type": "user",
        "message": {"role": "user", "content": text},
        "priority": priority,
        "msgV": 1,
        "msg_id": str(uuid.uuid4()),
    }
    payload = f"{json.dumps({'type': 'auth', 'token': token})}\n{json.dumps(frame)}\n".encode()
    peer = socket.socket(socket.AF_UNIX)
    peer.settimeout(2.0)
    try:
        peer.connect(sock_path)
        peer.sendall(payload)
        with contextlib.suppress(OSError):
            peer.shutdown(socket.SHUT_WR)
        with contextlib.suppress(TimeoutError, OSError):
            peer.recv(4096)
    finally:
        peer.close()
    return f"ok forwarded via child-env priority={priority}"


def read_request(conn: socket.socket) -> tuple[str, str]:
    chunks = []
    while True:
        data = conn.recv(65536)
        if not data:
            break
        chunks.append(data)
    header, _, body = b"".join(chunks).decode("utf-8", errors="replace").partition("\n")
    return header.strip(), body


def handle(conn: socket.socket, mode: str) -> None:
    with conn:
        header, body = read_request(conn)
        try:
            if header == "ping":
                reply = "ok pong"
            elif header.startswith("deliver "):
                how = header.removeprefix("deliver ").strip()
                if mode == "print":
                    sys.stdout.write(json.dumps({"text": body, "how": how}) + "\n")
                    sys.stdout.flush()
                    reply = "ok handed to the mod"
                else:
                    reply = forward(body, PRIORITY.get(how, "next"))
            else:
                reply = f"error unknown header {header!r}"
        except Exception as error:  # noqa: BLE001
            reply = f"error {type(error).__name__}: {error}"
        log(f"{header} -> {reply}")
        conn.sendall((reply + "\n").encode())


def serve(mode: str) -> None:
    path = os.environ["BELLHOP_SPIKE_SOCKET"]
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    os.chmod(path, 0o600)
    server.listen(8)
    present = {name: "yes" if os.environ.get(name) else "no" for name in CHILD_VARS}
    log(f"listening mode={mode} ppid={os.getppid()} socket={present[CHILD_VARS[0]]} token={present[CHILD_VARS[1]]}")
    while True:
        conn, _ = server.accept()
        threading.Thread(target=handle, args=(conn, mode), daemon=True).start()


def mcp_reply(request: dict) -> dict | None:
    method, req_id = request.get("method"), request.get("id")
    if req_id is None:
        return None
    if method == "initialize":
        version = (request.get("params") or {}).get("protocolVersion", "2025-06-18")
        return {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": []}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


def run_mcp() -> int:
    threading.Thread(target=serve, args=("forward",), daemon=True).start()
    for line in sys.stdin:
        try:
            reply = mcp_reply(json.loads(line))
        except json.JSONDecodeError:
            continue
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


def detach() -> int:
    child = subprocess.Popen(
        [sys.executable, "-I", os.path.abspath(__file__), "serve", os.environ["BELLHOP_SPIKE_SOCKET"]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    with open(os.environ["BELLHOP_SPIKE_SOCKET"] + ".pid", "w", encoding="utf-8") as handle:
        handle.write(f"{child.pid}\n")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "mcp"
    if mode == "mcp":
        return run_mcp()
    if mode == "detach":
        return detach()
    serve("print" if mode == "print" else "forward")
    return 0


if __name__ == "__main__":
    sys.exit(main())
