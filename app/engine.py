"""星间中继网络路径矢量路由引擎。

引擎是纯确定性状态机：相同初始配置与相同事件序列必然收敛到完全
相同的状态，因此可以把“断点恢复后继续执行”的结果与“从头不中断
回放”的结果互相校验（恢复回放一致性）。

邻接语义（有向）：
  邻接 X->Y 表示 X 可以向 Y 发送路由通告；Y 以该邻接配置的本地
  偏好评估来自 X 的入站路由。数据沿通告的反方向转发：若 Y 的
  最优路径下一跳为 X，则 Y 将数据交给 X（数据流经邻接 X->Y）。
  链路纪元标识邻接的当前“代”：断开重连后纪元递增，携带旧纪元
  的通告 / 撤销 / 消息一律按过期处理。

选路规则（稳定裁决）：
  1. 本地偏好高者优先；
  2. 路径长度短者优先；
  3. 下一跳标识字典序小者优先。
"""

import copy
import json

SELF_PREF = 1 << 30  # 本机始发路由的本地偏好（视为最高）

MAX_ROUTERS = 8
MAX_EVENTS = 64


def adj_key(src, dst):
    return "{}->{}".format(src, dst)


def initial_state(routers, links):
    """根据路由器清单与有向邻接清单构造初始状态（全部邻接在线）。"""
    adj = {}
    for link in links:
        adj[adj_key(link["src"], link["dst"])] = {
            "src": link["src"],
            "dst": link["dst"],
            "localpref": int(link["localpref"]),
            "epoch": int(link["epoch"]),
            "up": True,
        }
    return {
        "routers": list(routers),
        "adj": adj,
        "inbound": {r: {} for r in routers},      # router -> prefix -> neighbor -> {path, epoch}; 邻居 "" 表示本机始发
        "announced": {r: {} for r in routers},    # router -> prefix -> {path, sent: {nbr: path}}
        "messages": [],
        "msg_seq": 0,
        "step": 0,                                # 已应用的事件步骤数（追溯记录按此编号）
    }


def canonical(state):
    """状态规范化串，用于恢复回放与不中断回放的一致性比对。"""
    return json.dumps(state, sort_keys=True, ensure_ascii=False)


def best_path(state, router, prefix):
    """按 本地偏好 -> 路径长度 -> 下一跳标识 稳定裁决最优路径。"""
    routes = state["inbound"].get(router, {}).get(prefix)
    if not routes:
        return None
    best = None
    best_key = None
    for nbr in sorted(routes):
        rt = routes[nbr]
        if nbr == "":
            lp = SELF_PREF
        else:
            adj = state["adj"].get(adj_key(nbr, router))
            if adj is None or not adj["up"]:
                continue  # 已断开邻居不参与选路
            lp = adj["localpref"]
        key = (-lp, len(rt["path"]), nbr)
        if best_key is None or key < best_key:
            best_key = key
            best = {
                "neighbor": nbr,
                "path": list(rt["path"]),
                "localpref": lp,
                "epoch": rt["epoch"],
            }
    return best


def best_table(state):
    """全部路由器当前最优路径汇总（供界面展示与检查点存档）。"""
    table = {}
    for router in sorted(state["inbound"]):
        for prefix in sorted(state["inbound"][router]):
            best = best_path(state, router, prefix)
            if best is not None:
                table.setdefault(router, {})[prefix] = best
    return table


def _out_path(path, router):
    """生成对外传播路径：路径末端已是本机（本机始发）时直接沿用。"""
    return list(path) if path[-1] == router else list(path) + [router]


