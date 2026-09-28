"""Duplicate charges in a real LangGraph agent, against Stripe TEST MODE.

The charge tool creates and confirms a real PaymentIntent (test card pm_card_visa), then the
response is "lost": the tool raises stripe's APIConnectionError, which is what the SDK raises on a
timeout. The charge already happened. We then count charges in Stripe itself.

Graph: a hand-built LangGraph StateGraph (agent node -> ToolNode -> agent), nothing mocked but the fault.

Conditions
  graph_retry      tools node added with LangGraph's default RetryPolicy()        (framework retries)
  model_decides    ToolNode(handle_tool_errors=True): error goes to the model, no retry
  key_call_id      graph_retry + Stripe Idempotency-Key = tool_call_id            (fix 3 on langchain#40688)
  key_args         graph_retry + Idempotency-Key = hash(run, tool, args)          (the idea behind kiri-gate)
  kiri_gate        graph_retry + charge wrapped in kiri-gate's Gate (EXTERNAL) with a reconcile that looks
                   the charge up by its key in Stripe metadata. No Stripe Idempotency-Key: the dedupe is
                   kiri-gate's. The simulated human approves the first charge and denies anything after.

Usage (PowerShell):  $env:STRIPE_API_KEY="sk_test_..."
  python stripe_repro.py --scripted                 # no LLM: the graph alone, deterministic
  python stripe_repro.py --model qwen/qwen3.5-9b    # LM Studio at 127.0.0.1:1234
  python stripe_repro.py --scripted --fake-stripe   # offline, in-memory Stripe (for tests)
Refuses to run with anything other than a test-mode key.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Annotated

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import RetryPolicy

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))  # kiri_gate from this repo
from kiri_gate import EXTERNAL, Answer, Gate  # noqa: E402

CONDITIONS = ["graph_retry", "model_decides", "key_call_id", "key_args", "kiri_gate"]
GOAL = "Charge customer Anil (order o988) 12.00 GBP for the replacement lamp. Charge exactly once."
SYSTEM = "You are a payments assistant. Use the tools to do what the user asks, then reply with a short final message."


# ------------------------------------------------------------------ Stripe (real or fake)
class ConnError(Exception):
    """Stand-in for stripe.error.APIConnectionError in fake mode."""


class FakeStripe:
    """In-memory Stripe with the same idempotency semantics (same key -> same object, no new charge)."""

    def __init__(self):
        self.intents, self.keys = [], {}
        self.APIConnectionError = ConnError

    def charge(self, amount, currency, run, key=None, kiri_key=None):
        if key and key in self.keys:
            return self.keys[key]
        md = {"kiri_run": run, **({"kiri_key": kiri_key} if kiri_key else {})}
        pi = {"id": f"pi_fake_{len(self.intents)}", "amount": amount, "status": "succeeded", "metadata": md}
        self.intents.append(pi)
        if key:
            self.keys[key] = pi
        return pi

    def find(self, run, kiri_key, since):
        return next((p for p in self.intents if p["metadata"].get("kiri_key") == kiri_key
                     and p["metadata"]["kiri_run"] == run and p["status"] == "succeeded"), None)

    def count(self, run, since):
        return sum(1 for p in self.intents if p["metadata"]["kiri_run"] == run and p["status"] == "succeeded")


class RealStripe:
    def __init__(self):
        import stripe

        key = os.environ.get("STRIPE_API_KEY", "").strip().strip('"')
        if not key.startswith(("sk_test_", "rk_test_")):
            sys.exit("Set STRIPE_API_KEY to a TEST-mode key (sk_test_...). Refusing to run otherwise.")
        stripe.api_key = key
        stripe.max_network_retries = 0  # we inject the fault ourselves; keep the SDK out of it
        self.s = stripe
        self.APIConnectionError = getattr(stripe, "APIConnectionError", None) or stripe.error.APIConnectionError
        try:  # preflight: key valid, network reachable, test mode
            bal = stripe.Balance.retrieve()
            if getattr(bal, "livemode", False):
                sys.exit("This key is LIVE mode. Refusing.")
        except Exception as e:
            sys.exit(f"Can't use Stripe with this key: {type(e).__name__}: {e}\n"
                     f"(stripe-python {getattr(stripe, 'VERSION', getattr(stripe, '_version', '?'))}). "
                     "Check the key is the Secret key from Developers > API keys with Test mode on.")
        print(f"Stripe test mode OK (stripe-python {getattr(stripe, 'VERSION', '?')})")

    def charge(self, amount, currency, run, key=None, kiri_key=None):
        kw = {"idempotency_key": key} if key else {}
        md = {"kiri_run": run, **({"kiri_key": kiri_key} if kiri_key else {})}
        pi = self.s.PaymentIntent.create(
            amount=amount, currency=currency, payment_method="pm_card_visa", confirm=True,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            metadata=md, description="kiri chaos test", **kw)
        return {"id": pi.id, "status": pi.status}

    def find(self, run, kiri_key, since):
        # list, not search: search is eventually consistent and can miss a charge made seconds ago
        for pi in self.s.PaymentIntent.list(created={"gte": since - 5}, limit=100).auto_paging_iter():
            md = pi.metadata.to_dict() if hasattr(pi.metadata, "to_dict") else dict(pi.metadata or {})
            if md.get("kiri_run") == run and md.get("kiri_key") == kiri_key and pi.status == "succeeded":
                return {"id": pi.id, "status": pi.status}
        return None

    def count(self, run, since):
        n = 0
        for pi in self.s.PaymentIntent.list(created={"gte": since - 5}, limit=100).auto_paging_iter():
            md = pi.metadata.to_dict() if hasattr(pi.metadata, "to_dict") else dict(pi.metadata or {})
            if md.get("kiri_run") == run and pi.status == "succeeded":
                n += 1
        return n


# ------------------------------------------------------------------ the agent
def kiri_charge(stripe_api, run, since, state):
    """The charge as a kiri-gate tool: EXTERNAL, key passed in by the gate, reconciled against Stripe."""

    def approve_once(req):  # the user asked for this charge once; anything after that gets a no
        state["asks"] += 1
        return Answer(state["asks"] == 1, note="" if state["asks"] == 1 else "auto-deny")

    gate = Gate(ask=approve_once)

    def reconcile(customer, order_id, amount_gbp, idempotency_key):
        pi = stripe_api.find(run, idempotency_key, since)
        return None if pi is None else f"Charged {amount_gbp:.2f} GBP: {pi['id']} ({pi['status']})"

    @gate.tool(EXTERNAL, reconcile=reconcile)
    def charge(customer: str, order_id: str, amount_gbp: float, idempotency_key: str = None) -> str:
        state["calls"] += 1
        pi = stripe_api.charge(int(round(amount_gbp * 100)), "gbp", run, kiri_key=idempotency_key)
        if state["calls"] == 1:  # first call: the charge happened, the response is lost
            raise stripe_api.APIConnectionError("Request to Stripe timed out (read timeout)")
        return f"Charged {amount_gbp:.2f} GBP: {pi['id']} ({pi['status']})"

    def call(customer, order_id, amount_gbp):
        with gate.goal(run):
            return charge(customer, order_id, round(amount_gbp, 2))

    return call


def build(stripe_api, condition, run, model, since=0):
    state = {"calls": 0, "asks": 0}
    gated = kiri_charge(stripe_api, run, since, state) if condition == "kiri_gate" else None

    @tool
    def charge_card(customer: str, order_id: str, amount_gbp: float,
                    tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        """Charge the customer's saved card. amount_gbp is in pounds, e.g. 12.00."""
        if gated is not None:
            return gated(customer, order_id, amount_gbp)
        state["calls"] += 1
        key = None
        if condition == "key_call_id":
            key = f"kiri-{run}-{tool_call_id}"
        elif condition == "key_args":
            key = "kiri-" + hashlib.sha256(json.dumps([run, "charge_card", order_id, round(amount_gbp, 2)]).encode()).hexdigest()[:32]
        pi = stripe_api.charge(int(round(amount_gbp * 100)), "gbp", run, key)
        if state["calls"] == 1:  # first call: the charge happened, the response is lost
            raise stripe_api.APIConnectionError("Request to Stripe timed out (read timeout)")
        return f"Charged {amount_gbp:.2f} GBP: {pi['id']} ({pi['status']})"

    @tool
    def list_charges(order_id: str) -> str:
        """List charges already made for an order."""
        return json.dumps({"order_id": order_id, "successful_charges": stripe_api.count(run, since)})

    tools = [charge_card, list_charges]
    bound = model.bind_tools(tools)

    def agent(s: MessagesState):
        return {"messages": [bound.invoke([SystemMessage(SYSTEM)] + s["messages"])]}

    g = StateGraph(MessagesState)
    g.add_node("agent", agent)
    if condition == "model_decides":
        g.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    else:
        g.add_node("tools", ToolNode(tools, handle_tool_errors=False), retry_policy=RetryPolicy(initial_interval=0.05, jitter=False))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile(), state


