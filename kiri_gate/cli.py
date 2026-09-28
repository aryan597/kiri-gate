"""kiri-gate mcp [options] -- <server command...>"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from kiri_gate.log import DecisionLog

HOME = os.path.join(os.path.expanduser("~"), ".kiri")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]

    p = argparse.ArgumentParser(prog="kiri-gate")
    sub = p.add_subparsers(dest="command", required=True)
    m = sub.add_parser("mcp", help="put the gate in front of a stdio MCP server")
    m.add_argument("--config", default="kiri.toml", help="tool classes (default: ./kiri.toml)")
    m.add_argument("--log", default=os.path.join(HOME, "kiri.db"),
                   help="decision log, SQLite, shared by every server (default: ~/.kiri/kiri.db)")
    m.add_argument("--ledger", default="",
                   help="what already ran, to stop duplicates, SQLite (default: the same file as --log)")
    m.add_argument("--port", type=int, default=8766, help="approval page port (default: 8766)")
    m.add_argument("--ask-timeout", type=float, default=55.0,
                   help="seconds to wait for you before denying (default: 55; Claude Desktop gives up at 60)")
    m.add_argument("--name", default="", help="label for this server on the approval page")
    m.add_argument("--no-open", action="store_true", help="don't open the approval page in a browser")
    a = p.parse_args(argv)

    if a.command == "mcp":
        if not cmd:
            p.error("give the server command after --, e.g. kiri-gate mcp -- npx @some/mcp-server")
        if a.ask_timeout <= 0:
            p.error("--ask-timeout must be above 0")
        from kiri_gate.inbox import Approver
        from kiri_gate.ledger import Ledger
        from kiri_gate.mcp import Proxy, _note
        os.makedirs(os.path.dirname(os.path.abspath(a.log)), exist_ok=True)
        approver = Approver(port=a.port, log_path=os.path.abspath(a.log), open_browser=not a.no_open, notify=_note)
        return Proxy(cmd, config_path=a.config, log=DecisionLog(a.log), approver=approver,
                     ask_timeout=a.ask_timeout, name=a.name, ledger=Ledger(a.ledger or a.log)).run()
    return 2


if __name__ == "__main__":
    sys.exit(main())
