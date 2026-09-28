"""No-LLM proof for langchain#40688: the retry middleware alone duplicates a write whose response was lost."""
import unittest

from langchain_repro import episode


def run(scenario, fault, condition):
    return episode(None, "", scenario, fault, condition)


class RetryMiddleware(unittest.TestCase):
    def test_default_middleware_duplicates_lost_response(self):
        for sc in ("email", "refund"):
            r = run(sc, "lost_response", "default")
            self.assertEqual(r["effects"], 2, sc)

    def test_no_middleware_crashes_even_though_write_happened(self):
        r = run("email", "lost_response", "none")
        self.assertTrue(r["outcome"].startswith("CRASHED"))
        self.assertEqual(r["effects"], 1)

    def test_excluding_write_tools_prevents_duplicate(self):
        self.assertEqual(run("refund", "lost_response", "excluded")["effects"], 1)

    def test_tool_call_id_key_prevents_duplicate_and_keeps_retry(self):
        self.assertEqual(run("refund", "lost_response", "keyed")["effects"], 1)
        self.assertEqual(run("refund", "timeout_no_write", "keyed")["effects"], 1)

    def test_retry_is_useful_when_nothing_was_written(self):
        self.assertEqual(run("email", "timeout_no_write", "default")["outcome"], "CORRECT")


if __name__ == "__main__":
    unittest.main()
