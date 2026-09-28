"""The approval page: http://127.0.0.1:8766

Pending requests wait here until you allow, deny or edit them, or until their deadline passes (then: deny).
Several proxies can run at once (Claude Desktop starts one per MCP server). The first one to bind the port
hosts the page; the others post their requests to it and wait. If the host goes away, the next ask takes over.

Only this machine can reach it (127.0.0.1), and only the page itself can decide: requests with a foreign
Origin or Host are refused, and decisions must be JSON (so other sites can't post them without a preflight).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from kiri_gate.gate import Answer, Request
from kiri_gate.log import DecisionLog

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TIMED_OUT = "timed out"


class _Server(ThreadingHTTPServer):
    # Exactly one process may own the port. HTTPServer turns on SO_REUSEADDR, which on Windows lets a
    # second process bind the same port and take some of the traffic: two inboxes, or a hijacked one.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _Pending:
    def __init__(self, item: Dict[str, Any]) -> None:
        self.item = item
        self.done = threading.Event()
        self.answer: Dict[str, Any] = {}


class Inbox:
    """Holds pending requests and serves the page. One per port."""

    def __init__(self, port: int = 8766, log_path: Optional[str] = None, open_browser: bool = True) -> None:
        self.port, self.log_path, self.open_browser = port, log_path, open_browser
        self._pending: Dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._seen = 0.0     # last time the page polled
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._log: Optional[DecisionLog] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> bool:
        """Bind and serve on a thread. False if the port is taken."""
        try:
            self._httpd = _Server(("127.0.0.1", self.port), _handler(self))
        except OSError:
            return False
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return True

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._log:
            self._log._db.close()

    # ---- the queue -------------------------------------------------------------

    def ask(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Block until decided or item['deadline'] passes. Returns {approve, args, note}."""
        p = _Pending(item)
        with self._lock:
            self._pending[item["id"]] = p
        if self.open_browser and time.time() - self._seen > 3:
            threading.Thread(target=webbrowser.open, args=(self.url,), daemon=True).start()
        p.done.wait(max(0.0, item["deadline"] - time.time()))
        with self._lock:
            if self._pending.pop(item["id"], None) is not None and not p.done.is_set():
                p.answer = {"approve": False, "args": None, "note": TIMED_OUT}
        return p.answer

    def decide(self, key: str, approve: bool, args: Optional[Dict[str, Any]] = None, note: str = "") -> bool:
        with self._lock:
            p = self._pending.pop(key, None)
            if p is None:
                return False
            p.answer = {"approve": bool(approve), "args": args, "note": note}
            p.done.set()
        return True

    def cancel(self, key: str) -> None:
        self.decide(key, False, note="cancelled by the client")

    def state(self) -> Dict[str, Any]:
        self._seen = time.time()
        with self._lock:
            items = sorted((p.item for p in self._pending.values()), key=lambda i: i["deadline"])
        return {"now": time.time(), "pending": items}

    def recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.log_path or not os.path.exists(self.log_path):
            return []
        if self._log is None:
            self._log = DecisionLog(self.log_path)
        return self._log.recent(limit)


