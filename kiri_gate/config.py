"""kiri.toml: which class each MCP tool is in.

    [tools]            # confirmed by you. These are the only classes kiri-gate trusts.
    read_file = "read"
    send_email = "external"

    [unconfirmed]      # suggested from the server's annotations. Still treated as IRREVERSIBLE.
    delete_file = "irreversible"   # destructiveHint

Anything not under [tools] is IRREVERSIBLE: it always asks until you move it there.
Annotations come from the server, so they are only ever suggestions.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from kiri_gate.classes import IRREVERSIBLE, Class, parse

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class Config:
    tools: Dict[str, Class] = field(default_factory=dict)
    unconfirmed: Dict[str, str] = field(default_factory=dict)

    def class_for(self, tool: str) -> Class:
        return self.tools.get(tool, IRREVERSIBLE)


def load(path: str) -> Config:
    if not os.path.exists(path):
        return Config()
    with open(path, "rb") as f:
        raw = f.read().decode("utf-8-sig")
    data = _parse(raw)
    tools = {}
    for name, cls in (data.get("tools") or {}).items():
        try:
            tools[name] = parse(cls)
        except ValueError as e:
            raise ValueError(f"{path}: [tools] {name}: {e}") from None
    return Config(tools, {k: str(v) for k, v in (data.get("unconfirmed") or {}).items()})


def suggest(tool: Dict[str, Any]) -> Tuple[str, str]:
    """(class, reason) from MCP annotations. Only explicit hints count; a missing hint means irreversible."""
    a = tool.get("annotations") or {}
    if a.get("readOnlyHint") is True:
        return "read", "readOnlyHint"
    if a.get("openWorldHint") is True:
        return "external", "openWorldHint"
    if a.get("destructiveHint") is False:
        return "undoable", "destructiveHint=false"
    if a.get("destructiveHint") is True:
        return "irreversible", "destructiveHint"
    return "irreversible", "no annotations"


def draft(tools: List[Dict[str, Any]], confirmed: Optional[Dict[str, Class]] = None) -> str:
    confirmed = confirmed or {}
    out = [
        "# kiri-gate tool classes. Written from the server's tool list.",
        "# Move a line from [unconfirmed] to [tools] once you agree with its class.",
        "# Classes: read, undoable, irreversible, external. Anything not under [tools] always asks.",
        "",
        "[tools]",
    ]
    out += [f'{_key(n)} = "{c.value}"' for n, c in confirmed.items()]
    out += ["", "[unconfirmed]"]
    for t in tools:
        name = t.get("name")
        if not isinstance(name, str) or name in confirmed:
            continue
        cls, why = suggest(t)
        out.append(f'{_key(name)} = "{cls}"  # {why}')
    return "\n".join(out) + "\n"


def _key(name: str) -> str:
    return name if _BARE.match(name) else json.dumps(name)


def _parse(text: str) -> Dict[str, Dict[str, Any]]:
    try:
        import tomllib  # Python 3.11+
        return tomllib.loads(text)
    except ImportError:
        return _mini_toml(text)


def _mini_toml(text: str) -> Dict[str, Dict[str, Any]]:
    """Just enough TOML for kiri.toml: [section] headers and key = "string" lines."""
    data: Dict[str, Dict[str, Any]] = {}
    section = None
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^\[\s*([A-Za-z0-9_-]+)\s*\]\s*(#.*)?$", s)
        if m:
            section = data.setdefault(m.group(1), {})
            continue
        m = re.match(r'^("(?:[^"\\]|\\.)*"|[A-Za-z0-9_-]+)\s*=\s*("(?:[^"\\]|\\.)*")\s*(#.*)?$', s)
        if not m or section is None:
            raise ValueError(f"kiri.toml line {n}: can't read {s!r}")
        key = json.loads(m.group(1)) if m.group(1).startswith('"') else m.group(1)
        section[key] = json.loads(m.group(2))
    return data
