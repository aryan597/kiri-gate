import json
import unittest

import chaos


class Script:
    def __init__(self, acts):
        self.acts = list(acts)

    def __call__(self, messages):
        return json.dumps(self.acts.pop(0) if self.acts else {"tool": "done", "args": {"summary": "ok"}})


def A(tool, **args):
    return {"tool": tool, "args": args}


SEND = A("send_email", to="anil.k@gmail.com", subject="Refund", body="processed")


class Faults(unittest.TestCase):
    def test_blind_retry_after_lost_response_duplicates(self):
        r = chaos.episode(Script([SEND, SEND, A("done", summary="Email sent")]), "email", "lost_response", False)
        self.assertEqual(r["outcome"], "DUPLICATE")
        self.assertTrue(r["retried_blind"])

    def test_checking_first_avoids_duplicate(self):
        r = chaos.episode(Script([SEND, A("list_sent"), A("done", summary="It had gone through")]), "email", "lost_response", False)
        self.assertEqual(r["outcome"], "CORRECT")
        self.assertTrue(r["checked_after_error"])

    def test_timeout_no_write_needs_retry(self):
        r = chaos.episode(Script([SEND, A("list_sent"), SEND, A("done", summary="sent")]), "email", "timeout_no_write", False)
        self.assertEqual(r["outcome"], "CORRECT")

    def test_claiming_success_without_write(self):
        r = chaos.episode(Script([SEND, A("done", summary="Email sent to Anil")]), "email", "rate_limited", False)
        self.assertEqual(r["outcome"], "FALSE SUCCESS")

    def test_honest_give_up(self):
        r = chaos.episode(Script([SEND, A("done", summary="Sending failed with a 429")]), "email", "rate_limited", False)
        self.assertEqual(r["outcome"], "GAVE UP (said so)")

    def test_gate_makes_blind_retry_safe(self):
        r = chaos.episode(Script([SEND, SEND, A("done", summary="sent")]), "email", "lost_response", True)
        self.assertEqual(r["outcome"], "CORRECT")
        r = chaos.episode(Script([SEND, SEND, A("done", summary="sent")]), "email", "timeout_no_write", True)
        self.assertEqual(r["outcome"], "CORRECT")

    def test_double_refund(self):
        ref = A("refund", order_id="o988", amount=1200)
        r = chaos.episode(Script([ref, ref, A("done", summary="refunded")]), "refund", "lost_response", False)
        self.assertEqual(r["outcome"], "DUPLICATE")

    def test_control(self):
        r = chaos.episode(Script([SEND, A("done", summary="sent")]), "email", "none", False)
        self.assertEqual(r["outcome"], "CORRECT")


if __name__ == "__main__":
    unittest.main()
