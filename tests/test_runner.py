"""运行管理器测试：检查点持久化、恢复续算、迟到结果隔离、收敛校验。"""

import tempfile
import time
import unittest

from app.drill import parse_all
from app.runner import RunManager
from app.sample import sample_drill


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


class CheckpointRestoreTest(unittest.TestCase):
    def test_restore_last_complete_steps_after_restart(self):
        spec = sample_spec()
        with tempfile.TemporaryDirectory() as tmp:
            m1 = RunManager(tmp, step_delay=0.01)
            m1.create(spec["routers"], spec["links"], spec["events"])
            run = wait_done(m1)
            self.assertTrue(run["converged"])
            self.assertEqual(run["completed_steps"], run["total"])
            # 模拟服务重启：新管理器从检查点恢复最后完整步骤
            m2 = RunManager(tmp, step_delay=0.01)
            restored = m2.current()["run"]
            self.assertIsNotNone(restored)
            self.assertEqual(restored["run_id"], run["run_id"])
            self.assertTrue(restored["done"])
            self.assertEqual(restored["completed_steps"], run["total"])
            self.assertEqual(len(restored["steps"]), run["total"] + 1)
            self.assertTrue(restored["converged"])

    def test_resume_unfinished_run(self):
        spec = sample_spec()
        with tempfile.TemporaryDirectory() as tmp:
            m1 = RunManager(tmp, step_delay=0.05)
            m1.create(spec["routers"], spec["links"], spec["events"])
            # 等到完成约一半后“宕机”
            deadline = time.time() + 10
            while time.time() < deadline:
                cur = m1.current()["run"]
                if cur and cur["completed_steps"] >= cur["total"] // 2:
                    break
                time.sleep(0.02)
            partial = m1.current()["run"]["completed_steps"]
            self.assertGreaterEqual(partial, 1)
            m1.shutdown()  # 模拟宕机：停止旧管理器的后台线程
            time.sleep(0.1)
            # 新管理器从断点继续执行剩余事件
            m2 = RunManager(tmp, step_delay=0.01)
            run = wait_done(m2)
            self.assertEqual(run["completed_steps"], run["total"])
            self.assertTrue(run["converged"])
            m2.shutdown()


class LateResultIsolationTest(unittest.TestCase):
    def test_superseded_run_cannot_overwrite(self):
        routers = ["A", "B"]
        links = [{"src": "A", "dst": "B", "localpref": 100, "epoch": 1}]
        many_events = [
            {"type": "announce", "from": "A", "to": "A", "prefix": "P%d" % i, "path": ["A"], "epoch": 1}
            for i in range(20)
        ]
        one_event = [
            {"type": "announce", "from": "A", "to": "A", "prefix": "ONLY", "path": ["A"], "epoch": 1}
        ]
        with tempfile.TemporaryDirectory() as tmp:
            m = RunManager(tmp, step_delay=0.05)
            first = m.create(routers, links, many_events)
            second = m.create(routers, links, one_event)  # 立即开启新演练
            run = wait_done(m)
            self.assertEqual(run["run_id"], second["run_id"])
            self.assertNotEqual(run["run_id"], first["run_id"])
            self.assertEqual(run["total"], 1)
            # 旧演练若继续产出步骤即为迟到结果，等待远超其所需时间后复查
            time.sleep(0.05 * 25)
            run = m.current()["run"]
            self.assertEqual(run["run_id"], second["run_id"])
            self.assertEqual(run["completed_steps"], 1)
            self.assertEqual(len(run["steps"]), 2)
            self.assertTrue(run["converged"])


class ConvergenceFlagTest(unittest.TestCase):
    def test_sample_run_converges(self):
        spec = sample_spec()
        with tempfile.TemporaryDirectory() as tmp:
            m = RunManager(tmp, step_delay=0.01)
            m.create(spec["routers"], spec["links"], spec["events"])
            run = wait_done(m)
            self.assertTrue(run["converged"])
            all_kinds = set()
            for step in run["steps"]:
                all_kinds.update(log["kind"] for log in step["logs"])
            for required in ("loop", "stale", "stale_withdraw", "expired", "delivered"):
                self.assertIn(required, all_kinds)


