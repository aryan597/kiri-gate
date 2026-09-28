# Duplicate side effects: scripted

Counts, not percentages (small n). A DUPLICATE means the email or refund really happened twice.

## LangChain `create_agent` + `ToolRetryMiddleware`

| fault | none | model | default | excluded | keyed |
|---|---|---|---|---|---|
| none | CORRECT 2 | CORRECT 2 | CORRECT 2 | CORRECT 2 | CORRECT 2 |
| lost_response | CRASHED (1 done) 2 | CORRECT 2 | DUPLICATE 2 | CORRECT 2 | CORRECT 2 |
| timeout_no_write | CRASHED (0 done) 2 | FALSE SUCCESS 2 | CORRECT 2 | FALSE SUCCESS 2 | CORRECT 2 |
| rate_limited | CRASHED (0 done) 2 | FALSE SUCCESS 2 | CORRECT 2 | FALSE SUCCESS 2 | CORRECT 2 |

Duplicates per condition: none 0/8, model 0/8, default 2/8, excluded 0/8, keyed 0/8

## Reading it

- `none`: without middleware a timeout crashes the agent, even when the write already happened. That is why people add retry.
- `default` vs `model` on `lost_response`: duplicates in `default` come from the middleware re-running the write, not the model.
- `excluded` (fix 2) and `keyed` (fix 3) should show 0 duplicates.
- `timeout_no_write` is why retry exists: `excluded` may leave it undone, so the model must check or ask.
