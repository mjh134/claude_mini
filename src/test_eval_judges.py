#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""eval_judges 的单测：手造事件 + 临时目录真实文件，全程不烧 LLM。

    python -m pytest test_eval_judges.py -q
    python test_eval_judges.py          # 不装 pytest 也能跑
"""

import tempfile
import sys
from pathlib import Path

import eval_judges as judges

USAGE = "Usage(cache_creation=None, input_tokens=4215, output_tokens=613)"
NEEDLE = {"value": "a3f9c2", "file": "needle_a.txt", "at": 3}

# 事件骨架的自增器：seq/ts 递增，模拟同一 writer 的落盘顺序
clock = {"seq": 0, "ts": 1000.0}


def ev(kind, turn=1, agent="main", cid=None, **payload):
    clock["seq"] += 1
    clock["ts"] += 0.5
    return {"ts": clock["ts"], "seq": clock["seq"], "agent_id": agent, "turn": turn,
            "type": kind, "correlation_id": cid, "payload": payload}


def tool(name, inp, cid="c1"):
    return ev("tool_call", cid=cid, ok=True, name=name, input=inp,
              output="ok", elapsed=0.1)


def make_trial(events, answer="", ws=None, needle=None):
    return {"case": "t", "suite": "capability", "trial_no": 1, "answer": answer,
            "events": events, "metrics": judges.compute_metrics(events),
            "ws_path": str(ws or ""), "needle": needle, "trace_path": "t.jsonl"}


def by_name(assertions):
    return {a["name"]: a["ok"] for a in assertions}


#============================================================
# ① 指标计算（只记不判）
#============================================================

def test_metrics_counts_turns_tools_tokens():
    events = [ev("llm_request", usage=USAGE),
              tool("bash", {"command": "echo hi"}),
              ev("task_event", turn=None, agent="runner-bg", action="submit"),
              ev("llm_request", turn=2, usage=USAGE)]
    m = judges.compute_metrics(events)
    assert m["n_turns"] == 2 and m["tool_call_count"] == 1 and m["llm_calls"] == 2
    assert m["tokens_in"] == 8430 and m["tokens_out"] == 1226
    assert m["wall_time"] >= 0


#============================================================
# ② state_check：探针回取 + 环境事实
#============================================================

def test_probe_answer_two_way():
    spec = {"probe": {"expect_exact": NEEDLE["value"]}}
    good = judges.judge_state_check(
        make_trial([tool("read_file", {"path": NEEDLE["file"]})],
                   answer="值是 a3f9c2，别的没记住"), spec)
    bad = judges.judge_state_check(make_trial([], answer="我不知道"), spec)
    assert good[0]["ok"] and not bad[0]["ok"]


def test_file_checks():
    ws = Path(tempfile.mkdtemp(prefix="judge_ws_"))
    (ws / "a.txt").write_text("hello", encoding="utf-8")
    exists = judges.judge_state_check(
        make_trial([], ws=ws), {"files": [{"path": "a.txt", "content_exact": "hello"}]})
    missing = judges.judge_state_check(
        make_trial([], ws=ws), {"files": [{"path": "missing.txt"}]})
    wrong = judges.judge_state_check(
        make_trial([], ws=ws), {"files": [{"path": "a.txt", "content_exact": "world"}]})
    assert exists[0]["ok"] and exists[1]["ok"]
    assert not missing[0]["ok"]
    assert wrong[0]["ok"] and not wrong[1]["ok"]


#============================================================
# ③ tool_calls：必需/禁用集、编造判据、重取代价
#============================================================

def test_required_any_and_forbidden():
    spec = {"required_any": ["write_file", "bash"], "forbidden": ["task_create"]}
    good = by_name(judges.judge_tool_calls(
        make_trial([tool("bash", {"command": "echo hi"})]), spec))
    sneaky = by_name(judges.judge_tool_calls(
        make_trial([tool("task_create", {})]), spec))
    assert good["required_any"] and good["forbidden:task_create"]
    assert not sneaky["required_any"] and not sneaky["forbidden:task_create"]


def test_no_fabrication_rule():
    """答对即可；答错时重读过=恢复、没重读=编造——只有 trace 能分出这两者"""
    spec = {"evidence_read": NEEDLE["file"]}
    reads = [tool("read_file", {"path": f"docs/{NEEDLE['file']}"})]
    confident = judges.judge_tool_calls(
        make_trial([], answer=f"值 {NEEDLE['value']}", needle=NEEDLE), spec)
    recovered = judges.judge_tool_calls(
        make_trial(reads, answer="答错了但重看过原文", needle=NEEDLE), spec)
    fabricated = judges.judge_tool_calls(
        make_trial([], answer="随便编一个 000000", needle=NEEDLE), spec)
    assert confident[0]["ok"] and recovered[0]["ok"] and not fabricated[0]["ok"]


def test_repeat_calls_cap():
    spec = {"max_same_call": 2}
    repeat = [tool("read_file", {"path": "f.txt"}) for _ in range(3)]
    varied = [tool("read_file", {"path": f"f{i}.txt"}) for i in range(3)]
    assert not judges.judge_tool_calls(make_trial(repeat), spec)[0]["ok"]
    assert judges.judge_tool_calls(make_trial(varied), spec)[0]["ok"]


def test_expect_all_items():
    spec = {"probe": {"expect_all": ["ITEM_1=aa", "ITEM_2=bb"]}}
    full = judges.judge_state_check(
        make_trial([], answer="清单：ITEM_1=aa / ITEM_2=bb"), spec)
    partial = judges.judge_state_check(
        make_trial([], answer="只记得 ITEM_1=aa"), spec)
    assert all(a["ok"] for a in full) and len(full) == 2
    assert partial[0]["ok"] and not partial[1]["ok"]


def test_evidence_read_after_marker():
    """知识更新格：旧读不算数，只有改写之后的重读才是证据"""
    spec = {"evidence_read": "cfg.txt"}
    old_read = [tool("read_file", {"path": "cfg.txt"})]          # ts 递增在前
    late = make_trial(old_read, answer="旧值", needle=NEEDLE)
    late["evidence_after_ts"] = 9999
    fresh = make_trial(old_read, answer="旧值", needle=NEEDLE)     # 无门槛=旧读也算
    assert not judges.judge_tool_calls(late, spec)[0]["ok"]
    assert judges.judge_tool_calls(fresh, spec)[0]["ok"]


#============================================================
# ④ budget：约束型判据
#============================================================

def test_budget_limits():
    events = [ev("llm_request", turn=t, usage=USAGE) for t in (1, 2, 3)]
    trial = make_trial(events)
    spec = {"max_turns": 3, "max_tokens_in": 13000, "max_tokens_out": 100}
    got = by_name(judges.judge_budget(trial, spec))
    assert got["max_turns"] and got["max_tokens_in"] and not got["max_tokens_out"]


#============================================================
# ⑤ pressure：压力必须真触发，否则整格「未测过」
#============================================================

def test_pressure_layer_detection():
    spec = {"compact_layer": "llm"}
    fired = make_trial([ev("compact", layer="llm",
                           before_chars=210000, after_chars=150000)])
    wrong_layer = make_trial([ev("compact", layer="snip", before=60, after=30)])
    no_shrink = make_trial([ev("compact", layer="llm",
                               before_chars=100, after_chars=100)])
    assert judges.judge_pressure(fired, spec)[0]["ok"]
    assert not judges.judge_pressure(wrong_layer, spec)[0]["ok"]
    assert not judges.judge_pressure(no_shrink, spec)[0]["ok"]
    assert judges.judge_pressure(make_trial([]), None) == [], "无压力格不该产生断言"


def test_verdict_precedence():
    """探针蒙对但压力没触发 = not_triggered，不许算 pass——假通过比失败更骗人"""
    case = {"expect_pressure": {"compact_layer": "llm"},
            "graders": [{"type": "state_check",
                         "probe": {"expect_exact": NEEDLE["value"]}}]}
    fired = [ev("compact", layer="llm", before_chars=200000, after_chars=90000)]
    assert judges.run_graders(case, make_trial(fired, answer=f"{NEEDLE['value']} 在这"))["verdict"] == "pass"
    assert judges.run_graders(case, make_trial(fired, answer="不知道"))["verdict"] == "fail"
    lucky = judges.run_graders(case, make_trial([], answer=NEEDLE["value"]))
    assert lucky["verdict"] == "not_triggered"


def test_unknown_grader_fails_loud():
    t = judges.run_graders({"graders": [{"type": "llm_rubric"}]}, make_trial([]))
    assert t["verdict"] == "fail" and t["assertions"][0]["name"] == "unknown_grader"


def test_sanity_case_without_pressure():
    case = {"graders": [{"type": "state_check",
                         "probe": {"expect_exact": NEEDLE["value"]}}]}
    t = judges.run_graders(case, make_trial([], answer=NEEDLE["value"]))
    assert t["verdict"] == "pass"


#============================================================
# 裸跑入口(不装 pytest 也能全量执行)
#============================================================

if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(failed)