class MessageTraceQueryTest(unittest.TestCase):
    def test_trace_returned_for_current_run_message(self):
        spec = sample_spec()
        with tempfile.TemporaryDirectory() as tmp:
            m = RunManager(tmp, step_delay=0.01)
            m.create(spec["routers"], spec["links"], spec["events"])
            wait_done(m)
            trace, err = m.message_trace(1)
            self.assertIsNone(err)
            self.assertEqual(trace["message"]["id"], 1)
            self.assertGreaterEqual(trace["attempt_count"], 1)
            # 滞留后恢复的消息含两条按步骤排列的尝试
            trace5, err = m.message_trace(5)
            self.assertIsNone(err)
            verdicts = [a["verdict"] for a in trace5["message"]["attempts"]]
            self.assertEqual(verdicts, ["pending", "delivered"])
            steps = [a["step"] for a in trace5["message"]["attempts"]]
            self.assertEqual(steps, sorted(steps))

    def test_missing_message_returns_clear_error(self):
        spec = sample_spec()
        with tempfile.TemporaryDirectory() as tmp:
            m = RunManager(tmp, step_delay=0.01)
            m.create(spec["routers"], spec["links"], spec["events"])
            wait_done(m)
            trace, err = m.message_trace(999)
            self.assertIsNone(trace)
            self.assertIn("不存在", err)
            self.assertIn("#999", err)

    def test_trace_without_any_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = RunManager(tmp, step_delay=0.01)
            trace, err = m.message_trace(1)
            self.assertIsNone(trace)
            self.assertIn("不存在演练", err)

    def test_trace_scoped_to_current_run_after_restart(self):
        spec = sample_spec()
        with tempfile.TemporaryDirectory() as tmp:
            m1 = RunManager(tmp, step_delay=0.01)
            m1.create(spec["routers"], spec["links"], spec["events"])
            done = wait_done(m1)
            m1.shutdown()
            # 重启恢复后，追溯记录顺序与内容须与不中断执行一致
            m2 = RunManager(tmp, step_delay=0.01)
            cur = m2.current()["run"]
            self.assertEqual(cur["run_id"], done["run_id"])
            for mid in range(1, 6):
                trace, err = m2.message_trace(mid)
                self.assertIsNone(err)
                restored = trace["message"]
                snapshot_msg = next(
                    s for s in done["steps"][-1]["state"]["messages"] if s["id"] == mid
                )
                self.assertEqual(
                    restored["attempts"], snapshot_msg["attempts"],
                    "消息 #{} 恢复后追溯与检查点不一致".format(mid),
                )
                self.assertEqual(restored["status"], snapshot_msg["status"])

    def test_trace_does_not_leak_superseded_run(self):
        routers = ["A", "B"]
        links = [{"src": "A", "dst": "B", "localpref": 100, "epoch": 1}]
        with tempfile.TemporaryDirectory() as tmp:
            m = RunManager(tmp, step_delay=0.01)
            # 旧演练含一条消息
            old_events = [
                {"type": "announce", "from": "A", "to": "A", "prefix": "A",
                 "path": ["A"], "epoch": 1},
                {"type": "deliver", "src": "B", "dst": "A", "msg": "old-run-msg"},
            ]
            first = m.create(routers, links, old_events)
            wait_done(m)
            # 新演练没有任何消息
            m.create(routers, links, [
                {"type": "announce", "from": "A", "to": "A", "prefix": "Z",
                 "path": ["A"], "epoch": 1},
            ])
            wait_done(m)
            trace, err = m.message_trace(1)
            self.assertIsNone(trace)
            self.assertIn("不存在", err)  # 仅报当前演练无此消息，不返回旧演练记录
            self.assertNotIn("old-run-msg", str(err))
            self.assertNotEqual(m.current()["run"]["run_id"], first["run_id"])


if __name__ == "__main__":
    unittest.main()
