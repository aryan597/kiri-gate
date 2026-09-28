"""A tiny stdio MCP server for tests. No network, no dependencies.

Every tools/call that actually runs is appended to the file in $FAKE_MCP_CALLS (one JSON per line),
so tests can check a denied call never reached the server. A call with "fail": true in its arguments
still runs, then answers with isError.
"""

import json
import os
import sys

TOOLS = [
    {"name": "read_file", "description": "Read a file", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    {"name": "write_draft", "description": "Write a draft", "annotations": {"destructiveHint": False, "openWorldHint": False},
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "delete_file", "description": "Delete a file", "annotations": {"destructiveHint": True},
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    {"name": "send_email", "description": "Send an email", "annotations": {"openWorldHint": True},
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}}},
    {"name": "mystery", "description": "No annotations at all",
     "inputSchema": {"type": "object", "properties": {}}},
]


def reply(mid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            reply(mid, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "0"}})
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            p = msg["params"]
            if os.environ.get("FAKE_MCP_CALLS"):
                with open(os.environ["FAKE_MCP_CALLS"], "a") as f:
                    f.write(json.dumps(p) + "\n")
            text = f"ran {p['name']} {json.dumps(p.get('arguments', {}), sort_keys=True)}"
            if (p.get("arguments") or {}).get("fail"):  # it ran, then reported an error: the effect may be real
                reply(mid, {"content": [{"type": "text", "text": "upstream timed out"}], "isError": True})
            else:
                reply(mid, {"content": [{"type": "text", "text": text}]})
        elif method == "ping":
            reply(mid, {})
        elif mid is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                                         "error": {"code": -32601, "message": f"no method {method}"}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
