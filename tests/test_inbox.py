import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from kiri_gate import EXTERNAL, DecisionLog, Request
from kiri_gate.inbox import TIMED_OUT, Approver
from kiri_gate.mcp import Proxy
from tests.test_mcp import SERVER, Client


def http(url, body=None, headers=None):
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=h,
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read()) if "json" in r.headers.get("Content-Type", "") else r.read()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_pending(url, n=1):
    for _ in range(100):
        p = http(url + "/api/state")[1]["pending"]
        if len(p) >= n:
            return p
        time.sleep(0.05)
    raise AssertionError("nothing showed up on the page")


REQ = Request("send_email", EXTERNAL, {"to": "recruiter@x.com"}, "Reply to the recruiter", "external actions always ask")


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.ap = Approver(port=0, open_browser=False)
        self.assertEqual(self.ap.ensure(), "host")
        self.url = self.ap.url

    def tearDown(self):
        self.ap.close()

    def ask_async(self, ap, deadline_in=10, key="k1"):
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("a", ap.ask(REQ, key, time.time() + deadline_in, "fake")))
        t.start()
        return t, out

    def test_page_shows_the_request_and_edited_args_come_back(self):
        t, out = self.ask_async(self.ap)
        item = wait_pending(self.url)[0]
        self.assertEqual((item["tool"], item["cls"], item["goal"], item["source"]),
                         ("send_email", "external", "Reply to the recruiter", "fake"))
        self.assertEqual(http(self.url + "/api/decide", {"id": "k1", "approve": True, "args": {"to": "me@x.com"}})[0], 200)
        t.join(5)
        self.assertTrue(out["a"].approve)
        self.assertEqual(out["a"].args, {"to": "me@x.com"})
        self.assertEqual(http(self.url + "/api/state")[1]["pending"], [])

    def test_no_answer_before_the_deadline_means_deny(self):
        t0 = time.time()
        a = self.ap.ask(REQ, "k2", time.time() + 0.4)
        self.assertFalse(a.approve)
        self.assertEqual(a.note, TIMED_OUT)
        self.assertLess(time.time() - t0, 2)
        code, body = http(self.url + "/api/decide", {"id": "k2", "approve": True})
        self.assertEqual(code, 409)  # too late: nothing can approve it now

    def test_only_true_approves(self):
        t, out = self.ask_async(self.ap)
        wait_pending(self.url)
        http(self.url + "/api/decide", {"id": "k1", "approve": "yes"})
        t.join(5)
        self.assertFalse(out["a"].approve)

    def test_other_sites_cannot_decide(self):
        t, out = self.ask_async(self.ap, deadline_in=1)
        wait_pending(self.url)
        port = self.ap.port
        self.assertEqual(http(self.url + "/api/decide", {"id": "k1", "approve": True},
                              {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(http(self.url + "/api/decide", {"id": "k1", "approve": True},
                              {"Host": f"evil.example:{port}"})[0], 403)
        req = urllib.request.Request(self.url + "/api/decide", data=b'{"id":"k1","approve":true}',
                                     headers={"Content-Type": "text/plain"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 415)
        e.exception.close()
        t.join(5)
        self.assertFalse(out["a"].approve)

    def test_second_proxy_uses_the_first_ones_page(self):
        other = Approver(port=self.ap.port, open_browser=False)
        self.assertEqual(other.ensure(), "remote")
        t, out = self.ask_async(other, key="k3")
        wait_pending(self.url)
        http(self.url + "/api/decide", {"id": "k3", "approve": False, "note": "no"})
        t.join(5)
        self.assertFalse(out["a"].approve)
        self.assertEqual(out["a"].note, "no")

    def test_page_and_log_are_served(self):
        code, html = http(self.url + "/")
        self.assertEqual(code, 200)
        self.assertIn(b"kiri-gate", html)
        self.assertEqual(http(self.url + "/api/log")[1], {"rows": []})


class ProxyWithPage(unittest.TestCase):
    def start(self, timeout=10.0):
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "kiri.toml")
        with open(cfg, "w") as f:
            f.write('[tools]\nsend_email = "external"\n')
        self.log_path = os.path.join(d, "k.db")
        self.log = DecisionLog(self.log_path)
        self.ap = Approver(port=0, log_path=self.log_path, open_browser=False)
        self.ap.ensure()
        c_r, p_w = os.pipe()
        p_r, c_w = os.pipe()
        self.proxy = Proxy(SERVER, config_path=cfg, log=self.log, approver=self.ap, ask_timeout=timeout,
                           stdin=os.fdopen(p_r, "rb"), stdout=os.fdopen(p_w, "wb"))
        self.to_proxy, self.from_proxy = os.fdopen(c_w, "wb"), os.fdopen(c_r, "rb")
        self.t = threading.Thread(target=self.proxy.run, daemon=True)
        self.t.start()
        self.c = Client(self.to_proxy, self.from_proxy)

    def tearDown(self):
        self.to_proxy.close()
        self.t.join(10)
        self.proxy.cin.close()
        self.proxy.cout.close()
        self.from_proxy.close()
        self.log._db.close()

    def test_denied_on_the_page(self):
        self.start()
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", self.c.call("send_email", to="a", body="b")))
        t.start()
        item = wait_pending(self.ap.url)[0]
        self.assertEqual(item["source"], "fake_mcp_server.py")
        http(self.ap.url + "/api/decide", {"id": item["id"], "approve": False, "note": "not yet"})
        t.join(5)
        self.assertIn("not yet", out["r"]["result"]["content"][0]["text"])
        rows = http(self.ap.url + "/api/log")[1]["rows"]
        self.assertEqual((rows[0]["tool"], rows[0]["approved"], rows[0]["note"]), ("send_email", 0, "not yet"))

    def test_timeout_denies_with_a_clear_message_and_logs_it(self):
        self.start(timeout=0.5)
        r = self.c.call("send_email", to="a", body="b")
        text = r["result"]["content"][0]["text"]
        self.assertTrue(r["result"]["isError"])
        self.assertIn("in time", text)
        self.assertIn(self.ap.url, text)
        self.assertEqual(self.log.rows()[0]["note"], TIMED_OUT)

    def test_client_cancel_clears_the_page_and_sends_no_reply(self):
        self.start()
        self.c.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                     "params": {"name": "send_email", "arguments": {"to": "a"}}})
        wait_pending(self.ap.url)
        self.c.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}})
        for _ in range(100):
            if not http(self.ap.url + "/api/state")[1]["pending"]:
                break
            time.sleep(0.05)
        self.assertEqual(http(self.ap.url + "/api/state")[1]["pending"], [])
        self.assertEqual(self.c.request("ping")["result"], {})
        self.assertTrue(self.c.q.empty())  # nothing was sent for id 7


class PageAtStart(unittest.TestCase):
    def test_page_is_up_before_anything_asks(self):
        ap = Approver(port=0, open_browser=False)
        c_r, p_w = os.pipe()
        p_r, c_w = os.pipe()
        proxy = Proxy(SERVER, approver=ap, config_path=os.path.join(tempfile.mkdtemp(), "k.toml"),
                      stdin=os.fdopen(p_r, "rb"), stdout=os.fdopen(p_w, "wb"))
        t = threading.Thread(target=proxy.run, daemon=True)
        t.start()
        for _ in range(100):
            if ap.inbox is not None:
                break
            time.sleep(0.05)
        self.assertEqual(http(ap.url + "/api/state")[1]["pending"], [])
        os.close(c_w)
        t.join(10)
        proxy.cin.close()
        proxy.cout.close()
        os.close(c_r)


if __name__ == "__main__":
    unittest.main()
