# Roadmap

## The bet
Models are getting better at *spotting* risk (AUROC 0.95 to 0.996 from Qwen 9B to Opus 5), but they keep taking unrecoverable actions when the action "matches what the user asked". Intelligence doesn't close that gap. Structure does. kiri-gate is the structure: small, boring and hard to get around.

## v0.1: core (done)
- Gate, the four classes, fixed at registration
- Scorers: local (OpenAI-compatible) and Claude
- Decision log, terminal approval, `Denied`

## v0.2: MCP proxy (next, the main product)
`kiri-gate mcp -- npx @some/mcp-server` sits between any MCP client (Claude Desktop, Cursor, Claude Code) and any MCP server. No code changes for anyone.
- Reads the server's tool list and classes each tool from `kiri.toml`. **Unknown tools default to IRREVERSIBLE**, so they always ask until you class them.
- Suggests classes from tool annotations (`readOnlyHint`, `destructiveHint`) but never auto-trusts them.
- Asks through MCP elicitation where the client supports it, and otherwise through a local approval page.
- Every decision goes into the log.

## v0.3: learning your line
- Per-user thresholds from the log: approve something often enough and it stops asking, but only for UNDOABLE.
- Personal ask-or-act: does a model given your past decisions match your choices better than a generic one?
- Calibration report from your own log (`kiri-gate report`).

## v0.4: adapters
LangGraph, OpenAI Agents SDK and plain function-calling loops.

## Never
- A setting that lets IRREVERSIBLE or EXTERNAL run unasked.
- Letting a model decide a tool's class.