def _recompute(state, router, prefix, logs):
    """最优路径变化后向全部在线邻接传播更新或撤销。"""
    best = best_path(state, router, prefix)
    new_path = best["path"] if best else None
    ann = state["announced"][router]
    entry = ann.get(prefix)
    old_path = entry["path"] if entry else None
    if old_path == new_path:
        return
    old_sent = dict(entry["sent"]) if entry else {}
    if new_path is None:
        for z in sorted(old_sent):
            adj = state["adj"].get(adj_key(router, z))
            if adj and adj["up"]:
                _withdraw(state, router, z, prefix, adj["epoch"], logs, propagated=True)
        ann.pop(prefix, None)
        return
    out_path = _out_path(new_path, router)
    new_sent = {}
    for key in sorted(state["adj"]):
        adj = state["adj"][key]
        if adj["src"] != router or not adj["up"]:
            continue
        z = adj["dst"]
        if z in out_path:
            logs.append({
                "kind": "loop",
                "text": "环路拒绝：不向 {} 传播前缀 {}（路径 {} 含接收方标识）".format(
                    z, prefix, "-".join(out_path)),
            })
            continue
        if old_sent.get(z) == out_path:
            new_sent[z] = out_path
            continue
        _announce(state, router, z, prefix, out_path, adj["epoch"], logs, propagated=True)
        new_sent[z] = out_path
    for z in sorted(set(old_sent) - set(new_sent)):
        adj = state["adj"].get(adj_key(router, z))
        if adj and adj["up"]:
            _withdraw(state, router, z, prefix, adj["epoch"], logs, propagated=True)
    ann[prefix] = {"path": list(new_path), "sent": new_sent}


def _announce(state, frm, to, prefix, path, epoch, logs, propagated=False):
    tag = "传播" if propagated else "通告"
    if frm == to:  # 本机始发
        routes = state["inbound"][frm].setdefault(prefix, {})
        self_rt = {"path": [frm], "epoch": 0}
        if routes.get("") == self_rt:
            logs.append({"kind": "duplicate", "text": "重复通告：{} 已始发前缀 {}".format(frm, prefix)})
            return
        routes[""] = self_rt
        logs.append({"kind": "originate", "text": "{} 始发前缀 {}".format(frm, prefix)})
        _recompute(state, frm, prefix, logs)
        return
    adj = state["adj"].get(adj_key(frm, to))
    if adj is None:
        logs.append({"kind": "ignored", "text": "{}忽略：邻接 {}->{} 不存在".format(tag, frm, to)})
        return
    if not adj["up"]:
        logs.append({"kind": "ignored", "text": "{}忽略：邻接 {}->{} 已断开，断开邻居不参与选路".format(tag, frm, to)})
        return
    if epoch != adj["epoch"]:
        logs.append({"kind": "stale", "text": "过期{}忽略：邻接 {}->{} 当前链路纪元 {}，{}携带纪元 {}".format(
            tag, frm, to, adj["epoch"], tag, epoch)})
        return
    if to in path:
        logs.append({"kind": "loop", "text": "环路拒绝：{} 拒收前缀 {}，路径 {} 含接收方标识".format(
            to, prefix, "-".join(path))})
        return
    if len(set(path)) != len(path):
        logs.append({"kind": "loop", "text": "环路拒绝：{} 拒收前缀 {}，路径 {} 含重复路由器".format(
            to, prefix, "-".join(path))})
        return
    if not path or path[-1] != frm:
        logs.append({"kind": "invalid", "text": "无效{}：路径末端须为发送方 {}".format(tag, frm)})
        return
    routes = state["inbound"][to].setdefault(prefix, {})
    rt = {"path": list(path), "epoch": epoch}
    if routes.get(frm) == rt:
        logs.append({"kind": "duplicate", "text": "重复{}：{} 已持有来自 {} 的前缀 {} 相同路由".format(tag, to, frm, prefix)})
        return
    routes[frm] = rt
    logs.append({"kind": "accept", "text": "{} 接受来自 {} 的{}：前缀 {} 路径 {} 纪元 {}".format(
        to, frm, tag, prefix, "-".join(path), epoch)})
    _recompute(state, to, prefix, logs)


