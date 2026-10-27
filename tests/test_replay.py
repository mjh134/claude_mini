#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""replay.py 的验收测试，本文件先于 replay.py 存在——它就是靶子。

    python -m pytest test_replay.py -q
    python test_replay.py          # 不装 pytest 也能跑

只断契约（完整重演 / 两代格式 / 坏尾不炸 / 确定性 / 只读），不管版式。
"""

#直跑自举:把 src/ 加进 import 路径(pytest 模式走 tests/conftest.py,不靠这两行)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "src"))

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from trace import Trace

import replay as replay_mod

SRC = Path(__file__).resolve().parent.parent / "src"
#2026-10-27 测试从 src/ 迁入 tests/ 后，"被测脚本和测试同目录"不再成立，显式指向 src/
# Windows 管道默认 GBK,子进程不显式给 utf-8 就会解码错位丢信息(踩过的雷)
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")

# 一条 llm_request 的 usage:真实落盘长这样——Usage 对象在写侧就被转成了 repr 字符串
USAGE = "Usage(cache_creation=None, input_tokens=4215, output_tokens=613)"


def run_replay(*args, cwd=None):
    """把 replay 当独立 CLI 跑一次。脚本路径用绝对式——cwd 要能指到临时目录去验只读"""
    r = subprocess.run([sys.executable, str(SRC / "replay.py"), *args],
                       cwd=str(cwd or SRC), env=ENV, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    # 脚本不存在时 python 自己退 2，不先排这一层下面的断言会假绿
    if "can't open file" in r.stderr and "replay.py" in r.stderr:
        raise AssertionError(f"replay.py 跑不起来: {r.stderr.strip()}")
    return r


def make_events(t):
    """用写侧 Trace 造五类事件，含一条 deny 和一条 ok=False 的失败现场"""
    with t.span("llm_request", agent_id="main", turn=1) as s:
        s.set("messages_sent", {"mode": "full",
                                "messages": [{"role": "user", "content": "跑个自测"}]})
        s.set("usage", USAGE)
    t.emit("hook_decision", {"hook": "PreToolUse", "tool": "bash",
                             "decision": "pass", "reason": None},
           agent_id="main", turn=1, correlation_id="call_ok")
    t.emit("hook_decision", {"hook": "PreToolUse", "tool": "write_file",
                             "decision": "deny", "reason": "角色禁止写 memory/"},
           agent_id="main", turn=1, correlation_id="call_deny")
    with t.span("tool_call", agent_id="main", turn=1, correlation_id="call_ok") as s:
        s.set("name", "bash")
        s.set("input", {"command": "echo trace_e2e_ok"})
        s.set("output", "trace_e2e_ok\n")
    t.emit("task_event", {"action": "submit", "kind": "bash", "command": "dir"},
           agent_id="runner-bg", correlation_id="bg_0001")
    t.emit("task_event", {"action": "finalize", "status": "completed"},
           agent_id="runner-bg", correlation_id="bg_0001")
    try:
        with t.span("tool_call", agent_id="main", turn=2, correlation_id="call_deny") as s:
            s.set("name", "read_file")
            raise FileNotFoundError("没这个文件")
    except FileNotFoundError:
        # span 让异常原样穿出是它的契约;这里收住只为了继续造后面的事件
        pass
    t.emit("compact", {"layer": "snip", "before": 7, "after": 5},
           agent_id="main", turn=2)
    # reactive 压缩重写 history 后，下一条请求游标越界——写侧重记全量并标 resnapshot
    with t.span("compact", agent_id="main", turn=3) as s:
        s.set("layer", "reactive")
        s.set("before", 12)
        s.set("after", 6)
    with t.span("llm_request", agent_id="main", turn=3) as s:
        s.set("messages_sent", {"mode": "resnapshot",
                                "messages": [{"role": "user"}] * 3})
        s.set("usage", USAGE)


def build_trace(one_line=False):
    """产出一个可回放的 trace 文件路径。one_line=True 时输出上一代一行式格式"""
    d = Path(tempfile.mkdtemp(prefix="replay_test_"))
    t = Trace(trace_dir=str(d), secrets=[])
    make_events(t)
    t.close()
    path = Path(t.path)
    if not one_line:
        return d, path
    parsed = read_events(path)
    legacy = d / "legacy.jsonl"
    legacy.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in parsed),
                      encoding="utf-8")
    return d, legacy


def read_events(path):
    """JSON 文档流读法（和 test_trace.py 同源）——两代格式通吃的关键"""
    raw = Path(path).read_text(encoding="utf-8")
    dec, events, pos = json.JSONDecoder(), [], 0
    while pos < len(raw):
        while pos < len(raw) and raw[pos] in " \t\r\n":
            pos += 1
        if pos >= len(raw):
            break
        obj, pos = dec.raw_decode(raw, pos)
        events.append(obj)
    return events


def list_tree(root):
    """目录树快照,用来证明 replay 没落盘"""
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


def test_parse_usage_both_generations():
    """写侧 usage 有两代形态：旧 repr 字符串与新 dict，读侧都要能抠"""
    assert replay_mod.parse_usage(USAGE) == {"input_tokens": 4215, "output_tokens": 613}
    assert replay_mod.parse_usage({"input": 100, "output": 7}) == {
        "input_tokens": 100, "output_tokens": 7}


#============================================================
# ① 完整重演:每条事件的关键事实都得见诸输出
#============================================================

# (payload 里的标志性内容, 为什么它必须出现)
SHOULD_APPEAR = [
    ("trace_e2e_ok", "工具入参/出参——重演不看命令内容就是空壳"),
    ("bash", "工具名:模型选了哪个工具是回放的第一信息"),
    ("write_file", "被拒的调用也要出现,而且要看得见"),
    ("deny", "hook 判定:安全维度的原始证据,漏了就无从审"),
    ("角色禁止写 memory/", "拒绝理由:光有 pass/deny 不够,人要知道为什么"),
    ("runner-bg", "后台任务身份:跨身份的因果链靠它分轨"),
    ("bg_0001", "任务 id:submit 与 finalize 就靠它配对"),
    ("finalize", "任务的终局——回放里它是「这轮到底干完了没」的答案"),
    ("没这个文件", "失败现场:ok=False 的事件不许被渲染成成功"),
    ("4215", "token 汇总的来源——usage 是 repr 字符串也得抠出来"),
    ("613", "输出 token 数"),
    ("snip", "压缩层：上下文被动过，回放必须留痕"),
    ("reactive", "reactive 压缩层：resnapshot 锚点的因"),
    ("resnapshot", "重快照锚点必须看得见——写侧靠它说明全量重记发生了"),
]


def test_full_replay_shows_every_event():
    d, path = build_trace()
    r = run_replay(str(path))
    assert r.returncode == 0, f"退出码 {r.returncode}\n{r.stderr}"
    out = r.stdout + r.stderr
    missing = [why for needle, why in SHOULD_APPEAR if needle not in out]
    assert not missing, f"这些事实没出现在输出里: {missing}"


def test_determinism_two_runs_identical():
    """同一文件跑两次必须一字不差——项3 要拿 stdout 当断言对象"""
    d, path = build_trace()
    a, b = run_replay(str(path)), run_replay(str(path))
    assert a.returncode == 0 and b.returncode == 0, "先确认它真的跑成了"
    assert a.stdout == b.stdout, "输出不确定,后续任何判定器都不可复跑"


def test_legacy_one_line_format_readable():
    """上一代一行式 JSONL:同一套读法必须照样能放(ADR 0001 放弃方案"单行 JSONL"的兑现)"""
    d, path = build_trace(one_line=True)
    r = run_replay(str(path))
    assert r.returncode == 0, r.stderr
    assert "trace_e2e_ok" in r.stdout + r.stderr, "老格式读不出来或读丢了内容"


#============================================================
# ② 鲁棒降级:坏尾、空文件、读不了的参数
#============================================================

def test_broken_tail_degrades():
    """模拟 kill 留下的半个事件:前面的照常重演,末尾只提示,不许抛、不许非零退出"""
    d, path = build_trace()
    with path.open("a", encoding="utf-8") as f:
        f.write('\n{\n  "seq": 999, "type": "tool_cal')
    r = run_replay(str(path))
    assert r.returncode == 0, f"坏尾不该让回放失败: {r.stderr}"
    assert "Traceback" not in r.stderr, "异常穿到 CLI 之外了"
    assert "trace_e2e_ok" in r.stdout + r.stderr, "前半段还得照常重演"


def test_empty_file_is_not_an_error():
    """0 字节的 trace(建了文件就断)在真实目录里存在过,不是异常场景"""
    d = Path(tempfile.mkdtemp(prefix="replay_test_"))
    path = d / "sess-empty.jsonl"
    path.write_text("", encoding="utf-8")
    r = run_replay(str(path))
    assert r.returncode == 0, f"空文件不该报错: {r.stderr}"
    assert "Traceback" not in r.stderr


def test_missing_file_friendly_error():
    """文件不存在是系统边界(用户输入):友好报错 + 非零退出,禁止裸 traceback"""
    r = run_replay("no_such_trace.jsonl")
    assert r.returncode != 0, "读不了还不报错,调用方会以为重演成功了"
    assert "Traceback" not in r.stderr, f"边界没拦住:{r.stderr}"


def test_no_argument_friendly_error():
    r = run_replay()
    assert r.returncode != 0
    assert "Traceback" not in r.stderr, "缺参数要提示怎么用,不是崩"


def test_resnapshot_marks_anchor_without_rebuild():
    """resnapshot 只当锚点标注，不许把之前 delta 的条数累加进来——重建即造假"""
    d, path = build_trace()
    evs = read_events(path)
    snap = [e for e in evs
            if e["payload"].get("messages_sent", {}).get("mode") == "resnapshot"][0]
    n = len(snap["payload"]["messages_sent"]["messages"])
    line = next(l for l in run_replay(str(path)).stdout.splitlines()
                if "mode=resnapshot" in l)
    assert f"msgs={n}" in line, f"锚点只该报本条的 {n} 条，实际：{line}"
    assert "锚点" in line, "resnapshot 得标出它是新锚点"


#============================================================
# ③ 边界：只读、且不碰写侧和业务模块
#============================================================

def test_replay_is_read_only():
    d, path = build_trace()
    cwd = Path(tempfile.mkdtemp(prefix="replay_test_cwd_"))
    before_trace, before_cwd = list_tree(d), list_tree(cwd)
    r = run_replay(str(path), cwd=cwd)
    assert r.returncode == 0, r.stderr
    assert list_tree(d) == before_trace, "replay 动了 trace 目录——它是只读的"
    assert list_tree(cwd) == before_cwd, "replay 落了文件——它不该写盘"


FORBIDDEN_IMPORTS = {"trace", "claude", "llm", "TOOLS", "HOOKS", "PERMISSIONS",
                    "compact", "task", "task_runner", "corn_job", "team",
                    "dispatcher", "skill", "memory", "message", "main", "ui"}


def test_no_business_imports():
    """读侧不许依赖写侧/业务模块:import trace 会牵出单例与 .env 指纹表,
    replay 只该拿一个文件路径当输入。决策见回复,不 import ui 同理(独立 CLI)"""
    src = (SRC / "replay.py").read_text(encoding="utf-8")
    imported = {m.split(".")[0] for m in
                re.findall(r"^\s*(?:import|from)\s+([\w\.]+)", src, re.M)}
    bad = imported & FORBIDDEN_IMPORTS
    assert not bad, f"replay 不该 import 这些: {sorted(bad)}"


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
    sys.exit(1 if failed else 0)
