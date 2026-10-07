"""演练脚本 DSL 解析与校验。

路由器：空白分隔的标识符，至多 8 台。
邻接：每行一条 ``<源> <宿> pref=<本地偏好> [epoch=<起始纪元>]``，
      也支持 ``<源> -> <宿> ...`` 写法。
事件：每行一条，至多 64 项——
      announce <从> <到> prefix=<前缀> [path=A,B,C] [epoch=N]
      withdraw <从> <到> prefix=<前缀> [epoch=N]
      disconnect <从> <到>
      reconnect  <从> <到>
      deliver    <源> <宿> [msg=任意文本]
以 # 开头的行为注释；announce 中 从==到 表示本机始发。
"""

import re

from .engine import MAX_EVENTS, MAX_ROUTERS

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
MAX_LOCALPREF = 1000000
MAX_EPOCH = 1000000
MAX_PATH_LEN = 16
MAX_PAYLOAD = 200


def _is_id(token):
    return bool(ID_RE.match(token))


def _lines(text):
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line and not line.startswith("#"):
            yield lineno, line


def _parse_int(value, what, lineno, errors, lo, hi):
    try:
        num = int(value)
    except (TypeError, ValueError):
        errors.append("第 {} 行：{} 须为整数，得到 {!r}".format(lineno, what, value))
        return None
    if not (lo <= num <= hi):
        errors.append("第 {} 行：{} 须位于 {}..{}，得到 {}".format(lineno, what, lo, hi, num))
        return None
    return num


def _parse_kv(tokens, lineno, allowed, errors):
    kv = {}
    for tok in tokens:
        if "=" not in tok:
            errors.append("第 {} 行：无法识别的参数 {!r}（应为 键=值）".format(lineno, tok))
            continue
        key, value = tok.split("=", 1)
        if key not in allowed:
            errors.append("第 {} 行：未知参数 {!r}".format(lineno, key))
            continue
        kv[key] = value
    return kv


def parse_routers(text, errors):
    routers = []
    for tok in text.split():
        if not _is_id(tok):
            errors.append("路由器标识 {!r} 非法（1-16 位字母数字、下划线或连字符）".format(tok))
            continue
        if tok in routers:
            errors.append("路由器 {!r} 重复".format(tok))
            continue
        routers.append(tok)
    if not routers:
        errors.append("至少需要 1 台路由器")
    elif len(routers) > MAX_ROUTERS:
        errors.append("路由器至多 {} 台，得到 {} 台".format(MAX_ROUTERS, len(routers)))
    return routers


def parse_links(text, routers, errors):
    links = []
    seen = set()
    known = set(routers)
    for lineno, line in _lines(text):
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "->":
            del parts[1]
        if len(parts) < 2:
            errors.append("第 {} 行：邻接须为 '<源> <宿> pref=N [epoch=M]'".format(lineno))
            continue
        src, dst = parts[0], parts[1]
        kv = _parse_kv(parts[2:], lineno, {"pref", "epoch"}, errors)
        pref = _parse_int(kv.get("pref", "100"), "本地偏好", lineno, errors, 0, MAX_LOCALPREF)
        epoch = _parse_int(kv.get("epoch", "1"), "起始纪元", lineno, errors, 1, MAX_EPOCH)
        if src not in known:
            errors.append("第 {} 行：源路由器 {!r} 未定义".format(lineno, src))
        if dst not in known:
            errors.append("第 {} 行：宿路由器 {!r} 未定义".format(lineno, dst))
        if src == dst:
            errors.append("第 {} 行：邻接两端不能相同（{}）".format(lineno, src))
        if (src, dst) in seen:
            errors.append("第 {} 行：邻接 {}->{} 重复定义".format(lineno, src, dst))
            continue
        seen.add((src, dst))
        if pref is not None and epoch is not None:
            links.append({"src": src, "dst": dst, "localpref": pref, "epoch": epoch})
    return links


def _check_router(name, role, lineno, known, errors):
    if name not in known:
        errors.append("第 {} 行：{}路由器 {!r} 未定义".format(lineno, role, name))
        return False
    return True


def _check_adj(frm, to, lineno, adj_keys, errors):
    if (frm, to) not in adj_keys:
        errors.append("第 {} 行：邻接 {}->{} 未配置".format(lineno, frm, to))
        return False
    return True


