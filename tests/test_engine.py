"""路由引擎场景测试：旧纪元撤销、环路拒绝、断开隔离、选路裁决、
消息纪元核验、恢复回放一致性等。"""

import unittest

from app import engine
from app.drill import parse_all
from app.sample import sample_drill


def make_state(routers, links):
    return engine.initial_state(routers, links)


def simple_links(pairs, pref=100, epoch=1):
    return [{"src": s, "dst": d, "localpref": pref, "epoch": epoch} for s, d in pairs]


def kinds(logs):
    return [l["kind"] for l in logs]


def texts(logs):
    return " | ".join(l["text"] for l in logs)


class LoopRejectionTest(unittest.TestCase):
    """含接收路由器标识的路径必须拒绝传播。"""

    def test_manual_loop_rejected(self):
        st = make_state(["A", "B"], simple_links([("A", "B")]))
        logs = engine.apply_event(st, {
            "type": "announce", "from": "A", "to": "B",
            "prefix": "P", "path": ["B", "A"], "epoch": 1,
        })
        self.assertIn("loop", kinds(logs))
        self.assertIn("环路拒绝", texts(logs))
        self.assertEqual(st["inbound"]["B"], {})

    def test_propagation_loop_blocked_on_ring(self):
        st = make_state(
            ["A", "B", "C"],
            simple_links([("A", "B"), ("B", "C"), ("C", "A")]),
        )
        logs = engine.apply_event(st, {
            "type": "announce", "from": "A", "to": "A", "prefix": "A",
        })
        # A 的通告沿环传播回到 A 时必须被拒绝
        self.assertIn("loop", kinds(logs))
        self.assertEqual(st["inbound"]["A"].get("A", {}).keys(), {""})
        self.assertEqual(st["inbound"]["B"]["A"]["A"]["path"], ["A"])
        self.assertEqual(st["inbound"]["C"]["A"]["B"]["path"], ["A", "B"])

    def test_duplicate_router_in_path_rejected(self):
        st = make_state(["A", "B", "C"], simple_links([("C", "B")]))
        logs = engine.apply_event(st, {
            "type": "announce", "from": "C", "to": "B",
            "prefix": "P", "path": ["A", "C", "A", "C"], "epoch": 1,
        })
        self.assertIn("loop", kinds(logs))
        self.assertEqual(st["inbound"]["B"], {})


class StaleEpochTest(unittest.TestCase):
    """旧纪元撤销 / 通告忽略。"""

    def _state_with_reconnect(self):
        st = make_state(["A", "B"], simple_links([("A", "B")]))
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        engine.apply_event(st, {"type": "disconnect", "from": "A", "to": "B"})
        engine.apply_event(st, {"type": "reconnect", "from": "A", "to": "B"})
        return st  # 邻接 A->B 现为纪元 2，B 持有纪元 2 的路由

    def test_stale_withdraw_ignored(self):
        st = self._state_with_reconnect()
        self.assertEqual(st["inbound"]["B"]["A"]["A"]["epoch"], 2)
        logs = engine.apply_event(st, {
            "type": "withdraw", "from": "A", "to": "B", "prefix": "A", "epoch": 1,
        })
        self.assertIn("stale_withdraw", kinds(logs))
        self.assertIn("旧纪元撤销忽略", texts(logs))
        # 旧纪元撤销不得移除新纪元的通告
        self.assertEqual(st["inbound"]["B"]["A"]["A"]["epoch"], 2)
        best = engine.best_path(st, "B", "A")
        self.assertIsNotNone(best)
        self.assertEqual(best["neighbor"], "A")

    def test_current_epoch_withdraw_applies(self):
        st = self._state_with_reconnect()
        logs = engine.apply_event(st, {
            "type": "withdraw", "from": "A", "to": "B", "prefix": "A", "epoch": 2,
        })
        self.assertIn("withdraw", kinds(logs))
        self.assertNotIn("A", st["inbound"]["B"].get("A", {}))
        self.assertIsNone(engine.best_path(st, "B", "A"))

    def test_stale_announce_ignored(self):
        st = self._state_with_reconnect()
        logs = engine.apply_event(st, {
            "type": "announce", "from": "A", "to": "B",
            "prefix": "X", "path": ["A"], "epoch": 1,
        })
        self.assertIn("stale", kinds(logs))
        self.assertIn("过期通告忽略", texts(logs))
        self.assertNotIn("X", st["inbound"]["B"])

    def test_reconnect_bumps_epoch_and_resends(self):
        st = make_state(["A", "B"], simple_links([("A", "B")]))
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        engine.apply_event(st, {"type": "disconnect", "from": "A", "to": "B"})
        self.assertNotIn("A", st["inbound"]["B"])
        logs = engine.apply_event(st, {"type": "reconnect", "from": "A", "to": "B"})
        self.assertEqual(st["adj"]["A->B"]["epoch"], 2)
        self.assertIn("resend", kinds(logs))
        self.assertEqual(st["inbound"]["B"]["A"]["A"]["epoch"], 2)


