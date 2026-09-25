"""python examples/quickstart.py  (no API key needed)"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kiri_gate import EXTERNAL, IRREVERSIBLE, READ, UNDOABLE, Denied, Gate

gate = Gate()  # asks in the terminal; add scorer=LocalScorer(...) to let a model decide undoable edits


@gate.tool(READ)
def read_file(path: str) -> str:
    return f"(contents of {path})"


@gate.tool(UNDOABLE)
def edit_file(path: str, text: str) -> None:
    print(f"edited {path}")


@gate.tool(IRREVERSIBLE)
def delete_folder(path: str) -> None:
    print(f"deleted {path}")


@gate.tool(EXTERNAL)
def send_email(to: str, body: str) -> None:
    print(f"sent to {to}")


with gate.goal("Reply to the recruiter saying I'm interested"):
    print(read_file("inbox/recruiter.txt"))         # acts
    edit_file("drafts/reply.txt", "Hi, I'm keen")  # acts (undoable, no scorer)
    try:
        send_email("recruiter@company.com", "Hi, I'm keen")  # always asks
    except Denied as e:
        print(e)