def _withdraw(state, frm, to, prefix, epoch, logs, propagated=False):
    """撤销仅移除该邻居同一前缀的对应通告（且纪元必须匹配）。"""
    tag = "传播撤销" if propagated else "撤销"
    if frm == to:  # 撤销本机始发
        routes = state["inbound"][frm].get(prefix, {})
        if "" not in routes:
            logs.append({"kind": "ignored", "text": "{}忽略：{} 未始发前缀 {}".format(tag, frm, prefix)})
            return
        del routes[""]
        if not routes:
            state["inbound"][frm].pop(prefix, None)
        logs.append({"kind": "withdraw", "text": "{} 撤销始发前缀 {}".format(frm, prefix)})
        _recompute(state, frm, prefix, logs)
        return
    adj = state["adj"].get(adj_key(frm, to))
    if adj is None or not adj["up"]:
        logs.append({"kind": "ignored", "text": "{}忽略：邻接 {}->{} 不存在或已断开".format(tag, frm, to)})
        return
    routes = state["inbound"][to].get(prefix, {})
    stored = routes.get(frm)
    if stored is None:
        logs.append({"kind": "ignored", "text": "{}忽略：{} 无来自 {} 的前缀 {} 对应通告".format(tag, to, frm, prefix)})
        return
    if stored["epoch"] != epoch:
        logs.append({"kind": "stale_withdraw", "text": "旧纪元撤销忽略：{} 持有 {} 的前缀 {} 为纪元 {}，撤销携带纪元 {}".format(
            to, frm, prefix, stored["epoch"], epoch)})
        return
    del routes[frm]
    if not routes:
        state["inbound"][to].pop(prefix, None)
    logs.append({"kind": "withdraw", "text": "{} 移除来自 {} 的前缀 {} 通告（纪元 {}）".format(to, frm, prefix, epoch)})
    _recompute(state, to, prefix, logs)


def _disconnect(state, frm, to, logs):
    adj = state["adj"].get(adj_key(frm, to))
    if adj is None:
        logs.append({"kind": "invalid", "text": "断开忽略：邻接 {}->{} 不存在".format(frm, to)})
        return
    if not adj["up"]:
        logs.append({"kind": "ignored", "text": "断开忽略：邻接 {}->{} 已处于断开".format(frm, to)})
        return
    adj["up"] = False
    inbox = state["inbound"][to]
    affected = sorted(p for p, routes in inbox.items() if frm in routes)
    for p in affected:
        del inbox[p][frm]
        if not inbox[p]:
            del inbox[p]
    logs.append({"kind": "disconnect", "text": "邻接 {}->{} 断开：{} 移除来自 {} 的 {} 条入站路由，断开邻居不再参与选路".format(
        frm, to, to, frm, len(affected))})
    for p in affected:
        _recompute(state, to, p, logs)


def _reconnect(state, frm, to, logs):
    adj = state["adj"].get(adj_key(frm, to))
    if adj is None:
        logs.append({"kind": "invalid", "text": "重连忽略：邻接 {}->{} 不存在".format(frm, to)})
        return
    if adj["up"]:
        logs.append({"kind": "ignored", "text": "重连忽略：邻接 {}->{} 已在线".format(frm, to)})
        return
    adj["up"] = True
    adj["epoch"] += 1
    logs.append({"kind": "reconnect", "text": "邻接 {}->{} 重连，进入链路纪元 {}".format(frm, to, adj["epoch"])})
    sent = 0
    for prefix in sorted(state["announced"][frm]):
        out_path = _out_path(state["announced"][frm][prefix]["path"], frm)
        if to in out_path:
            logs.append({"kind": "loop", "text": "环路拒绝：重连后不向 {} 重发前缀 {}（路径 {} 含接收方标识）".format(
                to, prefix, "-".join(out_path))})
            continue
        _announce(state, frm, to, prefix, out_path, adj["epoch"], logs, propagated=True)
        entry = state["announced"][frm].get(prefix)
        if entry is not None:
            entry["sent"][to] = out_path
        sent += 1
    logs.append({"kind": "resend", "text": "重连后 {} 按纪元 {} 向 {} 重发 {} 条路由".format(frm, adj["epoch"], to, sent)})


