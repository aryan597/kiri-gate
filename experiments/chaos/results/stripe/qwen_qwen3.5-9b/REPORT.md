# Stripe test-mode duplicate charges: qwen_qwen3.5-9b

LangGraph StateGraph + ToolNode. The first charge succeeds in Stripe, then the tool raises APIConnectionError.
Charges are counted from Stripe's own PaymentIntent list.

| condition | runs | duplicate charges | charges per run | outcomes |
|---|---|---|---|---|
| graph_retry | 5 | 5/5 | 2, 2, 2, 2, 2 | DUPLICATE 5 |
| model_decides | 5 | 5/5 | 2, 2, 2, 2, 2 | DUPLICATE 5 |
| key_call_id | 5 | 0/5 | 1, 1, 1, 1, 1 | CORRECT 5 |
| key_args | 5 | 0/5 | 1, 1, 1, 1, 1 | CORRECT 5 |
