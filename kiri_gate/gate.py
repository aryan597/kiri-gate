"""The gate: every tool call goes through here, and here decides act or ask.

Rules, in order:
  READ          acts.
  IRREVERSIBLE  asks. Always. No score, no setting and no model can change this.
  EXTERNAL      asks. Always.
  UNDOABLE      acts if a scorer says it's likely fine (>= threshold), otherwise asks.
                With no scorer, it acts: undoable means you can take it back.

A tool's class is fixed when it's registered and can't be registered twice.
The agent never holds the real function; it only gets the gated wrapper.

Doing things twice: IRREVERSIBLE and EXTERNAL calls are deduped by default (see ledger.py). An identical
call that already succeeded returns the saved result and doesn't run. One that failed may have happened
anyway, so it's reconciled (if the tool has a reconcile function) or asked about. It never runs blindly.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from kiri_gate.classes import Class, always_asks
from kiri_gate.ledger import CLAIMED, DONE, KEY_ARG, UNKNOWN, Ledger, make_key
from kiri_gate.log import DecisionLog

_goal: contextvars.ContextVar[str] = contextvars.ContextVar("kiri_goal", default="")


class Denied(Exception):
    """Raised inside the agent's call when a human says no. Agents should treat it as a normal tool error."""


class NotExecuted(Exception):
    """Raise this from a tool when you know nothing happened (e.g. a 400, or a 429 before the request ran).
    The claim is released, so a retry can run."""


class RegistryError(Exception):
    pass


@dataclass
class Request:
    """What the human is asked to decide on."""
    tool: str
    cls: Class
    args: Dict[str, Any]
    goal: str
    why: str                       # why Kiri is asking
    confidence: Optional[float] = None


@dataclass
class Answer:
    approve: bool
    args: Optional[Dict[str, Any]] = None   # edited arguments, if the human changed them
    note: str = ""


@dataclass
class _Tool:
    name: str
    cls: Class
    fn: Callable
    description: str = ""
    dedupe: bool = False
    reconcile: Optional[Callable[..., Any]] = None
    takes_key: bool = False        # fn has an idempotency_key parameter


def _takes_key(fn: Callable) -> bool:
    try:
        return KEY_ARG in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