def _deliver(state, src, dst, payload, logs):
    state["msg_seq"] += 1
    mid = state["msg_seq"]
    snapshot = {k: adj["epoch"] for k, adj in sorted(state["adj"].items())}
    state["messages"].append({
        "id": mid,
        "src": src,
        "dst": dst,
        "payload": payload,
        "snapshot": snapshot,
        "status": "pending",
        "hops": [],
        "detail": "",
        "attempts": [],   # 稳定追溯记录：仅追加，按发生步骤排列（见 _attempt）
    })
    logs.append({"kind": "enqueue", "text": "消息 #{} 入队：{} → {}（{}）".format(mid, src, dst, payload or "空载荷")})


def _hop_record(cur, nxt, state, msg):
    """构造一跳的复核明细：发送/接收路由器、采用前缀、最优路径与发送纪元。"""
    best = best_path(state, cur, msg["dst"])
    key = adj_key(nxt, cur)
    adj = state["adj"].get(key)
    epoch = adj["epoch"] if adj is not None else None
    return {
        "from": cur,
        "to": nxt,
        "prefix": msg["dst"],
        "best_path": list(best["path"]),
        "localpref": best["localpref"],
        "send_epoch": epoch,
    }


def _attempt(state, msg, logs):
    """沿各路由器当前最优路径逐跳投递；每跳核验接收端邻接仍处于发送纪元。

    每次实际尝试都在消息首次入队后保留的稳定追溯记录（msg["attempts"]）中
    追加一条不可变记录：发生步骤、事件、逐跳裁决（发送/接收路由器、采用
    前缀与最优路径、发送纪元、裁决结果）、本次尝试结论及未继续转发的直接
    原因。旧尝试永不改写；同一步骤内不会重复调用，因此不会产生重复记录。
    """
    step = state["step"]
    record = {
        "step": step,
        "event": copy.deepcopy(state.get("last_event")),
        "hops": [],
        "verdict": "",
        "stop_reason": "",
    }
    cur = msg["src"]
    hops = [cur]
    visited = {cur}
    limit = len(state["routers"]) + 2

    def finish(verdict, stop_reason, log=None):
        msg["hops"] = hops
        msg["detail"] = stop_reason
        record["verdict"] = verdict
        record["stop_reason"] = stop_reason
        msg["attempts"].append(record)
        if log is not None:
            logs.append(log)

    for _ in range(limit):
        if cur == msg["dst"]:
            msg["status"] = "delivered"
            record["hops"].append({
                "from": cur, "to": cur, "prefix": msg["dst"],
                "best_path": [cur], "localpref": SELF_PREF,
                "send_epoch": None, "result": "arrived",
                "reason": "到达目的路由器 {}".format(cur),
            })
            finish("delivered", "到达目的路由器 {}".format(msg["dst"]))
            msg["detail"] = ""
            logs.append({"kind": "delivered", "text": "消息 #{} 已投递：{}".format(msg["id"], " → ".join(hops))})
            return
        best = best_path(state, cur, msg["dst"])
        if best is None or best["neighbor"] == "":
            record["hops"].append({
                "from": cur, "to": None, "prefix": msg["dst"],
                "best_path": None, "localpref": None,
                "send_epoch": None, "result": "stranded",
                "reason": "{} 无到 {} 的可用路由，滞留待投递".format(cur, msg["dst"]),
            })
            finish("pending", "{} 无到 {} 的可用路由，滞留待投递".format(cur, msg["dst"]))
            return
        nxt = best["neighbor"]
        adj = state["adj"].get(adj_key(nxt, cur))
        if adj is None or not adj["up"]:
            hop = _hop_record(cur, nxt, state, msg)
            hop.update({
                "result": "stranded",
                "reason": "邻接 {}->{} 不可用，滞留待投递".format(nxt, cur),
            })
            record["hops"].append(hop)
            finish("pending", "邻接 {}->{} 不可用，滞留待投递".format(nxt, cur))
            return
        sent_epoch = msg["snapshot"].get(adj_key(nxt, cur))
        hop = _hop_record(cur, nxt, state, msg)
        if sent_epoch != adj["epoch"]:
            hop.update({
                "result": "expired",
                "reason": "邻接 {}->{} 纪元 {} → {}，过期消息忽略".format(
                    nxt, cur, sent_epoch, adj["epoch"]),
            })
            record["hops"].append(hop)
            msg["status"] = "expired"
            finish(
                "expired",
                "邻接 {}->{} 纪元 {} → {}".format(nxt, cur, sent_epoch, adj["epoch"]),
                log={"kind": "expired", "text": "过期消息忽略：消息 #{}（{}→{}）发送时邻接 {}->{} 纪元 {}，当前纪元 {}".format(
                    msg["id"], msg["src"], msg["dst"], nxt, cur, sent_epoch, adj["epoch"])},
            )
            return
        if nxt in visited:
            hop.update({
                "result": "failed",
                "reason": "下一跳 {} 已在转发路径中，检测到数据环路".format(nxt),
            })
            record["hops"].append(hop)
            msg["status"] = "failed"
            finish("failed", "检测到数据环路",
                   log={"kind": "failed", "text": "消息 #{} 投递失败：数据环路".format(msg["id"])})
            return
        hop.update({
            "result": "forwarded",
            "reason": "邻接 {}->{} 处于发送纪元 {}，转发至 {}".format(nxt, cur, sent_epoch, nxt),
        })
        record["hops"].append(hop)
        hops.append(nxt)
        visited.add(nxt)
        cur = nxt
    msg["status"] = "failed"
    finish("failed", "跳数超限",
           log={"kind": "failed", "text": "消息 #{} 投递失败：跳数超限".format(msg["id"])})


