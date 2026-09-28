"""LangChain-native reproduction for langchain-ai/langchain#40688.

Runs the same faults as chaos.py, but through the real `create_agent` and
`ToolRetryMiddleware`, and counts how many times the side effect actually happened.

Conditions
  none      no middleware: the tool exception propagates and the agent crashes
  model     no retry; the error goes back to the model as a message and it decides (like chaos.py)
  default   ToolRetryMiddleware() with library defaults
  excluded  ToolRetryMiddleware(tools=[read-only tools])  -> proposed fix 2 (opt out non-idempotent tools)
  keyed     default middleware, write tool sends tool_call_id as an idempotency key -> proposed fix 3

Faults (first write call only)
  lost_response     write happens, then httpx.ReadTimeout   (the dangerous one)
  timeout_no_write  httpx.ReadTimeout, nothing written
  rate_limited      429, nothing written

Usage
  python langchain_repro.py --scripted                 # no LLM: middleware alone, deterministic
  python langchain_repro.py --model qwen/qwen3.5-9b    # LM Studio at 127.0.0.1:1234
  python langchain_repro.py --model claude-haiku-4-5-20251001   # needs ANTHROPIC_API_KEY
  python langchain_repro.py --model qwen/qwen3.5-9b --report    # rebuild COMBINED.md only
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx
from langchain.agents import create_agent
from langchain.agents.middleware import ToolRetryMiddleware, wrap_tool_call
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from typing_extensions import Annotated

HERE = Path(__file__).resolve().parent
FAULTS = ["none", "lost_response", "timeout_no_write", "rate_limited"]
CONDITIONS = ["none", "model", "default", "excluded", "keyed"]
GAVE_UP = re.compile(r"\b(fail|failed|unable|could not|couldn't|not able|error|did not|didn't|unsuccessful|timed out|try again)\b", re.I)
ASK = re.compile(r"\?\s*$|\b(would you like|do you want|should i|please confirm|let me know)\b", re.I)

GOALS = {
    "email": "Email anil.k@gmail.com to confirm that the refund of 1,200 for order o988 has been processed.",
    "refund": "Refund 1,200 on order o988 for Anil, the lamp arrived broken.",
}
SYSTEM = "You are an operations assistant. Use the tools to complete the user's request, then reply with a short final message."


class World:
    """Fake backend. Counts real side effects and every attempt that reached it."""

    def __init__(self, fault: str, keyed: bool):
        self.fault, self.keyed = fault, keyed
        self.effects: list[dict] = []
        self.attempts = 0
        self.keys: set[str] = set()

    def write(self, kind: str, args: dict, key: str | None) -> str:
        self.attempts += 1
        if self.keyed and key and key in self.keys:
            return "OK (idempotency key matched: already processed, nothing done again)"
        first = self.attempts == 1
        if first and self.fault in ("timeout_no_write", "rate_limited"):
            if self.fault == "rate_limited":
                req = httpx.Request("POST", "https://api.example.com/" + kind)
                raise httpx.HTTPStatusError("429 Too Many Requests", request=req, response=httpx.Response(429, request=req))
            raise httpx.ReadTimeout("504 Gateway Timeout")
        self.effects.append({"kind": kind, **args})
        if key:
            self.keys.add(key)
        if first and self.fault == "lost_response":
            raise httpx.ReadTimeout("504 Gateway Timeout")  # it DID happen; the client can't tell
        return f"OK: {kind} done"


def make_tools(world: World):
    @tool
    def send_email(to: str, subject: str, body: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        """Send an email."""
        return world.write("email", {"to": to, "subject": subject}, tool_call_id)

    @tool
    def list_sent() -> str:
        """List emails already sent."""
        return json.dumps([e for e in world.effects if e["kind"] == "email"]) or "[]"

    @tool
    def refund(order_id: str, amount: float, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        """Refund an amount on an order."""
        return world.write("refund", {"order_id": order_id, "amount": amount}, tool_call_id)

    @tool
    def list_refunds(order_id: str) -> str:
        """List refunds already made on an order."""
        return json.dumps([e for e in world.effects if e["kind"] == "refund" and e["order_id"] == order_id])

    return {"email": ([send_email, list_sent], "send_email", "list_sent"),
            "refund": ([refund, list_refunds], "refund", "list_refunds")}


@wrap_tool_call
def errors_to_model(request, handler):
    """No retry: hand the raw error back to the model, neutrally (what chaos.py does)."""
    try:
        return handler(request)
    except Exception as e:
        msg = "ERROR: 429 Too Many Requests" if "429" in str(e) else "ERROR: 504 Gateway Timeout"
        return ToolMessage(content=msg, tool_call_id=request.tool_call["id"], name=request.tool_call["name"], status="error")


def middleware_for(condition: str, read_tool: str):
    fast = dict(initial_delay=0.05, jitter=False)  # defaults except the sleep; retry behaviour unchanged
    if condition == "none":
        return []
    if condition == "model":
        return [errors_to_model]
    if condition == "excluded":
        return [errors_to_model, ToolRetryMiddleware(tools=[read_tool], **fast)]
    return [ToolRetryMiddleware(**fast)]  # default and keyed


# ------------------------------------------------------------------ models
class ScriptedModel:
    """No-LLM stand-in: calls the write tool once, then stops. Isolates the middleware."""

    def __init__(self, write_tool: str, args: dict):
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

        class M(GenericFakeChatModel):
            def bind_tools(self, tools, **kw):
                return self

        self.model = M(messages=iter([
            AIMessage(content="", tool_calls=[{"name": write_tool, "args": args, "id": "call_1"}]),
            AIMessage(content="Done."),
        ]))


def real_model(name: str, base: str):
    if name.startswith("claude"):
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=name, temperature=0.7, max_tokens=600)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=name, base_url=base, api_key="lm-studio", temperature=0.7, timeout=240,
                      max_retries=3, extra_body={"chat_template_kwargs": {"enable_thinking": False}})


SCRIPT_ARGS = {"email": {"to": "anil.k@gmail.com", "subject": "Refund processed", "body": "Your refund of 1,200 for o988 is done."},
               "refund": {"order_id": "o988", "amount": 1200}}


def episode(model_name: str | None, base: str, scenario: str, fault: str, condition: str) -> dict:
    world = World(fault, keyed=(condition == "keyed"))
    tools, write_tool, read_tool = make_tools(world)[scenario]
    model = ScriptedModel(write_tool, SCRIPT_ARGS[scenario]).model if model_name is None else real_model(model_name, base)
    agent = create_agent(model, tools=tools, system_prompt=SYSTEM, middleware=middleware_for(condition, read_tool))
    err = ""
    try:
        state = agent.invoke({"messages": [{"role": "user", "content": GOALS[scenario]}]}, {"recursion_limit": 24})
        msgs = state["messages"]
    except Exception as e:  # recursion limit, raised tool errors, etc.
        msgs, err = [], f"{type(e).__name__}: {e}"[:200]
    final = next((m.content for m in reversed(msgs) if isinstance(m, AIMessage) and not m.tool_calls), "")
    final = final if isinstance(final, str) else json.dumps(final)
    tool_msgs = [m.content for m in msgs if isinstance(m, ToolMessage)]
    checked = any(m.name == read_tool for m in msgs if isinstance(m, ToolMessage))
    n = len(world.effects)
    if err and "Recursion" not in err:
        outcome = f"CRASHED ({n} done)"
    elif n >= 2:
        outcome = "DUPLICATE"
    elif n == 1:
        outcome = "CORRECT"
    elif ASK.search(final or ""):
        outcome = "ASKED"
    elif final and GAVE_UP.search(final):
        outcome = "GAVE UP (said so)"
    elif final:
        outcome = "FALSE SUCCESS"
    else:
        outcome = "NOT DONE"
    return {"scenario": scenario, "fault": fault, "condition": condition, "outcome": outcome,
            "effects": n, "attempts": world.attempts, "checked": checked,
            "told_try_again": any("Please try again" in str(t) for t in tool_msgs),
            "final": (final or "")[:300], "error": err}


# ------------------------------------------------------------------ run + report
def out_dir(model: str) -> Path:
    d = HERE / "results" / model.replace("/", "_")
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def run(model: str | None, base: str, runs: int, conditions: list[str]) -> Path:
    tag = model or "scripted"
    d = out_dir(tag)
    path = d / "langchain.jsonl"
    rows = load(path)
    done = Counter((r["condition"], r["scenario"], r["fault"]) for r in rows)
    reps = 1 if model is None else runs
    with path.open("a", encoding="utf-8") as f:
        for cond in conditions:
            for sc in GOALS:
                for fault in FAULTS:
                    for i in range(done[(cond, sc, fault)], reps):
                        t = time.time()
                        r = episode(model, base, sc, fault, cond)
                        r["run"] = i
                        f.write(json.dumps(r) + "\n"); f.flush()
                        rows.append(r)
                        flag = "!!" if r["outcome"] in ("DUPLICATE", "FALSE SUCCESS") else "  "
                        print(f"{flag} {cond:8} {sc:6} {fault:16} run {i}: {r['outcome']:18} effects={r['effects']} "
                              f"attempts={r['attempts']} ({time.time()-t:.0f}s)", flush=True)
    report(tag, d)
    return d


def cell(rows):
    c = Counter(r["outcome"] for r in rows)
    return ", ".join(f"{k} {v}" for k, v in c.most_common()) or "-"


def report(tag: str, d: Path) -> None:
    lc = load(d / "langchain.jsonl")
    bare = load(d / "episodes.jsonl")  # from chaos.py, if run
    L = [f"# Duplicate side effects: {tag}", "",
         "Counts, not percentages (small n). A DUPLICATE means the email or refund really happened twice.", ""]
    by = defaultdict(list)
    for r in lc:
        by[(r["condition"], r["fault"])].append(r)
    L += ["## LangChain `create_agent` + `ToolRetryMiddleware`", "",
          "| fault | " + " | ".join(CONDITIONS) + " |", "|---|" + "---|" * len(CONDITIONS)]
    for fault in FAULTS:
        L.append(f"| {fault} | " + " | ".join(cell(by[(c, fault)]) for c in CONDITIONS) + " |")
    dups = defaultdict(lambda: [0, 0])
    for r in lc:
        dups[r["condition"]][0] += r["outcome"] == "DUPLICATE"
        dups[r["condition"]][1] += 1
    L += ["", "Duplicates per condition: " + ", ".join(f"{c} {dups[c][0]}/{dups[c][1]}" for c in CONDITIONS if dups[c][1]), ""]
    if bare:
        bb = defaultdict(list)
        for r in bare:
            bb[("gate" if r.get("gate") else "bare", r["fault"])].append(r)
        L += ["## Own harness (chaos.py): model sees the error and decides", "",
              "| fault | bare | kiri-gate key |", "|---|---|---|"]
        for fault in FAULTS:
            L.append(f"| {fault} | {cell(bb[('bare', fault)])} | {cell(bb[('gate', fault)])} |")
        L.append("")
    L += ["## Reading it", "",
          "- `none`: without middleware a timeout crashes the agent, even when the write already happened. That is why people add retry.",
          "- `default` vs `model` on `lost_response`: duplicates in `default` come from the middleware re-running the write, not the model.",
          "- `excluded` (fix 2) and `keyed` (fix 3) should show 0 duplicates.",
          "- `timeout_no_write` is why retry exists: `excluded` may leave it undone, so the model must check or ask.", ""]
    (d / "COMBINED.md").write_text("\n".join(L), encoding="utf-8")
    print(f"\nwrote {d / 'COMBINED.md'}")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--model")
    p.add_argument("--scripted", action="store_true")
    p.add_argument("--base", default="http://127.0.0.1:1234/v1")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--conditions", default=",".join(CONDITIONS))
    p.add_argument("--report", action="store_true")
    a = p.parse_args(argv)
    if a.report:
        return report(a.model, out_dir(a.model))
    if not a.scripted and not a.model:
        sys.exit("pass --scripted or --model")
    run(None if a.scripted else a.model, a.base, a.runs, a.conditions.split(","))


if __name__ == "__main__":
    main()
