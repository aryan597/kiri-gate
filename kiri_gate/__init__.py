"""kiri-gate: put a gate in front of your agent's tools.

    from kiri_gate import Gate, READ, UNDOABLE, IRREVERSIBLE, EXTERNAL
    gate = Gate()

    @gate.tool(EXTERNAL)
    def send_email(to: str, body: str): ...
"""

from kiri_gate.classes import EXTERNAL, IRREVERSIBLE, READ, UNDOABLE, Class
from kiri_gate.gate import Answer, Denied, Gate, RegistryError, Request
from kiri_gate.log import DecisionLog

__all__ = ["Gate", "Class", "READ", "UNDOABLE", "IRREVERSIBLE", "EXTERNAL",
           "Request", "Answer", "Denied", "RegistryError", "DecisionLog"]
__version__ = "0.1.0"