def scripted_model():
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    class M(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            return self

    return M(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "charge_card", "id": "call_1",
                                           "args": {"customer": "Anil", "order_id": "o988", "amount_gbp": 12.0}}]),
        AIMessage(content="Done."),
    ]))


def real_model(name, base):
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=name, base_url=base, api_key="lm-studio", temperature=0.7, timeout=240,
                      extra_body={"chat_template_kwargs": {"enable_thinking": False}})


def episode(stripe_api, condition, model_name, base):
    run = uuid.uuid4().hex[:12]
    since = int(time.time())
    model = scripted_model() if model_name is None else real_model(model_name, base)
    app, st = build(stripe_api, condition, run, model, since)
    err, final = "", ""
    try:
        out = app.invoke({"messages": [HumanMessage(GOAL)]}, {"recursion_limit": 20})
        final = next((m.content for m in reversed(out["messages"]) if isinstance(m, AIMessage) and not m.tool_calls), "")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:160]
    time.sleep(0 if isinstance(stripe_api, FakeStripe) else 1.5)
    n = stripe_api.count(run, since)
    outcome = f"CRASHED ({n} charged)" if err and "Recursion" not in err else (
        "DUPLICATE" if n >= 2 else "CORRECT" if n == 1 else "NOT CHARGED")
    return {"condition": condition, "run_id": run, "charges": n, "tool_executions": st["calls"], "asks": st["asks"],
            "outcome": outcome, "final": str(final)[:200], "error": err}


