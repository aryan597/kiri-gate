"""The gate: every tool call goes through here, and here decides act or ask.

Rules, in order:
  READ          acts.
  IRREVERSIBLE  asks. Always. No score, no setting and no model can change this.
  EXTERNAL      asks. Always.
  UNDOABLE      acts if a scorer says it's likely fine (>= threshold), otherwise asks.
                With no scorer, it acts: undoable means you can take it back.

A tool's class is fixed when it's registered and can't be registered twice.
The agent never holds the real function; it only gets the gated wrapper.
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
from kiri_gate.log import DecisionLog

_goal: contextvars.ContextVar[str] = contextvars.ContextVar("kiri_goal", default="")


class Denied(Exception):
    """Raised inside the agent's call when a human says no. Agents should treat it as a normal tool error."""


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


@dataclass
class Gate:
    ask: Optional[Callable[[Request], Answer]] = None
    scorer: Optional[Callable[[str, str, Dict[str, Any]], float]] = None
    threshold: float = 0.5
    log: Optional[DecisionLog] = None
    _tools: Dict[str, _Tool] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.ask is None:
            from kiri_gate.ask import terminal
            self.ask = terminal
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")

    # ---- registration --------------------------------------------------------

    def tool(self, cls: Class, name: Optional[str] = None, description: str = ""):
        """Decorator: @gate.tool(EXTERNAL) def send_email(...)."""
        if not isinstance(cls, Class):
            raise RegistryError("class must be READ, UNDOABLE, IRREVERSIBLE or EXTERNAL")

        def wrap(fn: Callable) -> Callable:
            tname = name or fn.__name__
            with self._lock:
                if tname in self._tools:
                    raise RegistryError(f"tool '{tname}' is already registered as {self._tools[tname].cls.name}")
                self._tools[tname] = _Tool(tname, cls, fn, description or (inspect.getdoc(fn) or "").split("\n")[0])

            @functools.wraps(fn)
            def gated(*args, **kwargs):
                return self.call(tname, *args, **kwargs)

            gated.kiri_class = cls  # type: ignore[attr-defined]
            return gated

        return wrap

    def register(self, fn: Callable, cls: Class, name: Optional[str] = None, description: str = "") -> Callable:
        return self.tool(cls, name, description)(fn)

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
        call_args = dict(bound.arguments)
        goal = _goal.get()
        decision, why, conf = self._route(tool, goal, call_args)

        answer: Optional[Answer] = None
        if decision == "ask":
            answer = self.ask(Request(tool.name, tool.cls, dict(call_args), goal, why, conf))
            if answer.approve and answer.args is not None:
                call_args = answer.args
        self._record(tool, goal, call_args, decision, why, conf, answer)
        if answer is not None and not answer.approve:
            raise Denied(f"{tool.name}: the user said no" + (f" ({answer.note})" if answer.note else ""))
        return tool.fn(**call_args)

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
