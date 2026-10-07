"""验收服务：代码测试 + 页面构建检查 + 站点与 /healthz 的 HTTP 冒烟。

围绕旧纪元撤销、环路拒绝、恢复收敛三类场景运行单元/集成测试，
核对页面构建产物清单，随后对运行中的站点执行 HTTP 冒烟（含一次
端到端演练：创建示例演练、等待后台计算完成、校验收敛结论与关键
日志类别）。全部通过后以退出码 0 报告验收成功，否则退出码 1。
"""

import hashlib
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_PORT = os.environ.get("APP_PORT", "8000")
BASE = "http://{}:{}".format(APP_HOST, APP_PORT)
WAIT_READY_SEC = float(os.environ.get("VERIFY_WAIT_READY", "60"))
E2E_TIMEOUT_SEC = float(os.environ.get("VERIFY_E2E_TIMEOUT", "90"))

RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    line = "[{}] {}{}".format("PASS" if ok else "FAIL", name, " — " + detail if detail else "")
    print(line, flush=True)


# ---------------- 代码测试 ----------------

def run_unit_tests():
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=1)
    result = runner.run(suite)
    detail = "{} 项测试，失败 {}，错误 {}".format(
        result.testsRun, len(result.failures), len(result.errors))
    return result.wasSuccessful(), detail


# ---------------- 页面构建检查 ----------------