# ------------------------------------------------------------------ run + report
def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--scripted", action="store_true")
    p.add_argument("--model")
    p.add_argument("--base", default="http://127.0.0.1:1234/v1")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--fake-stripe", action="store_true")
    p.add_argument("--conditions", default=",".join(CONDITIONS))
    a = p.parse_args(argv)
    if not a.scripted and not a.model:
        sys.exit("pass --scripted or --model")
    stripe_api = FakeStripe() if a.fake_stripe else RealStripe()
    tag = ("scripted" if a.scripted else a.model.replace("/", "_")) + ("_fake" if a.fake_stripe else "")
    out = HERE / "results" / "stripe" / tag
    out.mkdir(parents=True, exist_ok=True)
    path = out / "episodes.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
    done = Counter(r["condition"] for r in rows)
    reps = 1 if a.scripted else a.runs
    import traceback
    with path.open("a") as f:
        for cond in a.conditions.split(","):
            for i in range(done[cond], reps):
                try:
                    r = episode(stripe_api, cond, None if a.scripted else a.model, a.base)
                except Exception as e:
                    with (out / "errors.log").open("a") as ef:
                        ef.write(f"--- {cond} run {i}\n{traceback.format_exc()}\n")
                    print(f"   {cond:14} run {i}: ERROR {type(e).__name__}: {str(e)[:150]} (full trace in errors.log)", flush=True)
                    continue
                f.write(json.dumps(r) + "\n"); f.flush()
                rows.append(r)
                flag = "!!" if r["charges"] >= 2 else "  "
                print(f"{flag} {cond:14} run {i}: {r['outcome']:20} charges={r['charges']} executions={r['tool_executions']}", flush=True)
    report(rows, out, tag)
    return rows


def report(rows, out, tag):
    by = defaultdict(list)
    for r in rows:
        by[r["condition"]].append(r)
    L = [f"# Stripe test-mode duplicate charges: {tag}", "",
         "LangGraph StateGraph + ToolNode. The first charge succeeds in Stripe, then the tool raises APIConnectionError.",
         "Charges are counted from Stripe's own PaymentIntent list.", "",
         "| condition | runs | duplicate charges | charges per run | outcomes |", "|---|---|---|---|---|"]
    for c in CONDITIONS:
        g = by.get(c, [])
        if not g:
            continue
        oc = Counter(r["outcome"] for r in g)
        L.append(f"| {c} | {len(g)} | {sum(r['charges'] >= 2 for r in g)}/{len(g)} | "
                 f"{', '.join(str(r['charges']) for r in g)} | {', '.join(f'{k} {v}' for k, v in oc.most_common())} |")
    (out / "REPORT.md").write_text("\n".join(L) + "\n")
    print(f"\nwrote {out / 'REPORT.md'}")


if __name__ == "__main__":
    main()
