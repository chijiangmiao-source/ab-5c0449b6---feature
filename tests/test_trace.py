"""消息追溯记录测试：逐跳裁决、追加式追溯、同步去重、恢复一致与查询隔离。"""

import copy
import tempfile
import time
import unittest

from app import engine
from app.drill import parse_all
from app.runner import RunManager
from app.sample import sample_drill


def make_state(routers, links):
    return engine.initial_state(routers, links)


def simple_links(pairs, pref=100, epoch=1):
    return [{"src": s, "dst": d, "localpref": pref, "epoch": epoch} for s, d in pairs]


def sample_spec():
    spec, errors = parse_all(**sample_drill())
    assert not errors
    return spec


def wait_done(manager, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = manager.current()["run"]
        if cur and cur["done"]:
            return cur
        time.sleep(0.02)
    raise AssertionError("演练未在时限内完成")


class TraceRecordTest(unittest.TestCase):
    """每次实际尝试记录发送/接收路由器、前缀与最优路径、发送纪元与裁决。"""

    def test_delivered_message_records_each_hop(self):
        st = make_state(
            ["A", "B", "C"],
            simple_links([("A", "B"), ("B", "C")]),
        )
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        engine.apply_event(st, {"type": "deliver", "src": "C", "dst": "A", "msg": "hi"})
        msg = st["messages"][0]
        self.assertEqual(msg["status"], "delivered")
        self.assertEqual(len(msg["trace"]), 1)
        attempt = msg["trace"][0]
        self.assertEqual(attempt["step"], 2)  # 第二个事件步骤
        self.assertEqual(attempt["outcome"], "delivered")
        self.assertEqual(len(attempt["hops"]), 2)
        hop1, hop2 = attempt["hops"]
        # 每跳列出发送与接收路由器、采用的前缀与最优路径、发送纪元与裁决
        self.assertEqual((hop1["from"], hop1["to"]), ("C", "B"))
        self.assertEqual(hop1["prefix"], "A")
        self.assertEqual(hop1["path"], ["A", "B"])
        self.assertEqual(hop1["epoch"], 1)
        self.assertEqual(hop1["current_epoch"], 1)
        self.assertEqual(hop1["verdict"], "forward")
        self.assertEqual((hop2["from"], hop2["to"]), ("B", "A"))
        self.assertEqual(hop2["path"], ["A"])
        self.assertEqual(hop2["verdict"], "forward")

    def test_no_route_attempt_records_reason(self):
        st = make_state(["A", "B"], simple_links([("A", "B")]))
        engine.apply_event(st, {"type": "deliver", "src": "B", "dst": "A", "msg": "m"})
        msg = st["messages"][0]
        self.assertEqual(msg["status"], "pending")
        self.assertEqual(len(msg["trace"]), 1)
        attempt = msg["trace"][0]
        self.assertEqual(attempt["outcome"], "pending")
        hop = attempt["hops"][0]
        self.assertEqual(hop["from"], "B")
        self.assertIsNone(hop["to"])
        self.assertEqual(hop["verdict"], "no_route")
        self.assertIn("无到 A 的可用路由", hop["reason"])


class TraceAppendTest(unittest.TestCase):
    """重连或最优路径改变后再次尝试：旧尝试保持原样，新尝试按步骤追加。"""

    def _pending_then_expired(self):
        st = make_state(["A", "B"], simple_links([("A", "B")]))
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        engine.apply_event(st, {"type": "disconnect", "from": "A", "to": "B"})
        engine.apply_event(st, {"type": "deliver", "src": "B", "dst": "A", "msg": "m"})
        return st

    def test_retry_appends_and_preserves_old_attempts(self):
        st = self._pending_then_expired()
        msg = st["messages"][0]
        self.assertEqual(len(msg["trace"]), 1)
        before = copy.deepcopy(msg["trace"])
        engine.apply_event(st, {"type": "reconnect", "from": "A", "to": "B"})
        self.assertEqual(msg["status"], "expired")
        self.assertEqual(len(msg["trace"]), 2)
        # 旧尝试保持原样
        self.assertEqual(msg["trace"][0], before[0])
        # 新尝试按发生步骤追加
        self.assertEqual(msg["trace"][1]["step"], 4)
        self.assertEqual(msg["trace"][1]["outcome"], "expired")
        hop = msg["trace"][1]["hops"][0]
        self.assertEqual((hop["from"], hop["to"]), ("B", "A"))
        self.assertEqual(hop["epoch"], 1)          # 入队快照中的发送纪元
        self.assertEqual(hop["current_epoch"], 2)  # 重连后的当前纪元
        self.assertEqual(hop["verdict"], "expired")
        self.assertIn("过期", hop["reason"])

    def test_second_attempt_uses_then_current_best_path(self):
        st = make_state(
            ["A", "B", "C"],
            simple_links([("A", "B"), ("B", "C")]),
        )
        # 第一次尝试：无任何路由，滞留
        engine.apply_event(st, {"type": "deliver", "src": "C", "dst": "A", "msg": "m"})
        msg = st["messages"][0]
        self.assertEqual(msg["trace"][0]["hops"][0]["verdict"], "no_route")
        # 路由出现后再次尝试：记录当时采用的最优路径
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        self.assertEqual(msg["status"], "delivered")
        self.assertEqual(len(msg["trace"]), 2)
        attempt = msg["trace"][1]
        self.assertEqual(attempt["step"], 2)
        self.assertEqual([h["to"] for h in attempt["hops"]], ["B", "A"])
        self.assertEqual(attempt["hops"][0]["path"], ["A", "B"])
        self.assertEqual(attempt["hops"][1]["path"], ["A"])

    def test_no_duplicate_attempt_within_same_step(self):
        st = self._pending_then_expired()
        msg = st["messages"][0]
        self.assertEqual(len(msg["trace"]), 1)
        # 同一事件步骤内的自动重试不得生成重复记录
        engine._attempt_deliveries(st, [])
        engine._attempt_deliveries(st, [])
        self.assertEqual(len(msg["trace"]), 1)
        # 进入下一步骤后才会追加新记录
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        self.assertEqual(len(msg["trace"]), 2)
        self.assertEqual(msg["trace"][1]["step"], 4)


class TraceRecoveryTest(unittest.TestCase):
    """页面重开 / 检查点恢复后，追溯顺序、内容及最终结果与不中断执行一致。"""

    def test_restored_traces_match_uninterrupted_replay(self):
        spec = sample_spec()
        with tempfile.TemporaryDirectory() as tmp:
            m1 = RunManager(tmp, step_delay=0.01)
            m1.create(spec["routers"], spec["links"], spec["events"])
            run = wait_done(m1)
            self.assertTrue(run["converged"])
            # 模拟页面重开 / 服务重启：从检查点恢复
            m2 = RunManager(tmp, step_delay=0.01)
            restored = m2.current()["run"]
            self.assertTrue(restored["done"])
            # 不中断回放得到的追溯记录
            final_state, _ = engine.run_script(spec["routers"], spec["links"], spec["events"])
            expected = {m["id"]: m["trace"] for m in final_state["messages"]}
            actual = {m["id"]: m["trace"] for m in restored["steps"][-1]["state"]["messages"]}
            self.assertEqual(actual, expected)
            # 示例演练中滞留后过期的消息应有两条尝试记录
            self.assertEqual([a["outcome"] for a in actual[3]], ["pending", "expired"])

    def test_resumed_run_traces_match_uninterrupted_replay(self):
        spec = sample_spec()
        with tempfile.TemporaryDirectory() as tmp:
            m1 = RunManager(tmp, step_delay=0.05)
            m1.create(spec["routers"], spec["links"], spec["events"])
            deadline = time.time() + 10
            while time.time() < deadline:
                cur = m1.current()["run"]
                if cur and cur["completed_steps"] >= cur["total"] // 2:
                    break
                time.sleep(0.02)
            m1.shutdown()  # 模拟宕机
            time.sleep(0.1)
            m2 = RunManager(tmp, step_delay=0.01)
            run = wait_done(m2)
            self.assertTrue(run["converged"])
            final_state, _ = engine.run_script(spec["routers"], spec["links"], spec["events"])
            expected = {m["id"]: m["trace"] for m in final_state["messages"]}
            actual = {m["id"]: m["trace"] for m in run["steps"][-1]["state"]["messages"]}
            self.assertEqual(actual, expected)
            m2.shutdown()


class TraceQueryTest(unittest.TestCase):
    """按标识查询追溯：不存在的消息返回空，不泄露其他演练记录。"""

    def test_message_trace_lookup(self):
        spec = sample_spec()
        with tempfile.TemporaryDirectory() as tmp:
            m = RunManager(tmp, step_delay=0.01)
            m.create(spec["routers"], spec["links"], spec["events"])
            run = wait_done(m)
            ids = [msg["id"] for msg in run["steps"][-1]["state"]["messages"]]
            self.assertTrue(ids)
            found = m.message_trace(ids[0])
            self.assertIsNotNone(found)
            self.assertEqual(found["id"], ids[0])
            self.assertIn("trace", found)
            # 不存在的消息标识
            self.assertIsNone(m.message_trace(9999))
            m.shutdown()

    def test_no_run_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = RunManager(tmp, step_delay=0.01)
            self.assertIsNone(m.message_trace(1))

    def test_other_run_ids_not_visible(self):
        spec = sample_spec()
        with tempfile.TemporaryDirectory() as tmp:
            m = RunManager(tmp, step_delay=0.01)
            first = m.create(spec["routers"], spec["links"], spec["events"])
            wait_done(m)
            # 新演练（无 deliver 事件）取代旧演练
            routers = ["A", "B"]
            links = [{"src": "A", "dst": "B", "localpref": 100, "epoch": 1}]
            events = [{"type": "announce", "from": "A", "to": "A", "prefix": "A"}]
            second = m.create(routers, links, events)
            wait_done(m)
            self.assertNotEqual(first["run_id"], second["run_id"])
            # 旧演练的消息标识在新演练中不可见
            self.assertIsNone(m.message_trace(1))
            m.shutdown()


if __name__ == "__main__":
    unittest.main()