class WithdrawScopeTest(unittest.TestCase):
    """撤销仅移除该邻居同一前缀的对应通告。"""

    def test_withdraw_only_matching_neighbor_and_prefix(self):
        st = make_state(
            ["A", "B", "C"],
            simple_links([("A", "B"), ("C", "B")]),
        )
        engine.apply_event(st, {
            "type": "announce", "from": "A", "to": "B", "prefix": "P", "path": ["A"], "epoch": 1,
        })
        engine.apply_event(st, {
            "type": "announce", "from": "C", "to": "B", "prefix": "P", "path": ["C"], "epoch": 1,
        })
        engine.apply_event(st, {
            "type": "announce", "from": "A", "to": "B", "prefix": "Q", "path": ["A"], "epoch": 1,
        })
        engine.apply_event(st, {
            "type": "withdraw", "from": "A", "to": "B", "prefix": "P", "epoch": 1,
        })
        routes = st["inbound"]["B"]
        self.assertNotIn("A", routes["P"])          # 对应通告被移除
        self.assertIn("C", routes["P"])             # 其他邻居同前缀保留
        self.assertIn("A", routes["Q"])             # 同邻居其他前缀保留
        best = engine.best_path(st, "B", "P")
        self.assertEqual(best["neighbor"], "C")     # 最优回退到 C


class DisconnectTest(unittest.TestCase):
    """已断开邻居不再参与选路。"""

    def test_disconnect_removes_neighbor_routes(self):
        st = make_state(
            ["A", "B", "C"],
            simple_links([("A", "B"), ("C", "B")]),
        )
        engine.apply_event(st, {
            "type": "announce", "from": "A", "to": "B", "prefix": "P", "path": ["A"], "epoch": 1,
        })
        engine.apply_event(st, {
            "type": "announce", "from": "C", "to": "B", "prefix": "P", "path": ["C"], "epoch": 1,
        })
        self.assertEqual(engine.best_path(st, "B", "P")["neighbor"], "A")
        logs = engine.apply_event(st, {"type": "disconnect", "from": "A", "to": "B"})
        self.assertIn("disconnect", kinds(logs))
        self.assertNotIn("A", st["inbound"]["B"]["P"])
        self.assertEqual(engine.best_path(st, "B", "P")["neighbor"], "C")
        # 断开邻接上的后续通告一律忽略
        logs = engine.apply_event(st, {
            "type": "announce", "from": "A", "to": "B", "prefix": "P", "path": ["A"], "epoch": 1,
        })
        self.assertIn("ignored", kinds(logs))
        self.assertNotIn("A", st["inbound"]["B"]["P"])


class BestPathTest(unittest.TestCase):
    """最优路径按本地偏好、路径长度、下一跳标识稳定裁决。"""

    def _three_neighbor_state(self):
        return make_state(
            ["R", "X", "Y", "Z"],
            [
                {"src": "X", "dst": "R", "localpref": 100, "epoch": 1},
                {"src": "Y", "dst": "R", "localpref": 100, "epoch": 1},
                {"src": "Z", "dst": "R", "localpref": 200, "epoch": 1},
            ],
        )

    def test_localpref_wins(self):
        st = self._three_neighbor_state()
        engine.apply_event(st, {"type": "announce", "from": "X", "to": "R",
                                "prefix": "P", "path": ["X"], "epoch": 1})
        engine.apply_event(st, {"type": "announce", "from": "Z", "to": "R",
                                "prefix": "P", "path": ["Q", "Z"], "epoch": 1})
        # Z 路径更长但本地偏好更高
        self.assertEqual(engine.best_path(st, "R", "P")["neighbor"], "Z")

    def test_shorter_path_wins_on_tie(self):
        st = self._three_neighbor_state()
        engine.apply_event(st, {"type": "announce", "from": "X", "to": "R",
                                "prefix": "P", "path": ["X"], "epoch": 1})
        engine.apply_event(st, {"type": "announce", "from": "Y", "to": "R",
                                "prefix": "P", "path": ["Q", "Y"], "epoch": 1})
        self.assertEqual(engine.best_path(st, "R", "P")["neighbor"], "X")

    def test_nexthop_id_breaks_tie(self):
        st = self._three_neighbor_state()
        engine.apply_event(st, {"type": "announce", "from": "Y", "to": "R",
                                "prefix": "P", "path": ["Y"], "epoch": 1})
        engine.apply_event(st, {"type": "announce", "from": "X", "to": "R",
                                "prefix": "P", "path": ["X"], "epoch": 1})
        self.assertEqual(engine.best_path(st, "R", "P")["neighbor"], "X")
        # 撤销 X 后稳定回退到 Y
        engine.apply_event(st, {"type": "withdraw", "from": "X", "to": "R",
                                "prefix": "P", "epoch": 1})
        self.assertEqual(engine.best_path(st, "R", "P")["neighbor"], "Y")


