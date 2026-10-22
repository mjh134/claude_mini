#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""trace.py 观测面的回归测试。

    python -m pytest test_trace.py -q
    python test_trace.py          # 不装 pytest 也能跑,自己打印结果

## 这个文件守住什么

trace 是 replay(项2)和红队证据链(阶段2)的数据地基,三组不变量:

  · 脱敏无旁路——凡落盘内容必经 sanitize。e2e 实测踩过:SDK 对象被
    json.dumps(default=str) 在 sanitize **之后**才转文本,密钥原样落盘;
  · 观测不许带崩主链路——建不了目录就降级 no-op,emit 永远不上抛;
  · 事件骨架是回放的秩序——seq 并发不撞号,span 成败都记账。

测试全部走临时目录和假指纹表,不碰真 trace/ 和真 .env。
"""

import json
import sys
import tempfile
import threading
from pathlib import Path

from trace import Trace

# 假指纹表:只放"凭据形态"的 key,值都够 6 字符线
FAKE_SECRETS = [("API_KEY", "sk-fake-secret-123456"),
                ("DB_TOKEN", "tok_abc123456")]


def new_trace(secrets=FAKE_SECRETS):
    """写进临时目录的 Trace。目录不主动清理——测试件小,留给系统 temp"""
    d = tempfile.mkdtemp(prefix="trace_test_")
    return Trace(trace_dir=d, secrets=secrets)


def read_events(t):
    """按 JSON 文档流解析落盘文件——和 replay 约定的读法一致"""
    raw = Path(t.path).read_text(encoding="utf-8")
    dec, events, pos = json.JSONDecoder(), [], 0
    while pos < len(raw):
        while pos < len(raw) and raw[pos] in " \t\r\n":
            pos += 1
        if pos >= len(raw):
            break
        obj, pos = dec.raw_decode(raw, pos)
        events.append(obj)
    return events


def raw_text(t):
    return Path(t.path).read_text(encoding="utf-8")


#============================================================
# ① 脱敏:sanitize_text 的用例表 (输入, 期望结果, 为什么)
#============================================================

SANITIZE_CASES = [
    (f"config api = {FAKE_SECRETS[0][1]}",
     "[REDACTED:API_KEY]", "凭据值被遮,标记带 key 名"),
    ("memory/long_term/soul.md",
     "memory/long_term/soul.md", "目录名不是凭据,必须原样可读"),
    ("sk-liveabcdefghijklmnop",
     "[REDACTED:pattern]", "没进指纹表但长得像密钥的,正则兜底"),
    ("ordinary sentence about tokens",
     "ordinary sentence about tokens", "无关文本不误伤(token 一词不在指纹里)"),
]


def test_sanitize_table():
    t = new_trace()
    for text, expect, why in SANITIZE_CASES:
        got = t.sanitize_text(text)
        assert expect in got, f"{why}: 期望含 {expect!r},实际 {got!r}"


def test_sanitize_object_bypass():
    """非 JSON 原生类型(SDK block 那种)必须先过 sanitize 再进 dumps——旁路曾是真泄露"""
    class _Block:
        def __repr__(self):
            return f"<Block text={FAKE_SECRETS[1][1]!r}>"

    t = new_trace()
    t.emit("llm_request", {"messages": [_Block()]}, agent_id="main", turn=1)
    raw = raw_text(t)
    assert FAKE_SECRETS[1][1] not in raw, "对象 repr 里的密钥被原样落盘了"
    assert "[REDACTED:DB_TOKEN]" in raw, "应出现遮罩标记"


def test_load_secrets_filters_by_key_name():
    """指纹表只收凭据类 key;短值、配置值都进不来。拿假 .env 验,不碰真的"""
    env = Path(tempfile.mkdtemp(prefix="trace_test_env_")) / ".env"
    env.write_text(
        "ANTHROPIC_API_KEY=sk-real-looking-abcdef\n"
        "MODEL_ID=some-model-name\n"          # 非凭据 key:配置,放行
        "MEMORY_DIR=memory\n"                  # 非凭据 key:目录名,放行
        "SHORT_TOKEN=abc\n",                   # 凭据 key 但值 <6:会误伤,不收
        encoding="utf-8")
    t = new_trace(secrets=[])
    found = dict(t.load_secrets(env))
    assert set(found) == {"ANTHROPIC_API_KEY"}, f"指纹表收错: {set(found)}"


#============================================================
# ② 事件骨架与 span
#============================================================

def test_skeleton_and_optional_turn():
    t = new_trace()
    t.emit("task_event", {"action": "submit"}, agent_id="runner-bg",
           correlation_id="bg_0001")        # 任务级:没有 turn
    ev = read_events(t)[0]
    for key in ("ts", "seq", "agent_id", "type", "payload"):
        assert key in ev, f"骨架缺 {key}"
    assert ev["turn"] is None and ev["correlation_id"] == "bg_0001"


def test_span_success_records_elapsed():
    t = new_trace()
    with t.span("tool_call", agent_id="main", turn=2, correlation_id="tu_1") as s:
        s.set("name", "bash")
    ev = read_events(t)[0]
    assert ev["payload"]["ok"] is True and ev["payload"]["name"] == "bash"
    assert ev["payload"]["elapsed"] >= 0 and ev["correlation_id"] == "tu_1"


def test_span_exception_records_and_reraises():
    """块里炸:失败也要成一条事件(ok=False+错误原文),然后异常原样穿出去"""
    t = new_trace()
    raised = False
    try:
        with t.span("llm_request", agent_id="main", turn=1) as s:
            s.set("messages_sent", {"mode": "full", "messages": []})
            raise ValueError("boom")
    except ValueError:
        raised = True
    assert raised, "span 不许吞异常"
    ev = read_events(t)[0]
    assert ev["payload"]["ok"] is False and "boom" in ev["payload"]["error"]
    assert ev["payload"]["messages_sent"]["mode"] == "full", "半途补的字段也要记上"


def test_truncation_keeps_head():
    t = new_trace()
    long_text = "A" * 500
    t.emit("tool_call", {"output": long_text}, agent_id="main", turn=1)
    out = read_events(t)[0]["payload"]["output"]
    assert out.startswith("AAAA") and "截断" in out, "超长应截头留标记"


#============================================================
# ③ 并发与降级
#============================================================

def test_concurrent_seq_unique():
    """两条线程同时写:seq 不撞号、每条事件完整——replay 靠 seq 定全序"""
    t = new_trace()
    def worker(n):
        for i in range(50):
            t.emit("compact", {"layer": "micro", "worker": n, "i": i},
                   agent_id="main", turn=1)
    threads = [threading.Thread(target=worker, args=(n,)) for n in (1, 2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    events = read_events(t)
    assert len(events) == 100, f"应 100 条,实际 {len(events)}"
    seqs = [e["seq"] for e in events]
    assert len(set(seqs)) == 100, "seq 撞号了"


def test_broken_dir_degrades():
    """trace_dir 指到一个普通文件:建不了目录 → enabled=False,之后 emit/close 都不炸"""
    blocker = Path(tempfile.mkdtemp(prefix="trace_test_block_")) / "afile"
    blocker.write_text("not a dir", encoding="utf-8")
    t = Trace(trace_dir=str(blocker), secrets=[])
    assert t.enabled is False, "初始化失败应降级"
    t.emit("tool_call", {"name": "x"}, agent_id="main", turn=1)   # 不许抛
    t.close()


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
