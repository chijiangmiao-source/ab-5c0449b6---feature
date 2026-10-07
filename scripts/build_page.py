"""页面构建：校验 index.html 引用的静态资源齐备且非空，
生成 build-manifest.json（文件清单 + SHA-256），供验收服务核对。
"""

import hashlib
import json
import os
import re

STATIC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "static",
)

REF_RE = re.compile(r'(?:src|href)="(/static/[^"]+)"')


def build():
    index_path = os.path.join(STATIC, "index.html")
    with open(index_path, encoding="utf-8") as fh:
        index = fh.read()
    refs = {os.path.basename(m) for m in REF_RE.findall(index)}
    names = sorted(refs | {"index.html"})
    entries = []
    for name in names:
        full = os.path.join(STATIC, name)
        if not os.path.isfile(full):
            raise SystemExit("页面构建失败：index.html 引用的资源缺失 {}".format(name))
        with open(full, "rb") as fh:
            data = fh.read()
        if not data:
            raise SystemExit("页面构建失败：资源为空 {}".format(name))
        entries.append({
            "name": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        })
    manifest = {"files": entries}
    with open(os.path.join(STATIC, "build-manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    print("页面构建完成：{} 个资源已写入 build-manifest.json".format(len(entries)))


if __name__ == "__main__":
    build()
