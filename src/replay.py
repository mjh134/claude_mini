#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回放一条 trace 文件：python replay.py <trace文件>

只读：不执行工具、不发 LLM 请求、不落任何文件。决策见 decisions/0002。
"""

import json
import re
import sys
from pathlib import Path

TOKEN_RE = re.compile(r"(cache_read_input_tokens|input_tokens|output_tokens)=(\d+)")
BLANKS = " \t\r\n"


def load_events(path):
    """JSON 文档流顺序读，一行式旧格式与缩进多行新格式通吃。
    读不了的尾数进 warnings——半截事件是 kill 现场，不是失败。"""
    raw = Path(path).read_text(encoding="utf-8")
    dec, events, warns, pos = json.JSONDecoder(), [], [], 0
    while pos < len(raw):
        while pos < len(raw) and raw[pos] in BLANKS:
            pos += 1
        if pos >= len(raw):
            break
        try:
            obj, pos = dec.raw_decode(raw, pos)
        except ValueError:
            warns.append(f"末尾有半个事件（最后完整 seq={events[-1]['seq'] if events else '无'}），已跳过")
            break
        if not isinstance(obj, dict) or "seq" not in obj:
            warns.append("一条没有 seq 的事件，已跳过")
            continue
        events.append(obj)
    events.sort(key=lambda e: e["seq"])
    return events, warns


def parse_usage(text):
    """usage 有两代形态：新的是 dict {input,output}，旧的是 Usage(...) repr，读侧都认"""
    if isinstance(text, dict):
        found = {}
        for src, dst in (("input", "input_tokens"), ("output", "output_tokens"),
                         ("cache_read", "cache_read_input_tokens")):
            if text.get(src) is not None:
                found[dst] = int(text[src])
        return found
    found = {}
    for key, value in TOKEN_RE.findall(str(text)):
        found[key] = found.get(key, 0) + int(value)
    return found


def brief(text, width=70):
    text = str(text).replace("\n", "\\n")
    return text if len(text) <= width else text[:width] + "…"


def event_line(e):
    p, kind = e["payload"], e["type"]
    if kind == "llm_request":
        sent = p.get("messages_sent") or {}
        u = parse_usage(p.get("usage"))
        state = "ok" if p.get("ok", True) else f"FAILED {brief(p.get('error'))}"
        anchor = "  ← 锚点（压缩重写过 history，此后 delta 以它为准）" \
            if sent.get("mode") == "resnapshot" else ""
        return (f"LLM  {p.get('elapsed')}s  mode={sent.get('mode')}  "
                f"msgs={len(sent.get('messages') or [])}  "
                f"in={u.get('input_tokens', 0)} out={u.get('output_tokens', 0)} "
                f"cache_read={u.get('cache_read_input_tokens', 0)}  [{state}]{anchor}")
    if kind == "tool_call":
        tail = f"error: {brief(p.get('error'))}" if not p.get("ok", True) \
            else f"→ {brief(p.get('output'))}"
        return (f"tool  {p.get('name')}  {p.get('input')}  {tail}"
                f"  ({p.get('elapsed')}s)")
    if kind == "hook_decision":
        reason = f"  ← {p.get('reason')}" if p.get("reason") else ""
        return f"hook  {p.get('hook')} {p.get('tool')} → {p.get('decision')}{reason}"
    if kind == "compact":
        if "before_chars" in p:
            return f"compact  {p.get('layer')}  {p['before_chars']}→{p['after_chars']} 字"
        return f"compact  {p.get('layer')}  {p.get('before')}→{p.get('after')} 条"
    if kind == "task_event":
        detail = p.get("command") or p.get("status") or ""
        return f"task  {p.get('action')}  {p.get('kind', '')}  {brief(detail)}  ({e.get('correlation_id')})"
    return f"{kind}  {brief(json.dumps(p, ensure_ascii=False))}"


def render(events):
    """按 agent_id 分轨、轨内按 turn 分段；turn 为 None 的走任务轨，不塞进任何一轮"""
    tracks, bucket = [], {}
    for e in events:
        key = e.get("agent_id")
        if key not in bucket:
            bucket[key] = []
            tracks.append((key, bucket[key]))
        bucket[key].append(e)
    origin = min(e["ts"] for e in events)
    lines = []
    for name, evs in tracks:
        lines.append(f"\n━━ 轨道 {name} ━━")
        current = object()
        for e in evs:
            if e.get("turn") != current:
                current = e.get("turn")
                head = f"turn {current}" if current is not None else "任务级（不属于任何一轮）"
                lines.append(f"  ▸ {head}   +{round(e['ts'] - origin, 2)}s")
            lines.append(f"      {event_line(e)}")
            cid = e.get("correlation_id")
            if cid and e["type"] in ("tool_call", "hook_decision"):
                lines[-1] += f"   [{cid}]"
    return "\n".join(lines)


def summarize(events):
    """六维里这一项先给两维：效率 + 安全原始计数。判定口径等项3 再定"""
    turns, calls = set(), []
    llm = hook = compact = 0
    tokens, decisions, tasks = {}, {}, []
    slow = llm_time = 0.0
    for e in events:
        p = e["payload"]
        if e.get("turn") is not None:
            turns.add((e.get("agent_id"), e["turn"]))
        if e["type"] == "llm_request":
            llm += 1
            slow = max(slow, p.get("elapsed") or 0)
            llm_time += p.get("elapsed") or 0
            for key, value in parse_usage(p.get("usage")).items():
                tokens[key] = tokens.get(key, 0) + value
        elif e["type"] == "tool_call":
            calls.append(p)
        elif e["type"] == "hook_decision":
            decisions[p.get("decision")] = decisions.get(p.get("decision"), 0) + 1
            hook += 1
        elif e["type"] == "compact":
            compact += 1
        elif e["type"] == "task_event":
            tasks.append(p.get("action"))
    tool_time = sum(c.get("elapsed") or 0 for c in calls)
    lines = ["", "—— 汇总 ——",
             f"轮数 {len(turns)} · LLM {llm} 次（最慢一轮 {round(slow, 2)}s，合计 {round(llm_time, 2)}s）"
             f" · 工具耗时合计 {round(tool_time, 2)}s",
             f"token: in={tokens.get('input_tokens', 0)} out={tokens.get('output_tokens', 0)} cache_read={tokens.get('cache_read_input_tokens', 0)}",
             f"工具 {len(calls)} 次，失败 {sum(1 for c in calls if not c.get('ok', True))} 次",
             f"hook 判定 {hook} 次：" + " / ".join(f"{k}={v}" for k, v in sorted(decisions.items())),
             f"压缩 {compact} 次 · 后台任务 {len(tasks)} 条事件"]
    return "\n".join(lines)


def main(argv):
    if len(argv) != 2:
        print(f"用法: python {Path(argv[0]).name} <trace文件>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"读不到 trace 文件: {path}", file=sys.stderr)
        return 1
    events, warns = load_events(path)
    print(f"回放 {path.name} · {len(events)} 条事件")
    print(render(events) if events else "（没有事件）")
    if events:
        print(summarize(events))
    for w in warns:
        print(f"[warn] {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
