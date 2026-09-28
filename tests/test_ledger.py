"""Doing things twice: the ledger, and the Gate's dedupe on top of it."""

import os
import tempfile
import threading
import time
import unittest

from kiri_gate import EXTERNAL, UNDOABLE, Answer, DecisionLog, Denied, Gate, Ledger, NotExecuted, make_key
from kiri_gate.ledger import DONE, UNKNOWN


class Recorder:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def __call__(self, req):
        self.asked.append(req)
        return self.answer


class LostResponse(Exception):
    """The write went through, the reply didn't."""


class Card:
    """A payment API: every charge is an effect. The first response is lost after the charge happens."""

    def __init__(self, lose_first=True):
        self.charges, self.lose_first = [], lose_first

    def charge(self, order: str, amount: int, idempotency_key: str = None):
        self.charges.append(idempotency_key)
        if self.lose_first and len(self.charges) == 1:
            raise LostResponse("read timed out")
        return {"id": f"ch_{len(self.charges)}", "order": order, "amount": amount}

    def find(self, order: str, amount: int, idempotency_key: str = None):
        for i, k in enumerate(self.charges):
            if k == idempotency_key:
                return {"id": f"ch_{i + 1}", "order": order, "amount": amount}
        return None


def setup(answer=Answer(True), reconcile=True, **gate_kw):
    card = Card()
    ask = Recorder(answer)
    g = Gate(ask=ask, **gate_kw)
    charge = g.register(card.charge, EXTERNAL, name="charge", reconcile=card.find if reconcile else None)
    return g, card, ask, charge