def _attempt_deliveries(state, logs):
    for msg in state["messages"]:
        if msg["status"] != "pending":
            continue
        # 同一事件步骤内的自动重试不得生成重复追溯记录
        if msg["attempts"] and msg["attempts"][-1]["step"] == state["step"]:
            continue
        _attempt(state, msg, logs)


def apply_event(state, event):
    """应用单条事件并返回本步骤日志；随后自动重试全部待投递消息。"""
    logs = []
    etype = event.get("type")
    state["last_event"] = copy.deepcopy(event)
    state["step"] = state.get("step", 0) + 1
    if etype == "announce":
        path = event.get("path") or [event["from"]]
        _announce(state, event["from"], event["to"], event["prefix"], list(path),
                  int(event.get("epoch", 1)), logs)
    elif etype == "withdraw":
        _withdraw(state, event["from"], event["to"], event["prefix"],
                  int(event.get("epoch", 1)), logs)
    elif etype == "disconnect":
        _disconnect(state, event["from"], event["to"], logs)
    elif etype == "reconnect":
        _reconnect(state, event["from"], event["to"], logs)
    elif etype == "deliver":
        _deliver(state, event["src"], event["dst"], event.get("msg", ""), logs)
    else:
        logs.append({"kind": "invalid", "text": "未知事件类型：{}".format(etype)})
    _attempt_deliveries(state, logs)
    return logs


def run_script(routers, links, events, upto=None):
    """从初始状态不中断回放事件序列，返回 (最终状态, 每步快照)。"""
    state = initial_state(routers, links)
    steps = [{
        "step": 0,
        "event": None,
        "logs": [],
        "state": copy.deepcopy(state),
        "best": best_table(state),
    }]
    for i, ev in enumerate(events[:upto], 1):
        logs = apply_event(state, ev)
        steps.append({
            "step": i,
            "event": ev,
            "logs": logs,
            "state": copy.deepcopy(state),
            "best": best_table(state),
        })
    return state, steps
