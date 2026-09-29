"""v0.2.1: secrets stay out of logs, the ledger file, the approval prompt and the scorer.
Dedupe keys can be scoped to a user. Old ledger entries are deleted, not just ignored."""

import os
import sqlite3
import tempfile
import time
import unittest

from kiri_gate import EXTERNAL, UNDOABLE, Answer, DecisionLog, Gate, Ledger
from kiri_gate.redact import REDACTED, redact, restore, sensitive_name

SECRET = "hunter2-very-secret"


def dump(path):
    db = sqlite3.connect(path)
    out = []
    for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        out += [repr(r) for r in db.execute(f"SELECT * FROM {t}")]
    db.close()
    return "\n".join(out)


class TestRedact(unittest.TestCase):
    def test_names_at_any_depth(self):
        r = redact({"to": "a@b.c", "api_key": SECRET, "auth": {"password": SECRET, "user": "ann"},
                    "cards": [{"card_number": "4242424242424242"}]})
        self.assertEqual(r["to"], "a@b.c")
        self.assertEqual(r["api_key"], REDACTED)
        self.assertEqual(r["auth"], {"password": REDACTED, "user": "ann"})
        self.assertEqual(r["cards"], [{"card_number": REDACTED}])

    def test_known_key_formats_in_any_field(self):
        for v in ("sk_live_abc123", "ghp_abcdef", "Bearer abc.def", "AKIAABCDEFGHIJKL1234"):
            self.assertEqual(redact({"body": v})["body"], REDACTED, v)

    def test_not_secret(self):
        for n in ("idempotency_key", "max_tokens", "to", "subject", "amount", "spinner", "hotplate"):
            self.assertFalse(sensitive_name(n), n)
        self.assertTrue(sensitive_name("note", extra={"note"}))

    def test_input_unchanged_and_restore(self):
        a = {"to": "x", "token": SECRET, "n": {"password": SECRET}}
        r = redact(a)
        self.assertEqual(a["token"], SECRET)
        edited = dict(r, to="y")
        self.assertEqual(restore(edited, a), {"to": "y", "token": SECRET, "n": {"password": SECRET}})
        self.assertEqual(restore(dict(r, token="new"), a)["token"], "new")


class TestGateNeverLeaks(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.log_path, self.ledger_path = os.path.join(d, "log.db"), os.path.join(d, "ledger.db")
        self.asked, self.got = [], []

    def gate(self, answer=Answer(True), **kw):
        def ask(req):
            self.asked.append(req)
            return answer
        return Gate(ask=ask, log=DecisionLog(self.log_path), ledger=Ledger(self.ledger_path), **kw)

    def test_log_ledger_and_prompt(self):
        g = self.gate()

        @g.tool(EXTERNAL)
        def send(to, api_key):
            self.got.append(api_key)
            return {"sent": True, "session_token": SECRET}

        send("a@b.c", SECRET)
        self.assertEqual(self.got, [SECRET])                      # the tool gets the real value
        self.assertEqual(self.asked[0].args["api_key"], REDACTED)  # the human doesn't see it
        self.assertNotIn(SECRET, dump(self.log_path))
        self.assertNotIn(SECRET, dump(self.ledger_path))           # neither argument nor result
        self.assertEqual(send("a@b.c", SECRET)["session_token"], SECRET)  # dedupe still works
        self.assertEqual(len(self.got), 1)

    def test_edit_keeps_real_secret_unless_changed(self):
        g = self.gate(Answer(True, args={"to": "new@b.c", "api_key": REDACTED}))

        @g.tool(EXTERNAL)
        def send(to, api_key):
            self.got.append((to, api_key))

        send("a@b.c", SECRET)
        self.assertEqual(self.got, [("new@b.c", SECRET)])

    def test_extra_sensitive_names(self):
        g = self.gate()

        @g.tool(EXTERNAL, sensitive={"note"})
        def post(note):
            return "ok"

        post(SECRET)
        self.assertEqual(self.asked[0].args["note"], REDACTED)
        self.assertNotIn(SECRET, dump(self.log_path))

    def test_scorer_never_sees_secrets(self):
        seen = []
        g = Gate(ask=lambda r: Answer(True), scorer=lambda goal, tool, args: seen.append(args) or 0.9)

        @g.tool(UNDOABLE)
        def update(user, password):
            return "ok"

        update("ann", SECRET)
        self.assertEqual(seen[0]["password"], REDACTED)


class TestScope(unittest.TestCase):
    def setUp(self):
        self.sent = []

    def gate(self, **kw):
        g = Gate(ask=lambda r: Answer(True), **kw)

        @g.tool(EXTERNAL)
        def send(to, body):
            self.sent.append((to, body))
            return "sent"
        return g, send

    def test_no_scope_behaves_like_v020(self):
        g, send = self.gate()
        send("x@y.z", "hello")
        send("x@y.z", "hello")
        self.assertEqual(len(self.sent), 1)

    def test_two_users_same_email_both_send(self):
        ledger = Ledger()
        ga, send_a = self.gate(ledger=ledger, scope="user-a")
        gb, send_b = self.gate(ledger=ledger, scope="user-b")
        send_a("x@y.z", "hello")
        send_b("x@y.z", "hello")
        send_a("x@y.z", "hello")
        self.assertEqual(len(self.sent), 2)

    def test_scope_per_block(self):
        g, send = self.gate()
        with g.goal("reply", scope="user-a"):
            send("x@y.z", "hello")
        with g.goal("reply", scope="user-b"):
            send("x@y.z", "hello")
        with g.goal("reply", scope="user-a"):
            send("x@y.z", "hello")
        self.assertEqual(len(self.sent), 2)


class TestExpiry(unittest.TestCase):
    def test_prune_deletes_old_rows_and_results(self):
        path = os.path.join(tempfile.mkdtemp(), "l.db")
        led = Ledger(path, window=0.05)
        led.claim("k", "t", {})
        led.done("k", {"big": "x" * 100})
        time.sleep(0.1)
        self.assertEqual(led.prune(), 1)
        self.assertEqual(led._results, {})
        self.assertEqual(led.get("k")[0], None)
        self.assertNotIn("big", dump(path))


if __name__ == "__main__":
    unittest.main()