class MessageTest(unittest.TestCase):
    """消息投递的纪元核验与过期忽略。"""

    def _line_state(self):
        st = make_state(
            ["A", "B", "C"],
            simple_links([("A", "B"), ("B", "C")]),
        )
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        return st

    def test_delivered_along_best_path(self):
        st = self._line_state()
        logs = engine.apply_event(st, {"type": "deliver", "src": "C", "dst": "A", "msg": "hi"})
        self.assertIn("delivered", kinds(logs))
        msg = st["messages"][0]
        self.assertEqual(msg["status"], "delivered")
        self.assertEqual(msg["hops"], ["C", "B", "A"])

    def test_pending_when_no_route_then_expired_after_epoch_change(self):
        st = make_state(
            ["A", "B"],
            simple_links([("A", "B")]),
        )
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        engine.apply_event(st, {"type": "disconnect", "from": "A", "to": "B"})
        engine.apply_event(st, {"type": "deliver", "src": "B", "dst": "A", "msg": "m"})
        msg = st["messages"][0]
        self.assertEqual(msg["status"], "pending")  # 无路由，滞留待投递
        logs = engine.apply_event(st, {"type": "reconnect", "from": "A", "to": "B"})
        # 重连后纪元 1→2，滞留消息过期忽略
        self.assertIn("expired", kinds(logs))
        self.assertIn("过期消息忽略", texts(logs))
        self.assertEqual(msg["status"], "expired")

    def test_delivery_ok_when_epoch_unchanged(self):
        st = self._line_state()
        engine.apply_event(st, {"type": "deliver", "src": "C", "dst": "A", "msg": "m"})
        # 无拓扑变化时后续事件不影响已投递消息，也不产生过期
        logs = engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        self.assertNotIn("expired", kinds(logs))
        self.assertEqual(st["messages"][0]["status"], "delivered")


class RecoveryConvergenceTest(unittest.TestCase):
    """恢复回放与不中断回放一致。"""

    def test_stepwise_snapshots_match_fresh_replay(self):
        spec, errors = parse_all(**sample_drill())
        self.assertEqual(errors, [])
        _, steps = engine.run_script(spec["routers"], spec["links"], spec["events"])
        self.assertEqual(len(steps), len(spec["events"]) + 1)
        for k in range(len(steps)):
            state, _ = engine.run_script(spec["routers"], spec["links"], spec["events"], upto=k)
            self.assertEqual(
                engine.canonical(state), engine.canonical(steps[k]["state"]),
                "第 {} 步检查点与不中断回放不一致".format(k),
            )

    def test_sample_covers_required_displays(self):
        spec, errors = parse_all(**sample_drill())
        self.assertEqual(errors, [])
        _, steps = engine.run_script(spec["routers"], spec["links"], spec["events"])
        all_kinds = set()
        for step in steps:
            all_kinds.update(kinds(step["logs"]))
        self.assertIn("loop", all_kinds)            # 环路拒绝
        self.assertIn("stale", all_kinds)           # 过期通告忽略
        self.assertIn("stale_withdraw", all_kinds)  # 旧纪元撤销忽略
        self.assertIn("expired", all_kinds)         # 过期消息忽略
        self.assertIn("delivered", all_kinds)       # 正常投递
        final = steps[-1]["state"]
        # 旧纪元撤销被忽略：C 仍持有 D 的纪元 2 路由
        self.assertEqual(final["inbound"]["C"]["D"]["D"]["epoch"], 2)
        best = engine.best_path(final, "C", "D")
        self.assertEqual(best["neighbor"], "D")


