"""Ways to ask the human. Anything that takes a Request and returns an Answer works."""

from __future__ import annotations

import json

from kiri_gate.gate import Answer, Request


def terminal(req: Request) -> Answer:
    print("\n" + "-" * 60)
    print(f"Kiri: the agent wants to run {req.tool} ({req.cls.name})")
    if req.goal:
        print(f"Goal:  {req.goal}")
    print(f"Args:  {json.dumps(req.args, default=str)[:600]}")
    print(f"Why asking: {req.why}")
    while True:
        a = input("[y] allow  [n] deny  [e] edit args > ").strip().lower()
        if a in ("y", "yes"):
            return Answer(True)
        if a in ("n", "no", ""):
            note = input("Reason (optional) > ").strip()
            return Answer(False, note=note)
        if a == "e":
            raw = input("New args as JSON > ")
            try:
                return Answer(True, args=json.loads(raw), note="edited")
            except json.JSONDecodeError:
                print("That wasn't valid JSON.")


def always(approve: bool):
    """For tests and CI: answer every question the same way."""
    return lambda req: Answer(approve)
