# Roadmap

## The bet
Models are getting better at *spotting* risk (AUROC 0.95 to 0.996 from Qwen 9B to Opus 5), but they keep taking unrecoverable actions when the action "matches what the user asked". Intelligence doesn't close that gap. Structure does. kiri-gate is the structure: small, boring and hard to get around.

## v0.1: core (done)
- Gate, the four classes, fixed at registration
- Scorers: local (OpenAI-compatible) and Claude
- Decision log, terminal approval, `Denied`

## v0.2: MCP proxy and double-charge protection (done)
`kiri-gate mcp -- npx @some/mcp-server` sits between an MCP client and a stdio MCP server, with no code changes on either side. Set up and documented for Claude Desktop; other stdio clients should work but haven't been tried.
- Reads the server's tool list and classes each tool from `kiri.toml`. **Unknown tools default to IRREVERSIBLE**, so they always ask until you class them.
- Suggests classes from tool annotations (`readOnlyHint`, `destructiveHint`, `openWorldHint`) but never auto-trusts them.
- Asks through a local approval page, with a deadline set by the client's timeout.
- Idempotency keys from the tool and its arguments, a ledger, `reconcile` and `NotExecuted`, in the library and the proxy.
- Every decision goes into the log.

## v0.2.1: fixes from an outside review (done)
- Redaction of secrets in logs, the ledger, prompts and scorer input.
- `scope=` so users sharing a ledger don't share keys.
- Expired ledger rows and results are deleted.
- A "What we know" table in the README: every claim, its evidence and its limits.

## Next
- Run kiri-gate itself against real Stripe test mode, not only the fake.
- Measure how often models retry with *different* arguments, which dedupe can't catch (ask-or-act v2).
- `sensitive` fields per tool in `kiri.toml`, for the MCP proxy.
- Give scorers the tool's description, not just its name.
- Ask through MCP elicitation where the client supports it.
- A demo gif: an email send being caught in Claude Desktop.

## v0.3: learning your line
- Per-user thresholds from the log: approve something often enough and it stops asking, but only for UNDOABLE.
- Personal ask-or-act: does a model given your past decisions match your choices better than a generic one?
- Calibration report from your own log (`kiri-gate report`).

## v0.4: adapters
LangGraph, OpenAI Agents SDK and plain function-calling loops.

## Never
- A setting that lets IRREVERSIBLE or EXTERNAL run unasked.
- Letting a model decide a tool's class.
