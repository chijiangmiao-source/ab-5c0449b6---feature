"""演练运行管理器：后台计算、逐步检查点持久化、恢复与收敛校验。

- 每次演练在后台线程中逐项应用事件，每完成一步即写入检查点文件；
- 页面重开（或服务重启）后从最近检查点恢复最后完整步骤，未完成的
  演练从断点继续执行；
- 新演练创建后，旧演练的后台结果（迟到结果）一律丢弃，不得覆盖；
- 演练结束（或载入已完成演练）时，从头不中断回放全部事件并与
  检查点逐步比对，给出“恢复回放与不中断回放一致”的收敛结论。
"""

import copy
import json
import os
import threading
import time
import uuid

from . import engine


class RunManager(object):
    def __init__(self, data_dir, step_delay=0.25):
        self.data_dir = data_dir
        self.step_delay = step_delay
        self.lock = threading.Lock()
        self.run = None
        os.makedirs(data_dir, exist_ok=True)
        self._load_latest()

    # ---------------- 持久化 ----------------

    def _load_latest(self):
        pointer = os.path.join(self.data_dir, "current.json")
        if not os.path.isfile(pointer):
            return
        try:
            with open(pointer, encoding="utf-8") as fh:
                run_dir = json.load(fh)["dir"]
            with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
            steps = []
            steps_dir = os.path.join(run_dir, "steps")
            for name in sorted(os.listdir(steps_dir)):
                if name.endswith(".json"):
                    with open(os.path.join(steps_dir, name), encoding="utf-8") as fh:
                        steps.append(json.load(fh))
            steps.sort(key=lambda s: s["step"])
        except (OSError, ValueError, KeyError):
            return
        if not steps:
            return  # 无任何完整检查点，视为无演练
        self.run = {"meta": meta, "steps": steps, "dir": run_dir}
        if not meta.get("done"):
            threading.Thread(target=self._resume, args=(self.run,), daemon=True).start()
        elif meta.get("converged") is None:
            meta["converged"] = self._check_convergence(self.run)
            self._write_meta(self.run)

    def _write_json(self, path, obj):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.replace(tmp, path)

    def _write_meta(self, run):
        self._write_json(os.path.join(run["dir"], "meta.json"), run["meta"])

    def _write_step(self, run, step):
        self._write_json(os.path.join(run["dir"], "steps", "%03d.json" % step["step"]), step)

    # ---------------- 演练控制 ----------------

    def create(self, routers, links, events):
        """创建新演练；旧演练的后台线程将自动放弃后续写入。"""
        with self.lock:
            seq = self.run["meta"]["seq"] + 1 if self.run else 1
            run_id = uuid.uuid4().hex[:12]
            run_dir = os.path.join(self.data_dir, "runs", "%04d-%s" % (seq, run_id))
            os.makedirs(os.path.join(run_dir, "steps"), exist_ok=True)
            meta = {
                "run_id": run_id,
                "seq": seq,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "routers": list(routers),
                "links": list(links),
                "events": list(events),
                "total": len(events),
                "done": False,
                "converged": None,
            }
            self.run = {"meta": meta, "steps": [], "dir": run_dir}
            self._write_meta(self.run)
            self._write_json(os.path.join(self.data_dir, "current.json"), {"dir": run_dir})
            run = self.run
        state = engine.initial_state(routers, links)
        threading.Thread(target=self._worker, args=(run, state, 0), daemon=True).start()
        return meta

    def _superseded(self, run):
        with self.lock:
            return self.run is not run

    def _worker(self, run, state, start):
        if start == 0:
            if not self._append(run, {
                "step": 0, "event": None, "logs": [],
                "state": copy.deepcopy(state), "best": engine.best_table(state),
            }):
                return
        events = run["meta"]["events"]
        for i in range(start, len(events)):
            if self._superseded(run):
                return  # 迟到结果不得覆盖新演练
            time.sleep(self.step_delay)
            logs = engine.apply_event(state, events[i])
            if not self._append(run, {
                "step": i + 1, "event": events[i], "logs": logs,
                "state": copy.deepcopy(state), "best": engine.best_table(state),
            }):
                return
        converged = self._check_convergence(run)
        with self.lock:
            if self.run is not run:
                return
            run["meta"]["done"] = True
            run["meta"]["converged"] = converged
            try:
                self._write_meta(run)
            except OSError:
                pass

    def _resume(self, run):
        """服务重启后从最后完整检查点继续执行剩余事件。"""
        state = copy.deepcopy(run["steps"][-1]["state"])
        start = run["steps"][-1]["step"]
        self._worker(run, state, start)

    def _append(self, run, step):
        with self.lock:
            if self.run is not run:
                return False
            run["steps"].append(step)
            try:
                self._write_step(run, step)
            except OSError:
                return False  # 数据目录已不可写（如演练被清理），放弃迟到结果
            return True

    def shutdown(self):
        """停止后台线程：当前演练被取代后，worker 会自动放弃后续写入。"""
        with self.lock:
            self.run = None

    def _check_convergence(self, run):
        """恢复回放一致性：从头不中断回放并逐步与检查点比对。"""
        meta = run["meta"]
        with self.lock:
            steps = list(run["steps"])
        if len(steps) != meta["total"] + 1:
            return False
        state = engine.initial_state(meta["routers"], meta["links"])
        if engine.canonical(state) != engine.canonical(steps[0]["state"]):
            return False
        for i, ev in enumerate(meta["events"], 1):
            engine.apply_event(state, ev)
            if engine.canonical(state) != engine.canonical(steps[i]["state"]):
                return False
        return True

    # ---------------- 查询 ----------------

    def message_trace(self, msg_id, step=None):
        """返回当前演练中某条消息的稳定追溯记录。

        step 给定时返回该检查步骤时刻的消息状态（追溯记录天然仅追加，
        因而即便是更早的步骤，已写入的记录也与后来完全一致）；step
        缺省返回最新检查点。仅服务于当前演练：消息不存在或标识不
        属于当前演练时返回 (None, 错误说明)，且不读取、不泄露磁盘
        上其他演练的记录。
        """
        with self.lock:
            if self.run is None:
                return None, "当前不存在演练，请先创建或恢复演练"
            meta = dict(self.run["meta"])
            steps = list(self.run["steps"])
        if not steps:
            return None, "演练尚未产生任何检查步骤"
        if step is None:
            target = steps[-1]
        else:
            target = next((s for s in steps if s["step"] == step), None)
            if target is None:
                return None, "步骤 {} 不存在（当前已完成 {} 步）".format(
                    step, len(steps) - 1)
        for msg in target.get("state", {}).get("messages", []):
            if msg["id"] == msg_id:
                break
        else:
            return None, "消息 #{} 不存在或不属于当前演练 #{}".format(msg_id, meta["seq"])
        return {
            "run_id": meta["run_id"],
            "seq": meta["seq"],
            "step": target["step"],
            "message": copy.deepcopy(msg),
            "attempt_count": len(msg.get("attempts", [])),
        }, None

    def current(self):
        with self.lock:
            if self.run is None:
                return {"run": None}
            meta = dict(self.run["meta"])
            steps = list(self.run["steps"])
        return {
            "run": {
                "run_id": meta["run_id"],
                "seq": meta["seq"],
                "created": meta["created"],
                "routers": meta["routers"],
                "links": meta["links"],
                "events": meta["events"],
                "total": meta["total"],
                "done": meta["done"],
                "converged": meta["converged"],
                "completed_steps": len(steps) - 1,
                "steps": steps,
            }
        }