class Dedupe(unittest.TestCase):
    def test_lost_response_with_reconcile_returns_the_result_and_charges_once(self):
        g, card, ask, charge = setup()
        with self.assertRaises(LostResponse):
            charge("o988", 1200)
        r = charge("o988", 1200)  # a framework retry
        self.assertEqual(r["id"], "ch_1")
        self.assertEqual(len(card.charges), 1)
        self.assertEqual(len(ask.asked), 1)  # only the first, normal EXTERNAL ask

    def test_lost_response_without_reconcile_asks_and_a_deny_stops_it(self):
        g, card, ask, charge = setup(reconcile=False)
        with self.assertRaises(LostResponse):
            charge("o988", 1200)
        ask.answer = Answer(False)
        with self.assertRaises(Denied):
            charge("o988", 1200)
        self.assertEqual(len(card.charges), 1)
        self.assertIn("may already have happened", ask.asked[1].why)
        self.assertIn("LostResponse", ask.asked[1].why)

    def test_unknown_asks_even_for_a_class_that_would_act(self):
        effects = []

        def save(path, text):
            effects.append(path)
            raise LostResponse()

        ask = Recorder(Answer(False))
        g = Gate(ask=ask)
        save = g.register(save, UNDOABLE, dedupe=True)
        with self.assertRaises(LostResponse):
            save("a", "t")
        self.assertEqual(ask.asked, [])  # undoable, no scorer: acted
        with self.assertRaises(Denied):
            save("a", "t")
        self.assertEqual(len(effects), 1)

    def test_model_replan_gets_the_same_key(self):
        # A model re-planning after an error sends a new call with the same arguments, maybe in another order.
        self.assertEqual(make_key("charge", {"order": "o988", "amount": 1200}),
                         make_key("charge", {"amount": 1200, "order": "o988"}))
        g, card, ask, charge = setup()
        with self.assertRaises(LostResponse):
            g.run("charge", {"order": "o988", "amount": 1200})
        self.assertEqual(g.run("charge", {"amount": 1200, "order": "o988"})["id"], "ch_1")
        self.assertEqual(len(card.charges), 1)

    def test_not_executed_releases_so_a_real_retry_runs_once(self):
        runs = []

        def send(to):
            runs.append(to)
            if len(runs) == 1:
                raise NotExecuted("429 before anything ran")
            return "sent"

        g = Gate(ask=Recorder(Answer(True)))
        send = g.register(send, EXTERNAL)
        with self.assertRaises(NotExecuted):
            send("a@b")
        self.assertEqual(send("a@b"), "sent")
        self.assertEqual(send("a@b"), "sent")  # third time: DONE, not run
        self.assertEqual(len(runs), 2)

    def test_done_returns_the_saved_result_without_asking(self):
        card = Card(lose_first=False)
        ask = Recorder(Answer(True))
        log = DecisionLog(os.path.join(tempfile.mkdtemp(), "k.db"))
        g = Gate(ask=ask, log=log)
        charge = g.register(card.charge, EXTERNAL, name="charge")
        a = charge("o1", 100)
        b = charge("o1", 100)
        self.assertIs(a, b)
        self.assertEqual(len(card.charges), 1)
        self.assertEqual(len(ask.asked), 1)
        self.assertEqual([r["decision"] for r in log.rows()], ["ask", "deduped"])
        log._db.close()

    def test_different_args_or_goal_are_different_calls(self):
        card = Card(lose_first=False)
        g = Gate(ask=Recorder(Answer(True)))
        charge = g.register(card.charge, EXTERNAL, name="charge")
        charge("o1", 100)
        charge("o1", 200)
        with g.goal("another task"):
            charge("o1", 100)
        self.assertEqual(len(card.charges), 3)

    def test_key_is_passed_to_the_tool_unless_the_caller_set_one(self):
        card = Card(lose_first=False)
        g = Gate(ask=Recorder(Answer(True)))
        charge = g.register(card.charge, EXTERNAL, name="charge")
        charge("o1", 100)
        self.assertEqual(card.charges[0], make_key("charge", {"order": "o1", "amount": 100}))
        charge("o2", 100, idempotency_key="mine")
        self.assertEqual(card.charges[1], "mine")

    def test_the_window_expires(self):
        card = Card(lose_first=False)
        g = Gate(ask=Recorder(Answer(True)), ledger=Ledger(window=0.2))
        charge = g.register(card.charge, EXTERNAL, name="charge")
        charge("o1", 100)
        charge("o1", 100)
        time.sleep(0.3)
        charge("o1", 100)
        self.assertEqual(len(card.charges), 2)

    def test_identical_call_in_flight_is_refused(self):
        started, go = threading.Event(), threading.Event()

        def slow(x):
            started.set()
            go.wait(5)
            return "ok"

        g = Gate(ask=Recorder(Answer(True)))
        slow = g.register(slow, EXTERNAL)
        t = threading.Thread(target=slow, args=(1,))
        t.start()
        started.wait(5)
        with self.assertRaises(Denied) as e:
            slow(1)
        self.assertIn("already running", str(e.exception))
        go.set()
        t.join(5)

    def test_broken_reconcile_means_ask(self):
        card = Card()
        ask = Recorder(Answer(False))

        def boom(**kw):
            raise RuntimeError("lookup down")

        g = Gate(ask=ask)
        charge = g.register(card.charge, EXTERNAL, name="charge", reconcile=boom)
        ask.answer = Answer(True)
        with self.assertRaises(LostResponse):
            charge("o1", 100)
        ask.answer = Answer(False)
        with self.assertRaises(Denied):
            charge("o1", 100)
        self.assertEqual(len(card.charges), 1)

    def test_reconcile_saying_it_did_not_happen_runs_it(self):
        card = Card()
        g = Gate(ask=Recorder(Answer(True)))
        charge = g.register(card.charge, EXTERNAL, name="charge", reconcile=lambda **kw: None)
        with self.assertRaises(LostResponse):
            charge("o1", 100)
        self.assertEqual(charge("o1", 100)["id"], "ch_2")

    def test_dedupe_defaults(self):
        g = Gate(ask=Recorder(Answer(True)))
        n = []
        edit = g.register(lambda p: n.append(p), UNDOABLE, name="edit")
        send = g.register(lambda p: n.append(p), EXTERNAL, name="send", dedupe=False)
        edit("a"); edit("a"); send("b"); send("b")
        self.assertEqual(n, ["a", "a", "b", "b"])


class LedgerFile(unittest.TestCase):
    def test_done_survives_a_restart(self):
        path = os.path.join(tempfile.mkdtemp(), "k.db")
        card = Card(lose_first=False)
        l1 = Ledger(path)
        Gate(ask=Recorder(Answer(True)), ledger=l1).register(card.charge, EXTERNAL, name="charge")("o1", 100)
        l1.close()
        l2 = Ledger(path)
        again = Gate(ask=Recorder(Answer(True)), ledger=l2).register(card.charge, EXTERNAL, name="charge")("o1", 100)
        self.assertEqual(again["id"], "ch_1")  # the saved JSON, not a new charge
        self.assertEqual(len(card.charges), 1)
        l2.close()

    def test_a_stale_claim_counts_as_unknown(self):
        l = Ledger(stale=0.1)
        self.assertTrue(l.claim("k", "t", {}))
        self.assertFalse(l.claim("k", "t", {}))
        time.sleep(0.2)
        self.assertEqual(l.get("k")[0], UNKNOWN)
        self.assertTrue(l.claim("k", "t", {}))

    def test_release_and_done(self):
        l = Ledger()
        l.claim("k", "t", {})
        l.release("k")
        self.assertEqual(l.get("k")[0], None)
        l.claim("k", "t", {})
        l.done("k", {"a": 1})
        self.assertEqual(l.get("k")[:2], (DONE, {"a": 1}))


if __name__ == "__main__":
    unittest.main()
