"""Offline checks (in-memory Stripe with real idempotency semantics). No network, no LLM."""
import unittest

import stripe_repro as S


def run(cond):
    return S.episode(S.FakeStripe(), cond, None, "")


class LangGraphRetry(unittest.TestCase):
    def test_default_retry_policy_charges_twice(self):
        r = run("graph_retry")
        self.assertEqual(r["charges"], 2)
        self.assertEqual(r["final"], "Done.")  # and still reports success

    def test_tool_call_id_key_dedupes_graph_retry(self):
        self.assertEqual(run("key_call_id")["charges"], 1)

    def test_args_key_dedupes_graph_retry(self):
        self.assertEqual(run("key_args")["charges"], 1)

    def test_fake_stripe_idempotency(self):
        f = S.FakeStripe()
        a = f.charge(1200, "gbp", "r", "k")
        b = f.charge(1200, "gbp", "r", "k")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(f.count("r", 0), 1)


if __name__ == "__main__":
    unittest.main()
