"""Stdio MCP proxy: client <-> kiri-gate <-> real server.

Every message passes through untouched except tools/call, which goes through the Gate first.
Denied calls come back to the client as a tool error (isError), so the model sees it and moves on.
Messages are newline-delimited JSON-RPC, as in the MCP stdio transport.

Asking has a deadline. Claude Desktop cancels a tool call after 60s (the MCP TypeScript SDK default; it
ignores config and progress notifications), so by default an unanswered ask is denied at 55s. The model
then gets a clear "not approved, did not run" instead of a timeout it might retry blindly.

Doing things twice: every non-READ call gets a ledger key from the server name, tool and arguments. The
proxy forwards without waiting, so it keeps the ledger itself: the server's reply marks the key DONE (with
the result) or UNKNOWN (isError or a JSON-RPC error). An identical call after DONE gets the saved result and
never reaches the server; after UNKNOWN it goes to the human, whatever its class.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, BinaryIO, Callable, Dict, List, Optional

from kiri_gate import config as kconfig
from kiri_gate.classes import Class, parse
from kiri_gate.gate import Answer, Denied, Gate, Request
from kiri_gate.inbox import TIMED_OUT, Approver
from kiri_gate.ledger import CLAIMED, DONE, UNKNOWN, Ledger, make_key
from kiri_gate.log import DecisionLog

ASK_TIMEOUT = 55.0  # seconds; Claude Desktop gives up at 60


class Proxy:
    def __init__(self, cmd: List[str], config_path: str = "kiri.toml",
                 ask: Optional[Callable[[Request], Answer]] = None, log: Optional[DecisionLog] = None,
                 approver: Optional[Approver] = None, ask_timeout: float = ASK_TIMEOUT, name: str = "",
                 ledger: Optional[Ledger] = None,
                 scorer=None, threshold: float = 0.5,
                 stdin: Optional[BinaryIO] = None, stdout: Optional[BinaryIO] = None) -> None:
        """ask: a plain Request -> Answer function (tests, custom UIs). Otherwise the approval page is used."""
        self.cmd = list(cmd)
        self.name = name or (os.path.basename(self.cmd[-1]) if self.cmd else "")
        self.config_path = config_path
        self.config = kconfig.load(config_path)
        self._cfg_mtime = self._mtime()
        self.ask_timeout = ask_timeout
        self._ask_fn = ask
        self.approver = None if ask else (approver or Approver(notify=_note))
        self.ledger = ledger or Ledger()
        self.gate = Gate(ask=self._ask, scorer=scorer, threshold=threshold, log=log)
        self.cin = stdin or sys.stdin.buffer
        self.cout = stdout or sys.stdout.buffer
        self.server: Optional[subprocess.Popen] = None
        self._out_lock = threading.Lock()
        self._in_lock = threading.Lock()
        self._reg_lock = threading.Lock()
        self._list_ids: Dict[Any, bool] = {}
        self._seen: Dict[str, Dict[str, Any]] = {}
        self._drafted = False
        self._current = threading.local()
        self._inflight: Dict[Any, str] = {}   # request id -> approval key, while a call is being decided
        self._cancelled: set = set()
        self._effects: Dict[Any, str] = {}    # request id -> ledger key, once forwarded and until the server answers
        self._done = threading.Event()

    # ---- lifecycle -------------------------------------------------------------

    def run(self) -> int:
        exe = shutil.which(self.cmd[0]) or self.cmd[0]  # finds npx.cmd etc. on Windows
        self.server = subprocess.Popen([exe] + self.cmd[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        if self.approver is not None:  # up from the start, so the page is there before the first ask
            threading.Thread(target=self.approver.ensure, daemon=True).start()
        threading.Thread(target=self._pump_client, daemon=True).start()
        threading.Thread(target=self._pump_server, daemon=True).start()
        self._done.wait()
        try:
            self.server.stdin.close()
        except OSError:
            pass
        try:
            code = self.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server.kill()
            code = self.server.wait()
        self.server.stdout.close()
        for key in list(self._effects.values()):
            self.ledger.unknown(key, "kiri-gate stopped before the server answered")
        if self.approver is not None:
            self.approver.close()
        return code

    # ---- client -> server ------------------------------------------------------

    def _pump_client(self) -> None:
        try:
            for line in self.cin:
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except (ValueError, RecursionError):
                    # Never forward what we can't read. A server may read it differently (invalid UTF-8 is
                    # decoded with replacement by the MCP TypeScript SDK) and run a tools/call we never saw.
                    self._to_client({"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": "kiri-gate: parse error, not forwarded"}})
                    continue
                # Batches were dropped from MCP, but unpack them anyway so no tools/call slips through inside one.
                for m in (msg if isinstance(msg, list) else [msg]):
                    self._from_client(m)
        finally:
            self._done.set()

    def _from_client(self, msg: Any) -> None:
        if not isinstance(msg, dict):  # e.g. a nested batch: nothing valid looks like this, so it isn't forwarded
            return self._to_client({"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32600, "message": "kiri-gate: invalid request, not forwarded"}})
        method = msg.get("method")
        if method == "tools/call":
            if "id" not in msg:
                return  # a call without an id is invalid; never let it through ungated
            threading.Thread(target=self._handle_call, args=(msg, time.time()), daemon=True).start()
            return
        if method == "notifications/cancelled":
            rid = (msg.get("params") or {}).get("requestId")
            if rid in self._effects:  # already sent, so it may still happen; the server's reply overrides this
                self.ledger.unknown(self._effects[rid], "the client cancelled while it was running")
            key = self._inflight.get(rid)
            if key is not None:
                self._cancelled.add(rid)
                if self.approver is not None:
                    self.approver.cancel(key)
        if method == "tools/list" and "id" in msg:
            self._list_ids[msg["id"]] = True
        self._to_server(msg)

    def _handle_call(self, msg: Dict[str, Any], received: float) -> None:
        mid = msg["id"]
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return self._to_client({"jsonrpc": "2.0", "id": mid,
                                    "error": {"code": -32602, "message": "tools/call needs a name and object arguments"}})
        self._reload_config()
        self._register(name)
        cur = self._current
        cur.msg, cur.deadline, cur.key, cur.timed_out = msg, received + self.ask_timeout, uuid.uuid4().hex, False
        self._inflight[mid] = cur.key
        must_ask = ""
        cur.checked, cur.seen = None, None  # the key and ledger state this call is routed on
        if self.gate.classes()[name] is not Class.READ:
            cur.checked = make_key(name, args, scope=self.name)
            state, saved, err = self.ledger.get(cur.checked)
            cur.seen = state
            if state == DONE:
                return self._replay(mid, name, args, saved)
            if state == CLAIMED:
                self._inflight.pop(mid, None)
                return self._tool_error(mid, f"Blocked by kiri-gate: an identical {name} call is already running.")
            if state == UNKNOWN:
                must_ask = (f"an identical call failed with {err or 'an error'} and may already have happened. "
                            f"Check before approving.")
        if not must_ask and name not in self.config.tools:
            must_ask = (f"{name} isn't confirmed in kiri.toml yet, so it asks every time. "
                        f"Tick \"remember\" below to set its class for good.")
        try:
            self.gate.run(name, args, must_ask=must_ask)  # on act, the forwarder sends it; the reply flows back
        except Denied as e:
            if mid in self._cancelled:
                pass  # the client gave up on this request; MCP says don't answer it
            elif cur.timed_out:
                self._tool_error(mid, f"Blocked by kiri-gate: nobody approved {name} in time, so it did not run. "
                                      f"Ask the user to approve it at {self._where()} and then try again.")
            else:
                self._tool_error(mid, f"Blocked by kiri-gate: {e}. The tool did not run.")
        except Exception as e:
            if mid not in self._cancelled:
                self._tool_error(mid, f"kiri-gate failed ({type(e).__name__}: {e}). The tool did not run.")
        finally:
            self._inflight.pop(mid, None)
            self._cancelled.discard(mid)

    def _replay(self, mid: Any, name: str, args: Dict[str, Any], saved: Any) -> None:
        note = {"type": "text", "text": "kiri-gate: an identical call already succeeded, so it was not sent again."}
        result = dict(saved) if isinstance(saved, dict) else {"content": []}
        result["content"] = list(result.get("content") or []) + [note]
        self._inflight.pop(mid, None)
        if self.gate.log is not None:  # log first, so anything reading the log after the reply sees it
            self.gate.log.record(ts=time.time(), tool=name, cls=self.gate.classes()[name].name, goal="", args=args,
                                 decision="deduped", why="an identical call already succeeded", confidence=None,
                                 approved=None, edited=False, note="")
        self._to_client({"jsonrpc": "2.0", "id": mid, "result": result})

    def _ask(self, req: Request) -> Answer:
        if self._ask_fn is not None:
            a = self._ask_fn(req)
        else:
            cur = self._current
            a = self.approver.ask(req, key=cur.key, deadline=cur.deadline, source=self.name,
                                  extra=self._card_extra(req.tool))
            cur.timed_out = not a.approve and a.note == TIMED_OUT
        if a.approve and a.remember and req.tool not in self.config.tools:
            self._confirm(req.tool, a.remember)
        return a

    def _card_extra(self, name: str) -> Dict[str, Any]:
        """For a tool not in [tools]: what the server's annotations suggest, so the human can confirm it."""
        if name in self.config.tools:
            return {}
        cls, why = kconfig.suggest(self._seen.get(name) or {})
        if name not in self._seen:
            why = "the server didn't list this tool"
        return {"confirmable": True, "suggested": cls, "suggested_why": why}

    def _confirm(self, name: str, cls_name: str) -> None:
        """The human ticked "remember" on the approval page: save the class and use it from the next call."""
        try:
            cls = parse(cls_name)
            with self._reg_lock:
                kconfig.confirm(self.config_path, name, cls)
                self.config.tools[name] = cls
                self.config.unconfirmed.pop(name, None)
                self._cfg_mtime = self._mtime()
                if name in self.gate.classes():
                    self.gate.reclassify(name, cls)
            _note(f"{name} is now {cls.value}, saved to {os.path.abspath(self.config_path)}")
        except (OSError, ValueError) as e:
            _note(f"couldn't save {name} as {cls_name} ({e}); it will keep asking")

    def _mtime(self) -> str:
        """What kiri.toml looks like now. Its content, not its timestamp: an edit inside the same timestamp tick
        (fast saves, coarse filesystems) must still be picked up, or an old class keeps applying."""
        try:
            with open(self.config_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except OSError:
            return ""

    def _reload_config(self) -> None:
        """Pick up hand edits to kiri.toml without restarting the client."""
        m = self._mtime()
        if m == self._cfg_mtime:
            return
        try:
            cfg = kconfig.load(self.config_path)
        except (OSError, ValueError) as e:
            _note(f"kiri.toml changed but can't be read ({e}); keeping the previous classes")
            self._cfg_mtime = m
            return
        with self._reg_lock:
            self.config, self._cfg_mtime = cfg, m
            for name, old in self.gate.classes().items():
                new = cfg.class_for(name)
                if new is not old:
                    self.gate.reclassify(name, new)
                    _note(f"{name}: {old.value} -> {new.value} (kiri.toml changed)")

    def _where(self) -> str:
        return self.approver.url if self.approver is not None else "the approval page"

    def _register(self, name: str) -> None:
        with self._reg_lock:
            if name in self.gate.classes():
                return

            cls = self.config.class_for(name)

            def forward(**args):
                msg = dict(self._current.msg)
                msg["params"] = dict(msg.get("params") or {}, arguments=args)
                if self.gate.classes()[name] is not Class.READ:  # live: the class can be confirmed later  # args are final here, after any edit by the human
                    key = make_key(name, args, scope=self.name)
                    # Only claim the row in the state this call was routed (and maybe approved) on. If an
                    # identical call went UNKNOWN meanwhile, it may have happened: refuse, and the retry is asked.
                    seen = self._current.seen if key == self._current.checked else None
                    if not self.ledger.claim(key, name, args, expect=seen):
                        if self.ledger.get(key)[0] == UNKNOWN:
                            raise Denied(f"an identical {name} call failed and may already have happened; "
                                         f"try again to be asked about it")
                        raise Denied(f"an identical {name} call already ran or is running")
                    self._effects[msg["id"]] = key
                self._to_server(msg)

            # The proxy keeps its own ledger (it forwards without waiting), so the Gate's is off here.
            self.gate.register(forward, cls, name=name, dedupe=False)

    def _tool_error(self, mid: Any, text: str) -> None:
        self._to_client({"jsonrpc": "2.0", "id": mid,
                         "result": {"content": [{"type": "text", "text": text}], "isError": True}})

    # ---- server -> client ------------------------------------------------------

    def _pump_server(self) -> None:
        try:
            for line in self.server.stdout:
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except (ValueError, RecursionError):
                    msg = None
                if isinstance(msg, dict) and "method" not in msg:
                    if self._list_ids.pop(msg.get("id"), False):
                        self._learn((msg.get("result") or {}).get("tools") or [])
                    key = self._effects.pop(msg.get("id"), None)
                    if key is not None:
                        self._settle(key, msg)
                self._to_client_raw(line)
        finally:
            self._done.set()

    def _settle(self, key: str, msg: Dict[str, Any]) -> None:
        result = msg.get("result")
        if "error" in msg or not isinstance(result, dict):
            self.ledger.unknown(key, "JSON-RPC error " + json.dumps(msg.get("error"))[:300])
        elif result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
            self.ledger.unknown(key, "a tool error: " + text[:300])
        else:
            self.ledger.done(key, result)

    def _learn(self, tools: List[Dict[str, Any]]) -> None:
        new = [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"] not in self._seen]
        for t in tools:
            if isinstance(t, dict) and isinstance(t.get("name"), str):
                self._seen[t["name"]] = t
        # Write the draft once, only when there's no kiri.toml. Rewriting it later would undo hand edits
        # made since (and could put back a class you just changed).
        if not os.path.exists(self.config_path):
            try:
                with open(self.config_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(kconfig.draft(list(self._seen.values()), self.config.tools))
                if not self._drafted:
                    _note(f"first run: wrote {os.path.abspath(self.config_path)} with {len(self._seen)} tools. "
                          f"Until you confirm a tool's class, it asks every time, even reads. Confirm each one "
                          f"by ticking \"remember\" on {self._where()}, or move its line to [tools].")
                self._drafted = True
            except OSError as e:
                _note(f"couldn't write {self.config_path} ({e}); every tool will ask")
            return
        unknown = [t["name"] for t in new if t["name"] not in self.config.tools]
        if unknown:
            _note(f"not in [tools] of {self.config_path}, so they always ask: {', '.join(unknown)}")

    # ---- io --------------------------------------------------------------------

    def _to_server(self, msg: Any) -> None:
        self._to_server_raw(json.dumps(msg, separators=(",", ":")).encode() + b"\n")

    def _to_server_raw(self, line: bytes) -> None:
        with self._in_lock:
            self.server.stdin.write(line if line.endswith(b"\n") else line + b"\n")
            self.server.stdin.flush()

    def _to_client(self, msg: Any) -> None:
        self._to_client_raw(json.dumps(msg, separators=(",", ":")).encode() + b"\n")

    def _to_client_raw(self, line: bytes) -> None:
        with self._out_lock:
            self.cout.write(line if line.endswith(b"\n") else line + b"\n")
            self.cout.flush()


def _note(text: str) -> None:
    # stderr is the only safe place: stdout belongs to the MCP client.
    print(f"kiri-gate: {text}", file=sys.stderr, flush=True)
