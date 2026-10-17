"""v0.2.2: confirm classes from the approval page, hot reload, whole-number keys, clearer asks."""
import os
import tempfile
import threading
import time
import unittest

from kiri_gate import EXTERNAL, READ, Answer, DecisionLog, Denied, Gate, make_key
from kiri_gate import config as kconfig
from kiri_gate.classes import Class
from kiri_gate.inbox import Approver
from kiri_gate.mcp import Proxy
from tests.test_inbox import http, wait_pending
from tests.test_mcp import SERVER, Client, ProxyRules


class Keys(unittest.TestCase):
    def test_whole_floats_match_ints(self):
        self.assertEqual(make_key("refund", {"amount": 64}), make_key("refund", {"amount": 64.0}))
        self.assertEqual(make_key("t", {"a": [1, {"b": 2.0}]}), make_key("t", {"a": [1.0, {"b": 2}]}))

    def test_other_differences_still_count(self):
        self.assertNotEqual(make_key("refund", {"amount": 64}), make_key("refund", {"amount": 64.5}))
        self.assertNotEqual(make_key("refund", {"amount": 64}), make_key("refund", {"amount": "64"}))
        self.assertNotEqual(make_key("t", {"a": True}), make_key("t", {"a": 1}))

    def test_retry_as_64_point_0_is_treated_as_the_same_call(self):
        ran, asked = [], []
        gate = Gate(ask=lambda r: (asked.append(r), Answer(len(asked) == 1))[1])

        @gate.tool(EXTERNAL)
        def refund(order, amount):
            ran.append(amount)
            raise TimeoutError("lost")

        with gate.goal("refund 1042"):
            self.assertRaises(TimeoutError, refund, "1042", 64)
            self.assertRaises(Denied, refund, "1042", 64.0)
        self.assertEqual(len(ran), 1)
        self.assertIn("may already have happened", asked[1].why)


class Reclassify(unittest.TestCase):
    def test_reclassify_changes_the_rule(self):
        asked = []
        gate = Gate(ask=lambda r: (asked.append(r), Answer(True))[1])

        @gate.tool(EXTERNAL)
        def look(x):
            return x

        look(1)
        gate.reclassify("look", READ)
        look(2)
        self.assertEqual(len(asked), 1)


class ConfirmFile(unittest.TestCase):
    def test_moves_the_line_and_keeps_the_rest(self):
        p = os.path.join(tempfile.mkdtemp(), "kiri.toml")
        with open(p, "w") as f:
            f.write('# mine\n[tools]\nsend = "external"\n\n[unconfirmed]\nread_file = "read"  # readOnlyHint\n'
                    '"odd name" = "irreversible"\n')
        kconfig.confirm(p, "read_file", Class.READ)
        kconfig.confirm(p, "odd name", Class.UNDOABLE)
        text = open(p).read()
        self.assertIn("# mine", text)
        self.assertEqual(text.count("read_file"), 1)
        cfg = kconfig.load(p)
        self.assertEqual(cfg.tools, {"send": Class.EXTERNAL, "read_file": Class.READ, "odd name": Class.UNDOABLE})
        self.assertEqual(cfg.unconfirmed, {})

    def test_creates_the_file_if_missing(self):
        p = os.path.join(tempfile.mkdtemp(), "kiri.toml")
        kconfig.confirm(p, "x", Class.READ)
        self.assertEqual(kconfig.load(p).tools, {"x": Class.READ})