def _handler(inbox: Inbox):
    def hosts():  # read live: with port 0 the real port is only known after bind
        return {f"127.0.0.1:{inbox.port}", f"localhost:{inbox.port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # stdout/stderr noise would be bad next to an MCP stream
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: Any) -> None:
            self._send(code, json.dumps(data, default=str).encode(), "application/json")

        def _ok_host(self) -> bool:
            # Stops DNS rebinding: a foreign site's name pointed at 127.0.0.1 still sends its own Host.
            if self.headers.get("Host", "") not in hosts():
                self._json(403, {"error": "wrong host"})
                return False
            return True

        def do_GET(self) -> None:
            if not self._ok_host():
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._file("inbox.html", "text/html; charset=utf-8")
            elif path == "/kiri.css":
                self._file("kiri.css", "text/css; charset=utf-8")
            elif path == "/api/hello":
                self._json(200, {"kiri": "inbox"})
            elif path == "/api/state":
                self._json(200, inbox.state())
            elif path == "/api/log":
                self._json(200, {"rows": inbox.recent()})
            else:
                self._json(404, {"error": "not found"})

        def _file(self, name: str, ctype: str) -> None:
            with open(os.path.join(STATIC, name), "rb") as f:
                self._send(200, f.read(), ctype)

        def do_POST(self) -> None:
            # Read the body before any refusal: on Windows, closing with unread data resets the
            # connection and the caller never sees the error.
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                self.close_connection = True
                return self._json(413, {"error": "too big"})
            raw = self.rfile.read(n) if n > 0 else b""
            if not self._ok_host():
                return
            origin = self.headers.get("Origin")
            if origin and origin.split("//", 1)[-1] not in hosts():
                return self._json(403, {"error": "cross-origin request refused"})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(415, {"error": "send JSON"})
            try:
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "bad JSON"})
            path = self.path.split("?", 1)[0]
            if path == "/api/decide":
                args = body.get("args")
                if args is not None and not isinstance(args, dict):
                    return self._json(400, {"error": "edited args must be a JSON object"})
                ok = inbox.decide(str(body.get("id", "")), body.get("approve") is True, args, str(body.get("note", ""))[:2000])
                self._json(200 if ok else 409, {"ok": ok} if ok else {"error": "already decided or timed out"})
            elif path == "/api/ask":   # from another proxy; blocks until decided
                if not isinstance(body.get("id"), str) or not isinstance(body.get("deadline"), (int, float)):
                    return self._json(400, {"error": "bad request"})
                self._json(200, inbox.ask(body))
            elif path == "/api/cancel":
                inbox.cancel(str(body.get("id", "")))
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})

    return H


class Approver:
    """What the proxy asks through. Hosts the inbox if it can, otherwise uses the one already running."""

    def __init__(self, port: int = 8766, log_path: Optional[str] = None, open_browser: bool = True,
                 notify=None) -> None:
        self.port, self.log_path, self.open_browser = port, log_path, open_browser
        self.inbox: Optional[Inbox] = None
        self._lock = threading.Lock()
        self._notify = notify or (lambda text: None)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def ensure(self) -> str:
        """Host the page if the port is free. Returns 'host', 'remote' or 'blocked'."""
        with self._lock:
            if self.inbox is not None:
                return "host"
            ib = Inbox(self.port, self.log_path, self.open_browser)
            if ib.start():
                self.inbox, self.port = ib, ib.port
                self._notify(f"approval page at {ib.url}")
                return "host"
        try:
            with urllib.request.urlopen(f"{self.url}/api/hello", timeout=2) as r:
                if json.loads(r.read()).get("kiri") == "inbox":
                    return "remote"
        except (OSError, ValueError):
            pass
        return "blocked"

    def ask(self, req: Request, key: str, deadline: float, source: str = "") -> Answer:
        item = {"id": key, "tool": req.tool, "cls": req.cls.value, "args": req.args, "goal": req.goal,
                "why": req.why, "confidence": req.confidence, "source": source,
                "created": time.time(), "deadline": deadline}
        mode = self.ensure()
        if mode == "host":
            a = self.inbox.ask(item)
        elif mode == "remote":
            try:
                a = _post(f"{self.url}/api/ask", item, timeout=max(1.0, deadline - time.time()) + 5)
            except (OSError, ValueError):
                a = {"approve": False, "note": "the approval page went away"}
        else:
            a = {"approve": False, "note": f"port {self.port} is used by something else, so there's nowhere to ask"}
        args = a.get("args")
        return Answer(a.get("approve") is True, args if isinstance(args, dict) else None, str(a.get("note") or ""))

    def cancel(self, key: str) -> None:
        if self.inbox is not None:
            self.inbox.cancel(key)
            return
        try:
            _post(f"{self.url}/api/cancel", {"id": key}, timeout=2)
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        if self.inbox is not None:
            self.inbox.stop()


def _post(url: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body, default=str).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())