@dataclass
class Gate:
    ask: Optional[Callable[[Request], Answer]] = None
    scorer: Optional[Callable[[str, str, Dict[str, Any]], float]] = None
    threshold: float = 0.5
    log: Optional[DecisionLog] = None
    ledger: Optional[Ledger] = None
    _tools: Dict[str, _Tool] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.ask is None:
            from kiri_gate.ask import terminal
            self.ask = terminal
        if self.ledger is None:
            self.ledger = Ledger()
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")

    # ---- registration --------------------------------------------------------

    def tool(self, cls: Class, name: Optional[str] = None, description: str = "",
             dedupe: Optional[bool] = None, reconcile: Optional[Callable[..., Any]] = None):
        """Decorator: @gate.tool(EXTERNAL) def send_email(...).

        dedupe: default on for IRREVERSIBLE and EXTERNAL. Pass True to opt another class in.
        reconcile: called with the same arguments when an identical earlier call failed. Return the result
                   if the effect already happened, or None if it didn't.
        """
        if not isinstance(cls, Class):
            raise RegistryError("class must be READ, UNDOABLE, IRREVERSIBLE or EXTERNAL")

        def wrap(fn: Callable) -> Callable:
            tname = name or fn.__name__
            with self._lock:
                if tname in self._tools:
                    raise RegistryError(f"tool '{tname}' is already registered as {self._tools[tname].cls.name}")
                self._tools[tname] = _Tool(tname, cls, fn, description or (inspect.getdoc(fn) or "").split("\n")[0],
                                           always_asks(cls) if dedupe is None else bool(dedupe), reconcile,
                                           _takes_key(fn))

            @functools.wraps(fn)
            def gated(*args, **kwargs):
                return self.call(tname, *args, **kwargs)

            gated.kiri_class = cls  # type: ignore[attr-defined]
            return gated

        return wrap

    def register(self, fn: Callable, cls: Class, name: Optional[str] = None, description: str = "",
                 dedupe: Optional[bool] = None, reconcile: Optional[Callable[..., Any]] = None) -> Callable:
        return self.tool(cls, name, description, dedupe, reconcile)(fn)

    def classes(self) -> Dict[str, Class]:
        return {n: t.cls for n, t in self._tools.items()}

    # ---- goal ----------------------------------------------------------------

    def goal(self, text: str):
        """with gate.goal("Clean up build artifacts"): ... gives the scorer and the human the context."""
        gate = self

        class _Ctx:
            def __enter__(self_inner):
                self_inner.token = _goal.set(text)
                return gate

            def __exit__(self_inner, *exc):
                _goal.reset(self_inner.token)
                return False

        return _Ctx()

    # ---- the decision --------------------------------------------------------

    def call(self, name: str, *args, **kwargs) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise RegistryError(f"unknown tool '{name}'")
        bound = inspect.signature(tool.fn).bind(*args, **kwargs)
        bound.apply_defaults()
        return self.run(name, dict(bound.arguments))

    def run(self, name: str, args: Dict[str, Any], must_ask: str = "") -> Any:
        """Like call, but with the arguments already as a dict (e.g. from an MCP tools/call).
        must_ask: ask the human whatever the class, with this as the reason."""
        tool = self._tools.get(name)
        if tool is None:
            raise RegistryError(f"unknown tool '{name}'")
        call_args = dict(args)
        goal = _goal.get()

        key = ""
        if tool.dedupe:
            key = make_key(tool.name, call_args, scope=goal)
            state, saved, err = self.ledger.get(key)
            if state == DONE:
                self._record(tool, goal, call_args, "deduped", "an identical call already succeeded", None, None)
                return saved
            if state == CLAIMED:
                raise Denied(f"{tool.name}: identical call already running")
            if state == UNKNOWN:
                found = self._reconcile(tool, call_args, key)
                if found is not None and found is not _CANT_TELL:
                    self.ledger.done(key, found)
                    self._record(tool, goal, call_args, "deduped", "reconciled: an identical call already happened",
                                 None, None)
                    return found
                if tool.reconcile is None or found is _CANT_TELL:
                    must_ask = (f"an identical call failed with {err or 'an error'} and may already have happened. "
                                f"Check before approving.")

        if must_ask:
            decision, why, conf = "ask", must_ask, None
        else:
            decision, why, conf = self._route(tool, goal, call_args)

        answer: Optional[Answer] = None
        if decision == "ask":
            answer = self.ask(Request(tool.name, tool.cls, dict(call_args), goal, why, conf))
            if answer.approve and answer.args is not None:
                call_args = answer.args
                if tool.dedupe:
                    key = make_key(tool.name, call_args, scope=goal)
        self._record(tool, goal, call_args, decision, why, conf, answer)
        if answer is not None and not answer.approve:
            raise Denied(f"{tool.name}: the user said no" + (f" ({answer.note})" if answer.note else ""))

        if not tool.dedupe:
            return tool.fn(**call_args)
        if not self.ledger.claim(key, tool.name, call_args):
            state, saved, _ = self.ledger.get(key)
            if state == DONE:  # e.g. the human edited the args into a call that had already succeeded
                return saved
            raise Denied(f"{tool.name}: identical call already running")
        try:
            result = tool.fn(**self._with_key(tool, call_args, key))
        except NotExecuted:
            self.ledger.release(key)
            raise
        except BaseException as e:
            self.ledger.unknown(key, repr(e))
            raise
        self.ledger.done(key, result)
        return result

    def _with_key(self, tool: _Tool, args: Dict[str, Any], key: str) -> Dict[str, Any]:
        if tool.takes_key and args.get(KEY_ARG) is None:
            return dict(args, **{KEY_ARG: key})
        return args

    def _reconcile(self, tool: _Tool, args: Dict[str, Any], key: str) -> Any:
        """The result if the earlier call happened, None if it didn't, _CANT_TELL if reconcile failed."""
        if tool.reconcile is None:
            return None
        rargs = dict(args, **{KEY_ARG: key}) if _takes_key(tool.reconcile) and args.get(KEY_ARG) is None else args
        try:
            return tool.reconcile(**rargs)
        except Exception:  # a broken reconcile must never mean "run it again"
            return _CANT_TELL

    def _route(self, tool: _Tool, goal: str, args: Dict[str, Any]):
        if tool.cls is Class.READ:
            return "act", "read-only", None
        if always_asks(tool.cls):
            return "ask", f"{tool.cls.name.lower()} actions always ask", None
        if self.scorer is None:
            return "act", "undoable, no scorer set", None
        try:
            p = float(self.scorer(goal, tool.name, args))
        except Exception as e:  # a broken scorer must never mean "act"
            return "ask", f"scorer failed ({type(e).__name__}), asking to be safe", None
        if p >= self.threshold:
            return "act", f"undoable and {p:.0%} likely fine", p
        return "ask", f"only {p:.0%} likely fine", p

    def _record(self, tool, goal, args, decision, why, conf, answer) -> None:
        if self.log is None:
            return
        self.log.record(ts=time.time(), tool=tool.name, cls=tool.cls.name, goal=goal, args=args,
                        decision=decision, why=why, confidence=conf,
                        approved=None if answer is None else answer.approve,
                        edited=bool(answer and answer.args is not None), note=answer.note if answer else "")


class _CantTell:
    def __repr__(self) -> str:
        return "CANT_TELL"


_CANT_TELL = _CantTell()