def _parse_path(value, lineno, known, errors):
    hops = value.split(",") if value else []
    path = []
    for hop in hops:
        hop = hop.strip()
        if not _is_id(hop):
            errors.append("第 {} 行：路径节点 {!r} 非法".format(lineno, hop))
            continue
        if hop not in known:
            errors.append("第 {} 行：路径节点 {!r} 未定义".format(lineno, hop))
            continue
        path.append(hop)
    if not path:
        errors.append("第 {} 行：路径不能为空".format(lineno))
    elif len(path) > MAX_PATH_LEN:
        errors.append("第 {} 行：路径长度超过 {}".format(lineno, MAX_PATH_LEN))
    return path


def parse_events(text, routers, links, errors):
    events = []
    known = set(routers)
    adj_keys = {(l["src"], l["dst"]) for l in links}
    for lineno, line in _lines(text):
        parts = line.split()
        etype = parts[0]
        if etype in ("announce", "withdraw"):
            if len(parts) < 3:
                errors.append("第 {} 行：{} 须为 '{} <从> <到> prefix=P [path=..] [epoch=N]'".format(lineno, etype, etype))
                continue
            frm, to = parts[1], parts[2]
            kv = _parse_kv(parts[3:], lineno, {"prefix", "path", "epoch"}, errors)
            ok = _check_router(frm, "发送方", lineno, known, errors)
            ok = _check_router(to, "接收方", lineno, known, errors) and ok
            prefix = kv.get("prefix")
            if not prefix or not _is_id(prefix):
                errors.append("第 {} 行：prefix 缺失或非法".format(lineno))
                ok = False
            epoch = _parse_int(kv.get("epoch", "1"), "纪元", lineno, errors, 1, MAX_EPOCH)
            if epoch is None:
                ok = False
            path = None
            if etype == "announce":
                if "path" in kv:
                    path = _parse_path(kv["path"], lineno, known, errors)
                    if not path:
                        ok = False
                if frm == to:
                    if path is not None and path != [frm]:
                        errors.append("第 {} 行：本机始发的路径须为 [{}]".format(lineno, frm))
                        ok = False
                    path = [frm]
                else:
                    if path is None:
                        path = [frm]
                    elif path[-1] != frm:
                        errors.append("第 {} 行：路径末端须为发送方 {}".format(lineno, frm))
                        ok = False
                    ok = _check_adj(frm, to, lineno, adj_keys, errors) and ok
            else:
                if frm != to:
                    ok = _check_adj(frm, to, lineno, adj_keys, errors) and ok
            if ok:
                ev = {"type": etype, "from": frm, "to": to, "prefix": prefix, "epoch": epoch}
                if etype == "announce":
                    ev["path"] = path
                events.append(ev)
        elif etype in ("disconnect", "reconnect"):
            if len(parts) != 3:
                errors.append("第 {} 行：{} 须为 '{} <从> <到>'".format(lineno, etype, etype))
                continue
            frm, to = parts[1], parts[2]
            ok = _check_router(frm, "发送方", lineno, known, errors)
            ok = _check_router(to, "接收方", lineno, known, errors) and ok
            ok = _check_adj(frm, to, lineno, adj_keys, errors) and ok
            if ok:
                events.append({"type": etype, "from": frm, "to": to})
        elif etype == "deliver":
            if len(parts) < 3:
                errors.append("第 {} 行：deliver 须为 'deliver <源> <宿> [msg=文本]'".format(lineno))
                continue
            src, dst = parts[1], parts[2]
            ok = _check_router(src, "源", lineno, known, errors)
            ok = _check_router(dst, "宿", lineno, known, errors) and ok
            rest = line.split(None, 3)[3] if len(parts) > 3 else ""
            rest = rest.strip()
            if rest.startswith("msg="):
                rest = rest[4:].strip()
            if len(rest) > MAX_PAYLOAD:
                errors.append("第 {} 行：消息内容超过 {} 字符".format(lineno, MAX_PAYLOAD))
                ok = False
            if ok:
                events.append({"type": "deliver", "src": src, "dst": dst, "msg": rest})
        else:
            errors.append("第 {} 行：未知事件类型 {!r}（支持 announce/withdraw/disconnect/reconnect/deliver）".format(lineno, etype))
    if len(events) > MAX_EVENTS:
        errors.append("事件至多 {} 项，得到 {} 项".format(MAX_EVENTS, len(events)))
    return events


def parse_all(routers_text, links_text, events_text):
    """解析并校验三段 DSL 文本，返回 (spec, errors)。"""
    errors = []
    routers = parse_routers(routers_text or "", errors)
    links = parse_links(links_text or "", routers, errors)
    events = parse_events(events_text or "", routers, links, errors)
    return {"routers": routers, "links": links, "events": events}, errors