class ProxyConfirm(ProxyRules):
    def test_unconfirmed_ask_says_why_and_remember_saves_it(self):
        self.start('[tools]\nsend_email = "external"\n', answer=Answer(True, remember="read"))
        self.c.request("tools/list")
        self.c.call("read_file", path="a")
        self.assertIn("isn't confirmed in kiri.toml", self.ask.asked[0].why)
        self.assertEqual(kconfig.load(self.cfg).tools["read_file"], Class.READ)
        self.c.call("read_file", path="b")
        self.assertEqual(len(self.ask.asked), 1)  # now a read: no second ask

    def test_hand_edit_is_picked_up_without_restart(self):
        self.start('[tools]\nsend_email = "external"\n')
        self.c.request("tools/list")
        self.c.call("send_email", to="a", body="1")
        with open(self.cfg, "w") as f:
            f.write('[tools]\nsend_email = "read"\n')
        t = time.time() + 5
        os.utime(self.cfg, (t, t))
        self.c.call("send_email", to="a", body="2")
        self.assertEqual(len(self.ask.asked), 1)

    def test_remember_on_a_confirmed_tool_changes_nothing(self):
        self.start('[tools]\nsend_email = "external"\n', answer=Answer(True, remember="read"))
        self.c.call("send_email", to="a", body="1")
        self.assertEqual(kconfig.load(self.cfg).tools["send_email"], Class.EXTERNAL)


for _name in [n for n in dir(ProxyRules) if n.startswith("test_")]:
    setattr(ProxyConfirm, _name, None)  # reuse the harness, not ProxyRules' own tests


class PageConfirm(unittest.TestCase):
    def start(self):
        d = tempfile.mkdtemp()
        self.cfg = os.path.join(d, "kiri.toml")
        with open(self.cfg, "w") as f:
            f.write('[tools]\nsend_email = "external"\n')
        self.log = DecisionLog(os.path.join(d, "k.db"))
        self.ap = Approver(port=0, open_browser=False)
        self.ap.ensure()
        c_r, p_w = os.pipe()
        p_r, c_w = os.pipe()
        self.proxy = Proxy(SERVER, config_path=self.cfg, log=self.log, approver=self.ap, ask_timeout=3,
                           stdin=os.fdopen(p_r, "rb"), stdout=os.fdopen(p_w, "wb"))
        self.to_proxy, self.from_proxy = os.fdopen(c_w, "wb"), os.fdopen(c_r, "rb")
        self.t = threading.Thread(target=self.proxy.run, daemon=True)
        self.t.start()
        self.c = Client(self.to_proxy, self.from_proxy)
        self.c.request("tools/list")

    def tearDown(self):
        self.to_proxy.close()
        self.t.join(10)
        self.proxy.cin.close()
        self.proxy.cout.close()
        self.from_proxy.close()
        self.log._db.close()

    def ask_on_page(self, tool, **args):
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("r", self.c.call(tool, **args)))
        th.start()
        return th, out, wait_pending(self.ap.url)[0]

    def test_card_shows_suggestion_and_remember_saves_it(self):
        self.start()
        th, out, item = self.ask_on_page("read_file", path="a")
        self.assertEqual((item["confirmable"], item["suggested"], item["suggested_why"]), (True, "read", "readOnlyHint"))
        code, _ = http(self.ap.url + "/api/decide", {"id": item["id"], "approve": True, "remember": "read"})
        self.assertEqual(code, 200)
        th.join(5)
        self.assertNotIn("isError", out["r"]["result"])
        self.assertEqual(kconfig.load(self.cfg).tools["read_file"], Class.READ)
        r = self.c.call("read_file", path="b")   # would time out (3s) with an error if it asked again
        self.assertNotIn("isError", r["result"])
        self.assertEqual(http(self.ap.url + "/api/state")[1]["pending"], [])

    def test_deny_never_remembers(self):
        self.start()
        th, out, item = self.ask_on_page("read_file", path="a")
        http(self.ap.url + "/api/decide", {"id": item["id"], "approve": False, "remember": "read"})
        th.join(5)
        self.assertNotIn("read_file", kconfig.load(self.cfg).tools)

    def test_confirmed_tools_are_not_offered_and_bad_classes_are_refused(self):
        self.start()
        th, out, item = self.ask_on_page("send_email", to="a", body="b")
        self.assertNotIn("confirmable", item)
        code, body = http(self.ap.url + "/api/decide", {"id": item["id"], "approve": True, "remember": "god"})
        self.assertEqual(code, 400)
        http(self.ap.url + "/api/decide", {"id": item["id"], "approve": True, "remember": "read"})
        th.join(5)
        self.assertEqual(kconfig.load(self.cfg).tools["send_email"], Class.EXTERNAL)


if __name__ == "__main__":
    unittest.main()
