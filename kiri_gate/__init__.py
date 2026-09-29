"""kiri-gate: put a gate in front of your agent's tools.

    from kiri_gate import Gate, READ, UNDOABLE, IRREVERSIBLE, EXTERNAL
    gate = Gate()

    @gate.tool(EXTERNAL)
    def send_email(to: str, body: str): ...
"""

from kiri_gate.classes import EXTERNAL, IRREVERSIBLE, READ, UNDOABLE, Class
from kiri_gate.gate import Answer, Denied, Gate, NotExecuted, RegistryError, Request
from kiri_gate.ledger import Ledger, make_key
from kiri_gate.log import DecisionLog

__all__ = ["Gate", "Class", "READ", "UNDOABLE", "IRREVERSIBLE", "EXTERNAL",
           "Request", "Answer", "Denied", "NotExecuted", "RegistryError", "DecisionLog", "Ledger", "make_key"]
__version__ = "0.2.1"
