import os
import tempfile
import unittest

from kiri_gate import EXTERNAL, IRREVERSIBLE, READ, UNDOABLE, Answer, DecisionLog, Denied, Gate, RegistryError
from kiri_gate.scorers import parse_ok


class Recorder:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def __call__(self, req):
        self.asked.append(req)
        return self.answer


def sure(goal, tool, args):  # a scorer that thinks everything is fine
    return 0.99


class GateRules(unittest.TestCase):
    def setUp(self):
        self.ask = Recorder(Answer(True))
        self.gate = Gate(ask=self.ask, scorer=sure)
        self.ran = []
        g = self.gate

        @g.tool(READ)
        def read(path): self.ran.append("read"); return "data"

        @g.tool(UNDOABLE)
        def edit(path, text): self.ran.append("edit")

        @g.tool(IRREVERSIBLE)
        def delete(path): self.ran.append("delete")

        @g.tool(EXTERNAL)
        def send(to, body): self.ran.append("send")

        self.read, self.edit, self.delete, self.send = read, edit, delete, send

    def test_read_acts(self):
        self.assertEqual(self.read("a"), "data")
        self.assertEqual(self.ask.asked, [])

    def test_irreversible_and_external_always_ask_even_when_model_is_sure(self):
        self.delete("x")
        self.send("a@b", "hi")
        self.assertEqual([r.tool for r in self.ask.asked], ["delete", "send"])

    def test_undoable_follows_the_score(self):
        self.edit("f", "t")
        self.assertEqual(self.ask.asked, [])
        self.gate.scorer = lambda g, t, a: 0.2
        self.edit("f", "t")
        self.assertEqual(len(self.ask.asked), 1)

    def test_broken_scorer_asks(self):
        def boom(*a): raise RuntimeError("down")
        self.gate.scorer = boom
        self.edit("f", "t")
        self.assertEqual(len(self.ask.asked), 1)

    def test_deny_raises_and_does_not_run(self):
        self.gate.ask = Recorder(Answer(False, note="not now"))
        with self.assertRaises(Denied):
            self.send("a@b", "hi")
        self.assertNotIn("send", self.ran)

    def test_edited_args_are_used(self):
        got = []
        g = Gate(ask=lambda r: Answer(True, args={"to": "safe@me", "body": "hi"}))

        @g.tool(EXTERNAL)
        def mail(to, body): got.append(to)
        mail("wrong@x", "hi")
        self.assertEqual(got, ["safe@me"])

    def test_class_is_fixed(self):
        with self.assertRaises(RegistryError):
            @self.gate.tool(READ)
            def delete(path): pass

    def test_goal_reaches_the_human(self):
        with self.gate.goal("Clean build artifacts"):
            self.delete("dist")
        self.assertEqual(self.ask.asked[0].goal, "Clean build artifacts")

    def test_log(self):
        path = os.path.join(tempfile.mkdtemp(), "k.db")
        g = Gate(ask=lambda r: Answer(True), scorer=lambda *a: 0.3, log=DecisionLog(path))

        @g.tool(UNDOABLE)
        def e(x): pass
        e(1)
        rows = g.log.rows()
        self.assertEqual(rows[0]["decision"], "ask")
        self.assertEqual(g.log.labelled(), [(0.3, 1)])


class Parse(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_ok('{"ok": 1}'), 0.01)
        self.assertEqual(parse_ok('{"reason": "x", "ok": 85}'), 0.85)
        self.assertEqual(parse_ok('{"ok": 0.3}'), 0.3)
        self.assertIsNone(parse_ok("nope"))


if __name__ == "__main__":
    unittest.main()
