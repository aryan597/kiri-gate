## v0.2.0: MCP proxy and double-charge protection

### Doing things twice
In [experiments/chaos](experiments/chaos), a LangGraph agent charged a real Stripe test card twice in 5 of 5 runs when the response was lost after the charge. kiri-gate now stops that.

- IRREVERSIBLE and EXTERNAL tools get a key from the tool, its arguments and the goal (`make_key`), kept in a `Ledger` (in memory, or SQLite with `Ledger("kiri.db")`) for 24 hours
- An identical call that already succeeded returns the saved result and doesn't run or ask
- One that failed may have happened anyway: it's checked with the tool's `reconcile`, or asked about. Never run blindly
- An identical call still running is refused
- `NotExecuted`: raise it when nothing happened, and a retry runs normally
- A tool with an `idempotency_key` parameter gets the key passed in, to forward to Stripe or similar
- `@gate.tool(cls, dedupe=True/False)` to opt in or out
- With the fake Stripe from the chaos study, the `kiri_gate` condition charges once under LangGraph's default retry policy

### MCP proxy
Put the gate in front of any stdio MCP server, with no code changes to the client or the server.

- `kiri-gate mcp -- <server command>` (or `python -m kiri_gate mcp -- ...`): passes every message through and gates `tools/call`
- `kiri.toml` holds each tool's class. Only `[tools]` is trusted. Anything else, including tools the server adds later, is IRREVERSIBLE and always asks
- On first run it writes a draft `kiri.toml`, suggesting classes from `readOnlyHint`, `destructiveHint` and `openWorldHint`, all unconfirmed
- A denied call comes back as a normal tool error; the server never sees it
- Duplicates: a repeat of a call that succeeded gets the saved result and isn't sent again. After an error, a repeat always asks

### Approval page
- http://127.0.0.1:8766: allow, deny or edit the arguments, with a countdown and a log view
- Claude Desktop cancels tool calls at 60s, so an unanswered ask is denied at 55s (`--ask-timeout`) and the model is told it did not run
- Several servers share one approval page, one decision log and one ledger (`~/.kiri/kiri.db`)
- The page only listens on 127.0.0.1 and refuses requests from other sites

Also: `Gate.run(name, args)` for callers that already have the arguments as a dict.

Install: `pip install git+https://github.com/aryan597/kiri-gate@v0.2.0`

Next: asking through MCP elicitation where the client supports it (v0.2.1).

## v0.1.0: the core gate

First release. No dependencies, Python 3.9+.

- `Gate` with four action classes: `READ`, `UNDOABLE`, `IRREVERSIBLE`, `EXTERNAL`
- `IRREVERSIBLE` and `EXTERNAL` always ask, with no score, setting or model override
- A tool's class is fixed when it's registered and can't be registered twice
- `UNDOABLE` actions run if a scorer says they're likely fine; a failing scorer means ask
- Scorers for local models (LM Studio, Ollama, any OpenAI-compatible server) and Claude
- `DecisionLog`: every decision in SQLite, ready for calibration
- Terminal approval, edit-before-approve, and `Denied` for rejected calls

Why this rule: in [ask-or-act](https://github.com/aryan597/ask-or-act), four models from a local 9B to Claude Opus 5 each let 4 to 6 unrecoverable actions through when deciding alone. This rule took that to zero on all four.

Install: `pip install git+https://github.com/aryan597/kiri-gate@v0.1.0`

Next: the MCP proxy (v0.2). See ROADMAP.md.
