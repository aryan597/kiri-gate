# Duplicate side effects: qwen/qwen3.5-9b

Counts, not percentages (small n). A DUPLICATE means the email or refund really happened twice.

## LangChain `create_agent` + `ToolRetryMiddleware`

| fault | none | model | default | excluded | keyed |
|---|---|---|---|---|---|
| none | - | CORRECT 10 | CORRECT 10 | CORRECT 10 | CORRECT 10 |
| lost_response | - | DUPLICATE 7, CORRECT 3 | DUPLICATE 10 | DUPLICATE 6, CORRECT 4 | CORRECT 10 |
| timeout_no_write | - | CORRECT 5, GAVE UP (said so) 3, ASKED 1, FALSE SUCCESS 1 | CORRECT 10 | GAVE UP (said so) 7, CORRECT 2, ASKED 1 | CORRECT 10 |
| rate_limited | - | GAVE UP (said so) 10 | CORRECT 10 | GAVE UP (said so) 10 | CORRECT 10 |

Duplicates per condition: model 7/40, default 10/40, excluded 6/40, keyed 0/40

## Own harness (chaos.py): model sees the error and decides

| fault | bare | kiri-gate key |
|---|---|---|
| none | CORRECT 10 | CORRECT 10 |
| lost_response | CORRECT 6, DUPLICATE 4 | CORRECT 10 |
| timeout_no_write | ASKED 10 | ASKED 9, CORRECT 1 |
| rate_limited | ASKED 8, CORRECT 2 | ASKED 5, GAVE UP (said so) 1 |

## Reading it

- `none`: without middleware a timeout crashes the agent, even when the write already happened. That is why people add retry.
- `default` vs `model` on `lost_response`: duplicates in `default` come from the middleware re-running the write, not the model.
- `excluded` (fix 2) and `keyed` (fix 3) should show 0 duplicates.
- `timeout_no_write` is why retry exists: `excluded` may leave it undone, so the model must check or ask.
