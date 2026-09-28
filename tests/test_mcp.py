import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

from kiri_gate import Answer, DecisionLog
from kiri_gate import config as kconfig
from kiri_gate.mcp import Proxy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = [sys.executable, os.path.join(HERE, "fake_mcp_server.py")]


class Client:
    """Talks JSON-RPC lines to something, reads replies on a thread."""

    def __init__(self, write, read):
        self.write, self.q, self.n = write, queue.Queue(), 0
        threading.Thread(target=lambda: [self.q.put(json.loads(l)) for l in read if l.strip()], daemon=True).start()

    def send(self, msg):
        self.write.write(json.dumps(msg).encode() + b"\n")
        self.write.flush()

    def request(self, method, params=None):
        self.n += 1
        self.send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        while True:
            m = self.q.get(timeout=10)
            if m.get("id") == self.n:
                return m

    def call(self, name, **args):
        return self.request("tools/call", {"name": name, "arguments": args})


class Recorder:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def __call__(self, req):
        self.asked.append(req)
        return self.answer


class ProxyRules(unittest.TestCase):
    def start(self, toml=None, answer=Answer(True)):
        self.dir = tempfile.mkdtemp()
        self.cfg = os.path.join(self.dir, "kiri.toml")
        self.calls = os.path.join(self.dir, "calls.jsonl")
        os.environ["FAKE_MCP_CALLS"] = self.calls
        if toml is not None:
            with open(self.cfg, "w") as f:
                f.write(toml)
        self.ask = Recorder(answer)
        self.log = DecisionLog(os.path.join(self.dir, "k.db"))
        c_r, p_w = os.pipe()   # proxy -> client
        p_r, c_w = os.pipe()   # client -> proxy
        self.proxy = Proxy(SERVER, config_path=self.cfg, ask=self.ask, log=self.log,
                           stdin=os.fdopen(p_r, "rb"), stdout=os.fdopen(p_w, "wb"))
        self.to_proxy = os.fdopen(c_w, "wb")
        self.from_proxy = os.fdopen(c_r, "rb")
        self.t = threading.Thread(target=self.proxy.run, daemon=True)
        self.t.start()
        self.c = Client(self.to_proxy, self.from_proxy)
        self.c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
        self.c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tearDown(self):
        self.to_proxy.close()
        self.t.join(timeout=10)
        self.proxy.cin.close()
        self.proxy.cout.close()  # the client's reader sees EOF
        self.from_proxy.close()
        self.log._db.close()
        os.environ.pop("FAKE_MCP_CALLS", None)

    def ran(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return [json.loads(l) for l in f]

    def test_passthrough_and_draft_written(self):
        self.start()
        tools = self.c.request("tools/list")["result"]["tools"]
        self.assertEqual(len(tools), 5)
        cfg = kconfig.load(self.cfg)
        self.assertEqual(cfg.tools, {})  # nothing is trusted until you confirm it
        self.assertEqual(cfg.unconfirmed, {"read_file": "read", "write_draft": "undoable",
                                           "delete_file": "irreversible", "send_email": "external",
                                           "mystery": "irreversible"})

    def test_unconfirmed_tools_always_ask_even_read_only_ones(self):
        self.start()
        self.c.request("tools/list")
        self.c.call("read_file", path="a")
        self.assertEqual([r.tool for r in self.ask.asked], ["read_file"])
        self.assertEqual(self.ask.asked[0].cls.name, "IRREVERSIBLE")

    def test_confirmed_read_acts_without_asking(self):
        self.start('[tools]\nread_file = "read"\n')
        r = self.c.call("read_file", path="a")
        self.assertEqual(r["result"]["content"][0]["text"], 'ran read_file {"path": "a"}')
        self.assertEqual(self.ask.asked, [])

    def test_external_asks_and_denial_is_a_tool_error_that_never_runs(self):
        self.start('[tools]\nsend_email = "external"\n', answer=Answer(False, note="not now"))
        r = self.c.call("send_email", to="recruiter@x.com", body="hi")
        self.assertTrue(r["result"]["isError"])
        self.assertIn("did not run", r["result"]["content"][0]["text"])
        self.assertIn("not now", r["result"]["content"][0]["text"])
        self.assertEqual(self.ran(), [])
        row = self.log.rows()[0]
        self.assertEqual((row["tool"], row["cls"], row["decision"], row["approved"]), ("send_email", "EXTERNAL", "ask", 0))

    def test_approved_with_edited_args_sends_the_edit(self):
        self.start('[tools]\nsend_email = "external"\n', answer=Answer(True, args={"to": "me@x.com", "body": "hi"}))
        self.c.call("send_email", to="recruiter@x.com", body="hi")
        self.assertEqual(self.ran(), [{"name": "send_email", "arguments": {"to": "me@x.com", "body": "hi"}}])

    def test_tools_call_inside_a_batch_is_still_gated(self):
        self.start('[tools]\nsend_email = "external"\n', answer=Answer(False))
        self.c.send([{"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                      "params": {"name": "send_email", "arguments": {"to": "a"}}}])
        while True:
            m = self.c.q.get(timeout=10)
            if m.get("id") == 99:
                break
        self.assertTrue(m["result"]["isError"])
        self.assertEqual(self.ran(), [])

    def test_duplicate_after_success_gets_the_saved_result_and_never_reaches_the_server(self):
        self.start('[tools]\nsend_email = "external"\n')
        first = self.c.call("send_email", to="a@x.com", body="hi")
        again = self.c.call("send_email", to="a@x.com", body="hi")
        self.assertEqual(again["result"]["content"][0], first["result"]["content"][0])
        self.assertIn("not sent again", again["result"]["content"][-1]["text"])
        self.assertEqual(len(self.ran()), 1)
        self.assertEqual(len(self.ask.asked), 1)
        self.assertEqual([r["decision"] for r in self.log.rows()], ["ask", "deduped"])

    def test_after_an_is_error_the_next_identical_call_asks(self):
        self.start('[tools]\nwrite_draft = "undoable"\n')
        r = self.c.call("write_draft", text="x", fail=True)
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(self.ask.asked, [])  # undoable with no scorer: it acted
        self.c.call("write_draft", text="x", fail=True)
        self.assertEqual(len(self.ask.asked), 1)
        self.assertIn("may already have happened", self.ask.asked[0].why)
        self.assertEqual(len(self.ran()), 2)  # the human approved it, so it ran again

    def test_read_tools_are_never_deduped(self):
        self.start('[tools]\nread_file = "read"\n')
        self.c.call("read_file", path="a")
        self.c.call("read_file", path="a")
        self.assertEqual(len(self.ran()), 2)

    def test_existing_config_is_not_overwritten(self):
        toml = '[tools]\nread_file = "read"\n'
        self.start(toml)
        self.c.request("tools/list")
        with open(self.cfg) as f:
            self.assertEqual(f.read(), toml)


class Config(unittest.TestCase):
    def test_bad_class_fails_loudly(self):
        p = os.path.join(tempfile.mkdtemp(), "kiri.toml")
        with open(p, "w") as f:
            f.write('[tools]\nx = "safe"\n')
        with self.assertRaises(ValueError):
            kconfig.load(p)

    def test_draft_round_trips_odd_names(self):
        p = os.path.join(tempfile.mkdtemp(), "kiri.toml")
        with open(p, "w") as f:
            f.write(kconfig.draft([{"name": "gh.create/issue"}]))
        self.assertEqual(kconfig.load(p).unconfirmed, {"gh.create/issue": "irreversible"})


class Cli(unittest.TestCase):
    def test_cli_end_to_end_times_out_to_deny(self):
        d = tempfile.mkdtemp()
        env = dict(os.environ, FAKE_MCP_CALLS=os.path.join(d, "calls.jsonl"), PYTHONPATH=ROOT)
        with open(os.path.join(d, "kiri.toml"), "w") as f:
            f.write('[tools]\nread_file = "read"\nsend_email = "external"\n')
        p = subprocess.Popen([sys.executable, "-m", "kiri_gate", "mcp", "--port", "0", "--no-open", "--ask-timeout", "1",
                              "--log", "kiri.db", "--", *SERVER], cwd=d, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        c = Client(p.stdin, p.stdout)
        c.request("initialize", {})
        self.assertFalse(c.call("read_file", path="a")["result"].get("isError"))
        r = c.call("send_email", to="a", body="b")["result"]
        self.assertTrue(r["isError"])
        self.assertIn("in time", r["content"][0]["text"])
        p.stdin.close()
        self.assertEqual(p.wait(timeout=10), 0)
        p.stdout.close()
        p.stderr.close()
        with open(os.path.join(d, "calls.jsonl")) as f:
            self.assertEqual([json.loads(l)["name"] for l in f], ["read_file"])
        log = DecisionLog(os.path.join(d, "kiri.db"))
        rows = log.rows()
        self.assertEqual([(r["tool"], r["decision"]) for r in rows], [("read_file", "act"), ("send_email", "ask")])
        log._db.close()


if __name__ == "__main__":
    unittest.main()
