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