def check_page_build():
    manifest_path = os.path.join(ROOT, "app", "static", "build-manifest.json")
    if not os.path.isfile(manifest_path):
        return False, "缺少 build-manifest.json（页面未构建）"
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    files = manifest.get("files", [])
    if not files:
        return False, "构建清单为空"
    for entry in files:
        full = os.path.join(ROOT, "app", "static", entry["name"])
        if not os.path.isfile(full):
            return False, "构建产物缺失：{}".format(entry["name"])
        with open(full, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        if digest != entry["sha256"]:
            return False, "构建产物校验失败：{}".format(entry["name"])
    return True, "{} 个页面产物校验通过".format(len(files))


# ---------------- HTTP 冒烟 ----------------

def http_get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


def http_post(path, obj, timeout=10):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def wait_ready():
    deadline = time.time() + WAIT_READY_SEC
    while time.time() < deadline:
        try:
            status, body = http_get("/healthz", timeout=3)
            if status == 200 and json.loads(body).get("status") == "ok":
                return True, "服务就绪"
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(1)
    return False, "等待服务就绪超时"


def smoke_healthz():
    status, body = http_get("/healthz")
    if status != 200:
        return False, "HTTP {}".format(status)
    payload = json.loads(body)
    if payload.get("status") != "ok":
        return False, "响应异常：{}".format(body[:80])
    return True, "HTTP 200，status=ok"


def smoke_site():
    status, body = http_get("/")
    if status != 200:
        return False, "HTTP {}".format(status)
    if "星间中继" not in body:
        return False, "首页缺少预期内容"
    for asset in ("/static/app.js", "/static/style.css"):
        astatus, abody = http_get(asset)
        if astatus != 200 or not abody:
            return False, "静态资源 {} 不可用（HTTP {}）".format(asset, astatus)
    return True, "首页与静态资源均可访问"


def smoke_e2e_drill():
    """端到端：创建示例演练 → 等待后台计算 → 校验收敛与关键日志。"""
    status, sample_body = http_get("/api/sample")
    if status != 200:
        return False, "获取示例失败（HTTP {}）".format(status)
    sample = json.loads(sample_body)
    status, created = http_post("/api/drills", sample)
    if status != 200:
        return False, "创建演练失败（HTTP {}）".format(status)
    deadline = time.time() + E2E_TIMEOUT_SEC
    run = None
    while time.time() < deadline:
        _, body = http_get("/api/drills/current")
        run = json.loads(body)["run"]
        if run and run["run_id"] == created["run_id"] and run["done"]:
            break
        time.sleep(1)
    else:
        return False, "等待演练完成超时"
    if run["converged"] is not True:
        return False, "恢复回放与不中断回放不一致"
    all_kinds = set()
    for step in run["steps"]:
        all_kinds.update(log["kind"] for log in step["logs"])
    required = {"loop", "stale", "stale_withdraw", "expired", "delivered"}
    missing = required - all_kinds
    if missing:
        return False, "缺少关键日志类别：{}".format(",".join(sorted(missing)))
    final = run["steps"][-1]["state"]
    stored = final["inbound"].get("C", {}).get("D", {}).get("D")
    if not stored or stored["epoch"] != 2:
        return False, "旧纪元撤销被错误生效（C 缺失纪元 2 的 D 路由）"
    return True, "端到端演练收敛一致，环路拒绝/旧纪元撤销/过期消息忽略均已展示"


def smoke_message_trace():
    """消息追溯：选中演练消息复核逐跳裁决；不存在或外演练标识须明确报错。"""
    _, body = http_get("/api/drills/current")
    run = json.loads(body)["run"]
    if not run or not run["done"]:
        return False, "当前无已完成演练"
    messages = run["steps"][-1]["state"]["messages"]
    if not messages:
        return False, "演练中无消息"
    mid = messages[0]["id"]
    status, body = http_get("/api/drills/current/messages/%d/trace" % mid)
    if status != 200:
        return False, "追溯查询失败（HTTP {}）".format(status)
    msg = json.loads(body).get("message")
    if not msg or msg.get("id") != mid or not msg.get("trace"):
        return False, "追溯记录缺失或不完整"
    hop = msg["trace"][0]["hops"][0]
    for field in ("from", "to", "prefix", "path", "epoch", "verdict", "reason"):
        if field not in hop:
            return False, "追溯跳记录缺少字段 {}".format(field)
    # 滞留后过期的消息应保留多次尝试：旧记录不变，新记录按步骤追加
    expired = [m for m in messages if m["status"] == "expired"]
    if expired:
        status, body = http_get("/api/drills/current/messages/%d/trace" % expired[0]["id"])
        attempts = json.loads(body)["message"]["trace"]
        if len(attempts) < 2 or attempts[-1]["outcome"] != "expired":
            return False, "滞留消息的多次尝试未按发生步骤追加"
        if [a["step"] for a in attempts] != sorted(a["step"] for a in attempts):
            return False, "追溯尝试未按步骤顺序排列"
    # 不存在的消息标识须返回明确错误（404 + error），不得泄露其他演练
    try:
        http_get("/api/drills/current/messages/99999/trace")
        return False, "不存在的消息标识未报错"
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            return False, "不存在的消息标识返回 HTTP {}（应为 404）".format(exc.code)
        if "error" not in json.loads(exc.read().decode("utf-8")):
            return False, "错误响应缺少 error 字段"
    return True, "逐跳追溯可查，多次尝试按步骤追加，不存在标识返回 404 明确错误"


# ---------------- 主流程 ----------------

def main():
    print("== 星间中继演练台验收 ==", flush=True)

    ok, detail = run_unit_tests()
    report("代码测试（旧纪元撤销 / 环路拒绝 / 恢复收敛等场景）", ok, detail)

    try:
        ok, detail = check_page_build()
    except Exception as exc:  # noqa: BLE001 - 验收脚本需汇总一切失败
        ok, detail = False, repr(exc)
    report("页面构建检查", ok, detail)

    try:
        ok, detail = wait_ready()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, repr(exc)
    report("等待服务就绪（{}）".format(BASE), ok, detail)

    if ok:
        for name, fn in (
            ("HTTP 冒烟：/healthz", smoke_healthz),
            ("HTTP 冒烟：站点页面", smoke_site),
            ("HTTP 冒烟：端到端演练收敛", smoke_e2e_drill),
            ("HTTP 冒烟：消息逐跳追溯", smoke_message_trace),
        ):
            try:
                ok, detail = fn()
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, repr(exc)
            report(name, ok, detail)
    else:
        for name in ("HTTP 冒烟：/healthz", "HTTP 冒烟：站点页面", "HTTP 冒烟：端到端演练收敛",
                     "HTTP 冒烟：消息逐跳追溯"):
            report(name, False, "服务未就绪，跳过")

    failed = [r for r in RESULTS if not r[1]]
    print("== 验收{}：{}/{} 项通过 ==".format(
        "失败" if failed else "通过", len(RESULTS) - len(failed), len(RESULTS)), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