class MessageTraceTest(unittest.TestCase):
    """消息首次入队后保留稳定追溯：逐跳裁决、旧尝试原样保留、按步骤追加。"""

    def _line_topology(self):
        return make_state(
            ["A", "B", "C"],
            simple_links([("A", "B"), ("B", "C")]),
        )

    def test_delivered_trace_lists_every_hop_verdict(self):
        st = self._line_topology()
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        engine.apply_event(st, {"type": "deliver", "src": "C", "dst": "A", "msg": "m"})
        msg = st["messages"][0]
        self.assertEqual(len(msg["attempts"]), 1)
        rec = msg["attempts"][0]
        self.assertEqual(rec["step"], 2)
        self.assertEqual(rec["verdict"], "delivered")
        self.assertEqual(rec["event"]["type"], "deliver")
        results = [(h["from"], h["to"], h["result"]) for h in rec["hops"]]
        self.assertEqual(results, [
            ("C", "B", "forwarded"),
            ("B", "A", "forwarded"),
            ("A", "A", "arrived"),
        ])
        hop = rec["hops"][0]
        self.assertEqual(hop["prefix"], "A")
        self.assertEqual(hop["best_path"], ["A", "B"])
        self.assertEqual(hop["localpref"], 100)
        self.assertEqual(hop["send_epoch"], 1)
        self.assertTrue(hop["reason"])

    def test_old_attempt_kept_new_attempt_appended_after_route_restore(self):
        st = self._line_topology()
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        # A 撤销始发：C 无到 A 的路由，消息滞留
        engine.apply_event(st, {"type": "withdraw", "from": "A", "to": "A", "prefix": "A"})
        engine.apply_event(st, {"type": "deliver", "src": "C", "dst": "A", "msg": "m"})
        msg = st["messages"][0]
        self.assertEqual(msg["status"], "pending")
        self.assertEqual([a["verdict"] for a in msg["attempts"]], ["pending"])
        old = msg["attempts"][0]
        self.assertEqual(old["hops"][0]["result"], "stranded")
        self.assertIn("无到 A 的可用路由", old["stop_reason"])
        # A 重新始发（链路纪元不变）：新尝试按发生步骤追加并投递成功
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        self.assertEqual(msg["status"], "delivered")
        self.assertEqual([a["step"] for a in msg["attempts"]], [3, 4])
        self.assertEqual([a["verdict"] for a in msg["attempts"]], ["pending", "delivered"])
        # 旧尝试保持原样：仍是滞留裁决，hops 不被补写
        self.assertEqual(old["verdict"], "pending")
        self.assertEqual(len(old["hops"]), 1)
        self.assertEqual(old["hops"][0]["result"], "stranded")
        new = msg["attempts"][1]
        self.assertEqual([h["result"] for h in new["hops"]],
                         ["forwarded", "forwarded", "arrived"])
        self.assertEqual(msg["hops"], ["C", "B", "A"])

    def test_expired_attempt_records_epochs_and_stop_reason(self):
        st = make_state(["A", "B"], simple_links([("A", "B")]))
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        engine.apply_event(st, {"type": "disconnect", "from": "A", "to": "B"})
        engine.apply_event(st, {"type": "deliver", "src": "B", "dst": "A", "msg": "m"})
        engine.apply_event(st, {"type": "reconnect", "from": "A", "to": "B"})
        msg = st["messages"][0]
        self.assertEqual([a["verdict"] for a in msg["attempts"]], ["pending", "expired"])
        hop = msg["attempts"][1]["hops"][0]
        self.assertEqual(hop["result"], "expired")
        self.assertEqual(hop["send_epoch"], 2)
        self.assertEqual(hop["best_path"], ["A"])
        self.assertIn("纪元 1 → 2", hop["reason"])
        self.assertIn("纪元 1 → 2", msg["attempts"][1]["stop_reason"])

    def test_no_duplicate_trace_record_within_same_step(self):
        st = self._line_topology()
        engine.apply_event(st, {"type": "announce", "from": "A", "to": "A", "prefix": "A"})
        engine.apply_event(st, {"type": "deliver", "src": "C", "dst": "A", "msg": "m"})
        # 再造一条滞留消息，并在同一事件步骤内重复触发自动重试
        engine.apply_event(st, {"type": "disconnect", "from": "A", "to": "B"})
        engine.apply_event(st, {"type": "deliver", "src": "C", "dst": "X", "msg": "m2"})
        msg = st["messages"][1]
        before = len(msg["attempts"])
        engine._attempt_deliveries(st, [])
        engine._attempt_deliveries(st, [])
        self.assertEqual(len(msg["attempts"]), before)  # 同一步骤不重复记录
        self.assertEqual(len(msg["attempts"]), 1)
        # 进入下一事件步骤后允许再次尝试并追加新记录
        engine.apply_event(st, {"type": "reconnect", "from": "A", "to": "B"})
        self.assertEqual(len(msg["attempts"]), 2)

    def test_trace_identical_between_interrupted_and_uninterrupted_runs(self):
        spec, errors = parse_all(**sample_drill())
        self.assertEqual(errors, [])
        full_state, _ = engine.run_script(spec["routers"], spec["links"], spec["events"])
        # 任意断点恢复后续跑，追溯记录须与不中断执行逐条一致
        for k in range(0, len(spec["events"]) + 1):
            state, _ = engine.run_script(spec["routers"], spec["links"], spec["events"], upto=k)
            for ev in spec["events"][k:]:
                engine.apply_event(state, ev)
            self.assertEqual(
                engine.canonical(state["messages"]),
                engine.canonical(full_state["messages"]),
                "断点 {} 恢复后消息追溯与不中断执行不一致".format(k),
            )


if __name__ == "__main__":
    unittest.main()
