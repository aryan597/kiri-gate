# Stripe test-mode duplicate charges: scripted

LangGraph StateGraph + ToolNode. The first charge succeeds in Stripe, then the tool raises APIConnectionError.
Charges are counted from Stripe's own PaymentIntent list.

| condition | runs | duplicate charges | charges per run | outcomes |
|---|---|---|---|---|
| graph_retry | 1 | 1/1 | 2 | DUPLICATE 1 |
| model_decides | 1 | 0/1 | 1 | CORRECT 1 |
| key_call_id | 1 | 0/1 | 1 | CORRECT 1 |
| key_args | 1 | 0/1 | 1 | CORRECT 1 |
