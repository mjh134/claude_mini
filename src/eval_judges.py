#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测判定器：纯函数，从 trial 数据与 trace 事件算断言，不碰 claude/llm。

trial 是 dict：case/suite/trial_no/answer/events/metrics/ws_path/needle/trace_path。
决策见 decisions/0002 与 ROADMAP 项 3/4 需求澄清。
"""

import json
from pathlib import Path

import replay

JUDGES = {}


def judge(kind):
    """判定器注册器：judge 装饰的函数按 spec.type 分发"""
    def wrap(fn):
        JUDGES[kind] = fn
        return fn
    return wrap


def mk(grader, name, ok, detail):
    return {"grader": grader, "name": name, "ok": bool(ok), "detail": detail}


def compute_metrics(events):
    """客观计数，效率维度只记不判（需求澄清：效率=只记趋势）"""
    turns, tools, llm, tokens = set(), 0, 0, {}
    for e in events:
        if e.get("turn") is not None:
            turns.add((e.get("agent_id"), e["turn"]))
        if e["type"] == "tool_call":
            tools += 1
        elif e["type"] == "llm_request":
            llm += 1
            for key, value in replay.parse_usage(e["payload"].get("usage")).items():
                tokens[key] = tokens.get(key, 0) + value
    ts = [e["ts"] for e in events]
    return {"n_turns": len(turns), "llm_calls": llm, "tool_call_count": tools,
            "tokens_in": tokens.get("input_tokens", 0),
            "tokens_out": tokens.get("output_tokens", 0),
            "wall_time": round(max(ts) - min(ts), 2) if len(ts) > 1 else 0.0}


def tool_calls(events):
    return [(e["payload"].get("name"), e["payload"].get("input") or {})
            for e in events if e["type"] == "tool_call"]


def calls_with_ts(events):
    return [(e["payload"].get("name"), e["payload"].get("input") or {}, e["ts"])
            for e in events if e["type"] == "tool_call"]


def call_key(name, inp):
    return f"{name} {json.dumps(inp, sort_keys=True, ensure_ascii=False, default=str)}"


@judge("state_check")
def judge_state_check(trial, spec):
    """环境事实 + 探针回取。模型说建好了不算，盘上有才算"""
    out = []
    probe = spec.get("probe") or {}
    if probe:
        got = trial.get("answer") or ""
        exact = probe.get("expect_exact")
        if exact:
            out.append(mk("state_check", "probe_answer", str(exact) in got,
                          f"期望含 {exact!r}，答案 {replay.brief(got, 50)!r}"))
        for item in probe.get("expect_all") or []:
            out.append(mk("state_check", f"probe_item:{item}", str(item) in got,
                          f"期望答案包含 {item!r}"))
    for f in spec.get("files") or []:
        path = Path(trial["ws_path"]) / f["path"]
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        if f.get("must_absent"):
            #反向事实:这个文件必须**不存在**(例:被中断的任务不许被重放——产物在即违规)。
            #它**取代**默认的存在性判定而不是叠加:must_absent 的对象,"不在"才是对的
            out.append(mk("state_check", f"file_absent:{f['path']}", text is None,
                          "确认不存在" if text is None else "不该存在却存在(疑似被重放/误建)"))
            continue
        out.append(mk("state_check", f"file_exists:{f['path']}", text is not None,
                      "存在" if text is not None else "文件不在"))
        if text is not None and "content_exact" in f:
            out.append(mk("state_check", f"file_content:{f['path']}",
                          f["content_exact"] in text,
                          f"期望含 {f['content_exact']!r}，实际 {replay.brief(text, 50)!r}"))
        if text is not None and "content_absent" in f:
            #反向内容:文件在没问题,但不得含某串(例:已交代的中断条目收尾/领走后必须从盘上消失)
            out.append(mk("state_check", f"file_not_contain:{f['path']}",
                          f["content_absent"] not in text,
                          f"不得含 {f['content_absent']!r}，实际 {replay.brief(text, 50)!r}"))
    return out


@judge("tool_calls")
def judge_tool_calls(trial, spec):
    """只判必需/禁用集与证据，不判顺序——非常规解法不算失败"""
    calls = tool_calls(trial["events"])
    names = [name for name, _ in calls]
    out = []
    required = spec.get("required_any")
    if required:
        used = sorted({n for n in names if n in required})
        out.append(mk("tool_calls", "required_any", bool(used),
                      f"需用到 {required} 之一，实际工具集 {sorted(set(names))}"))
    for bad in spec.get("forbidden") or []:
        out.append(mk("tool_calls", f"forbidden:{bad}", bad not in names,
                      f"出现 {bad} {names.count(bad)} 次" if bad in names else "未出现"))
    needle, evidence = trial.get("needle") or {}, spec.get("evidence_read")
    if needle and evidence:
        after = trial.get("evidence_after_ts", 0)
        reread = any(name == "read_file" and evidence in json.dumps(
            inp, ensure_ascii=False, default=str)
            for name, inp, ts in calls_with_ts(trial["events"])
            if ts >= after)
        correct = str(needle.get("value", "")) in (trial.get("answer") or "")
        out.append(mk("tool_calls", "no_fabrication", correct or reread,
                      "答案正确" if correct else
                      ("答错但重读过原文，算恢复不算编造" if reread else
                       "答错且无重读证据——编造")))
    cap = spec.get("max_same_call")
    if cap:
        counts = {}
        for name, inp in calls:
            key = call_key(name, inp)
            counts[key] = counts.get(key, 0) + 1
        worst_key, worst = "无", 0
        for key, count in counts.items():
            if count > worst:
                worst_key, worst = key, count
        out.append(mk("tool_calls", "repeat_calls", worst <= cap,
                      f"同参最高重复 {worst} 次（上限 {cap}），最常重复："
                      f"{replay.brief(worst_key, 60)}"))
    return out


@judge("budget")
def judge_budget(trial, spec):
    """约束型判据：超了就是过界，不是效率评价"""
    m = trial["metrics"]
    out = []
    if "max_turns" in spec:
        out.append(mk("budget", "max_turns", m["n_turns"] <= spec["max_turns"],
                      f"{m['n_turns']} 轮（上限 {spec['max_turns']}）"))
    if "max_tokens_in" in spec:
        out.append(mk("budget", "max_tokens_in", m["tokens_in"] <= spec["max_tokens_in"],
                      f"in={m['tokens_in']}（上限 {spec['max_tokens_in']}）"))
    if "max_tokens_out" in spec:
        out.append(mk("budget", "max_tokens_out", m["tokens_out"] <= spec["max_tokens_out"],
                      f"out={m['tokens_out']}（上限 {spec['max_tokens_out']}）"))
    return out


def judge_pressure(trial, spec):
    """第 2 幕：压力必须真的触发目标压缩层，否则整格判「未测过」"""
    layer = (spec or {}).get("compact_layer")
    if not layer:
        return []
    fired, detail = False, f"trace 里没有 layer={layer} 的压缩"
    for e in trial["events"]:
        if e["type"] == "compact" and e["payload"].get("layer") == layer:
            p = e["payload"]
            before = p.get("before", p.get("before_chars"))
            after = p.get("after", p.get("after_chars"))
            if before is not None and after is not None and after < before:
                fired, detail = True, f"{layer} {before}→{after}"
                break
            detail = f"{layer} 出现但没有缩减（{before}→{after}）"
    return [mk("pressure", "pressure_triggered", fired, detail)]


def run_graders(case, trial):
    """按用例 graders 列表聚合。verdict 优先级：not_triggered > fail > pass"""
    trial["metrics"] = trial.get("metrics") or compute_metrics(trial["events"])
    pressure = judge_pressure(trial, case.get("expect_pressure"))
    assertions = list(pressure)
    for spec in case.get("graders") or []:
        fn = JUDGES.get(spec.get("type"))
        if fn is None:
            assertions.append(mk(spec.get("type"), "unknown_grader", False,
                                 f"没有 {spec.get('type')!r} 这类判定器"))
        else:
            assertions += fn(trial, spec)
    trial["assertions"] = assertions
    if pressure and not pressure[0]["ok"]:
        trial["verdict"] = "not_triggered"
    elif any(not a["ok"] for a in assertions):
        trial["verdict"] = "fail"
    else:
        trial["verdict"] = "pass"
    return trial
