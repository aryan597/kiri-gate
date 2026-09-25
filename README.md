# kiri-gate

**A gate in front of your AI agent's tools. It acts on what can be undone and asks about what can't.**

In the [ask-or-act benchmark](https://github.com/aryan597/ask-or-act), four models from a local 9B up to Claude Opus 5 each let **4 to 6 unrecoverable actions** through when trusted to decide alone, like sending a recruiter email unasked or paying a bill. This rule took that to **zero on every model**, for about 21 extra questions per 120 actions. kiri-gate is that rule as a library.

```python
from kiri_gate import Gate, READ, UNDOABLE, IRREVERSIBLE, EXTERNAL

gate = Gate()   # asks in the terminal by default

@gate.tool(EXTERNAL)
def send_email(to: str, body: str): ...

@gate.tool(UNDOABLE)
def edit_file(path: str, text: str): ...

with gate.goal("Reply to the recruiter"):
    edit_file("reply.txt", "Hi")          # runs
    send_email("r@company.com", "Hi")     # stops and asks you first
```

## Install

```
pip install git+https://github.com/aryan597/kiri-gate
```

Not on PyPI yet. Or clone it and run `python examples/quickstart.py` to see it ask you in the terminal.

## Ways to use it

1. **Wrap your own tools** with `@gate.tool(CLASS)`, as above. This works with any agent loop that calls Python functions.
2. **Let a model decide undoable edits** with a scorer (below). Anything irreversible or external still asks you.
3. **Plug in your own approval**, such as Slack, a web page or your phone, through `ask=`.
4. **In front of MCP servers**: coming in v0.2. One command will put the gate in front of any MCP server for Claude Desktop, Cursor or Claude Code.

## The rule

| Class | Examples | What happens |
|---|---|---|
| `READ` | read, search, query | runs |
| `UNDOABLE` | edit a file, draft, change a setting | runs if the scorer says it's likely fine, asks if not |
| `IRREVERSIBLE` | delete, run a command | **always asks** |
| `EXTERNAL` | send, pay, publish, deploy | **always asks** |

- **You declare the class** when you register the tool. It can't be changed or registered twice. Models misjudge tool risk about 40% of the time, so the model never gets to decide the class.
- **No model can talk its way past** `IRREVERSIBLE` or `EXTERNAL`. The score is never even consulted.
- **A broken or down scorer means ask**, never act.
- **Denied calls raise `Denied`**. Agents see it as a normal tool error and can move on.
- **Every decision is logged** (`DecisionLog`, SQLite): confidence, what you decided, and whether you edited the args. That's the data for checking the model's honesty and, later, learning where your line is.

## Letting a model decide undoable edits

```python
from kiri_gate.scorers import LocalScorer, ClaudeScorer
gate = Gate(scorer=LocalScorer("qwen/qwen3.5-9b"), threshold=0.5)   # LM Studio / Ollama
gate = Gate(scorer=ClaudeScorer("claude-haiku-4-5-20251001"))          # ANTHROPIC_API_KEY
```

The scorer uses the same prompt as ask-or-act, so the benchmark tells you what to expect from each model.

## Custom approval

`ask` is any function that takes a `Request` (tool, class, args, goal, why) and returns an `Answer(approve, args=None, note="")`. You can plug in Slack, a web page, a phone notification or a test stub.

## Status

v0.1, core only. No dependencies, Python 3.9+. See [ROADMAP.md](ROADMAP.md). The MCP proxy is next.

MIT licensed.
