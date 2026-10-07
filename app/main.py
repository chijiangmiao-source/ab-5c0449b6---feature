"""星间中继网络演练台 HTTP 服务（纯标准库）。

端点：
  GET  /healthz               健康检查
  GET  /                      演练台页面
  GET  /static/<文件>         页面静态资源
  GET  /api/sample            预置示例演练（DSL 文本）
  POST /api/drills            创建新演练（后台计算并保存检查点）
  GET  /api/drills/current    当前演练及已完成步骤（页面重开后恢复）
  GET  /api/drills/current/messages/<id>  单条消息的稳定追溯记录
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import drill, sample
from .runner import RunManager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

MAX_BODY = 1 << 20


def make_handler(manager):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RelayDrill/1.0"

        def log_message(self, fmt, *args):
            sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))
            sys.stdout.flush()

        # ---------- 响应辅助 ----------
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def _static(self, name):
            name = os.path.basename(name)
            full = os.path.join(STATIC_DIR, name)
            if not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            ext = os.path.splitext(name)[1]
            with open(full, "rb") as fh:
                data = fh.read()
            self._send(200, data, CONTENT_TYPES.get(ext, "application/octet-stream"))

        # ---------- 路由 ----------
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/healthz":
                return self._json({"status": "ok"})
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/sample":
                return self._json(sample.sample_drill())
            if path == "/api/drills/current":
                return self._json(manager.current())
            if path.startswith("/api/drills/current/messages/"):
                raw = path[len("/api/drills/current/messages/"):]
                mid = self._parse_msg_id(raw)
                if mid is None:
                    return self._json({"error": "消息标识须为正整数"}, 400)
                query = parse_qs(urlparse(self.path).query)
                step = None
                if "step" in query:
                    try:
                        step = int(query["step"][0])
                    except ValueError:
                        step = -1
                    if step < 0:
                        return self._json({"error": "step 须为非负整数"}, 400)
                trace, err = manager.message_trace(mid, step)
                if err is not None:
                    return self._json({"error": err}, 404)
                return self._json(trace)
            return self._json({"error": "not found"}, 404)

        @staticmethod
        def _parse_msg_id(raw):
            if not raw or "/" in raw:
                return None
            try:
                mid = int(raw)
            except ValueError:
                return None
            return mid if mid >= 1 else None

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            path = urlparse(self.path).path
            if path != "/api/drills":
                return self._json({"error": "not found"}, 404)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                return self._json({"errors": ["请求体缺失或过大"]}, 400)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._json({"errors": ["请求体不是合法 JSON"]}, 400)
            spec, errors = drill.parse_all(
                str(payload.get("routers_text", "")),
                str(payload.get("links_text", "")),
                str(payload.get("events_text", "")),
            )
            if errors:
                return self._json({"errors": errors}, 422)
            meta = manager.create(spec["routers"], spec["links"], spec["events"])
            return self._json({
                "run_id": meta["run_id"],
                "seq": meta["seq"],
                "total": meta["total"],
            })

    return Handler


def main():
    port = int(os.environ.get("PORT", "8000"))
    data_dir = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
    step_delay = float(os.environ.get("STEP_DELAY", "0.25"))
    manager = RunManager(data_dir, step_delay=step_delay)
    server = ThreadingHTTPServer(("", port), make_handler(manager))
    print("星间中继演练台已启动：端口 {}，数据目录 {}".format(port, data_dir), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
