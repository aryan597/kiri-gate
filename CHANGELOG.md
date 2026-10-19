## v0.2.2: a first run that doesn't fight you

From a real install on Linux and a live demo: on a fresh install every tool asked, reads included, and the only way out was editing kiri.toml and restarting the client.

- **Fixed: a stale read could re-run an UNKNOWN call** ([#1](https://github.com/aryan597/kiri-gate/issues/1), reported by [@xianmgb-cmyk](https://github.com/xianmgb-cmyk) with a minimal repro). Two identical dedupe calls at once: B reads "absent" and is being scored, A runs, loses its response and is marked UNKNOWN, then B claimed the UNKNOWN row and ran again without asking. `Ledger.claim` now takes `expect`, the state the caller was routed on, and refuses if the row has changed since. The Gate then goes back through reconcile/ask. The MCP proxy had the same check-then-claim gap and gets the same fix.
- Limits: tested with the in-memory ledger and simulated effects (the C01 schedule). The proxy fix is covered by the ledger unit test, not a concurrent proxy test. `claim()` without `expect` keeps the v0.2.1 behaviour for code calling the ledger directly.
- 2 new tests for #1. The C01 test fails on v0.2.1 (2 effects, 0 prompts) and passes now (1 effect, 1 prompt).
- **Fixed: a malformed line could skip the gate.** A client line that isn't valid JSON (for example a `tools/call` with one invalid UTF-8 byte) used to be forwarded to the server unread. The MCP TypeScript SDK decodes invalid bytes with replacement, so the server ran a call kiri-gate never saw. Reproduced against a real SDK 1.32.1 server: a denied charge went through with the bad byte. Lines kiri-gate can't read, and anything that isn't a JSON-RPC object, are now refused with an error and never forwarded. Found in an internal audit after #1.
- **Fixed: kiri.toml hand edits could be undone.** After the first draft, every `tools/list` rewrote kiri.toml from memory, and a change made inside the same file-timestamp tick wasn't noticed. Either could put back a class you had just changed, so a tool could run without asking. The draft is now written only when there is no kiri.toml, and changes are detected by content, not timestamp.
- **Fixed: a very deeply nested line stopped the proxy.** It's now refused like any unreadable line.
- Limits: the bypass needs a client that sends invalid bytes; the model can't produce them through a normal client. Other audit findings (no token on the approval page, 24h replay of identical calls, server labels) are still open.
- **Confirm classes on the approval page.** A tool not yet in `[tools]` shows what the server suggests ("read, from readOnlyHint") and a "remember" box. Tick it and Allow, and the class is saved to kiri.toml and used from the next call. Off by default, only on Allow, and only for tools not already confirmed, so a tired click can't downgrade anything.
- **kiri.toml is re-read when it changes.** No client restart after editing it by hand.
- **Clear reasons.** An unconfirmed tool says so ("isn't confirmed in kiri.toml yet") instead of "irreversible actions always ask", and its card is marked NOT CONFIRMED. First run prints where kiri.toml is and how to confirm tools.
- **64 and 64.0 are the same call.** Whole-number floats hash like ints in dedupe keys. "64" (a string) and 64.5 are still different.
- **Log page fixes.** A caught duplicate showed as "denied"; it now says "duplicate, not sent again", with a "Duplicates stopped" count. Rows keep the class the tool had when you decided.
- The dedupe log row is written before the reply, so anything reading the log right after sees it.
- 30 new tests (91 in total).

## v0.2.1: fixes from an outside review

Someone reviewed the code and found real gaps. These are the fixes.

- **Secrets redacted.** Arguments and results are redacted before they reach the decision log, the ledger file, the terminal prompt, the approval page or a scorer. Secret-looking names (password, token, api_key, card_number, ...) and known key formats (`sk_live_`, `ghp_`, `AKIA`, `Bearer`, JWTs) become `[REDACTED]`. Add your own with `@gate.tool(..., sensitive={"field"})`. The tool still gets the real values, and an edit that leaves `[REDACTED]` keeps the real value.
- **Scope.** `Gate(scope="user-123")` or `gate.goal(text, scope=...)` puts the user in the dedupe key, so two users sending the identical email through one shared ledger are two calls. No scope set means the same keys as v0.2.0.
- **Expiry.** Ledger rows and in-memory results older than the 24 hour window are now deleted (every 100 writes, or `Ledger.prune()`), not just ignored.
- **Docs say what's measured.** The README has a "What we know" table: each claim, its evidence, whether it's measured, true by construction or untested, and its limits. "Can't happen twice" wording is gone: it catches identical calls, and never retries an uncertain one automatically.
- 12 new tests (61 in total).

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
