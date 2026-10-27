#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""取消传播(ROADMAP 项 8)的回归测试:bash 树杀/轮询取消、cancel_all、run() 检查点。

    python -m pytest test_cancel.py -q        # pytest 模式
    python test_cancel.py                     # 直接运行模式(逐个调用,全过打 PASS)

全程不烧 LLM:检查点测试用 __new__ 绕过 ClaudeMini.__init__(不建 LLM client/MCP)。
"""

#直跑自举:把 src/ 加进 import 路径(pytest 模式走 tests/conftest.py,不靠这两行)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "src"))

import threading
import time

from bash_exec import execute_bash
from task_runner import TaskRunner


#--- execute_bash ---

def test_execute_bash_without_event_unchanged():
    """不传 stop_event:正常路径行为与从前一致(退出码/输出原样)"""
    out, code = execute_bash("echo hello")
    assert code == 0 and "hello" in out


def test_execute_bash_nonzero_exit_still_reports():
    """非零退出:stdout+stderr 合并的现场保留(finally 不得干扰正常收尾)"""
    out, code = execute_bash("echo before-error; exit 3")
    assert code == 3 and "before-error" in out


def test_execute_bash_timeout_still_works():
    """超时路径文案不变,且轮询改造后仍按时杀树"""
    t0 = time.monotonic()
    out, code = execute_bash("sleep 30", timeout=1)
    elapsed = time.monotonic() - t0
    assert code == -1 and "超时" in out
    assert 1 <= elapsed < 10, f"超时应按时生效,实际 {elapsed:.1f}s"


def test_execute_bash_cancel_kills_tree():
    """置位信号:sleep 长命令应在一个轮询档+余量内返回取消文案, exit -1"""
    ev = threading.Event()
    ev.set()
    t0 = time.monotonic()
    out, code = execute_bash("sleep 30", timeout=60, stop_event=ev)
    elapsed = time.monotonic() - t0
    assert code == -1 and "取消" in out
    assert elapsed < 5, f"取消应在轮询档内生效,实际 {elapsed:.1f}s"


def test_execute_bash_cancel_midway():
    """跑起来之后才置位:模拟真实的'先提交后取消'时序"""
    ev = threading.Event()
    result = {}

    def run():
        result["out"], result["code"] = execute_bash(
            "sleep 30", timeout=60, stop_event=ev)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(1.0)             # 让命令真的跑进 sleep
    ev.set()
    t.join(timeout=10)
    assert not t.is_alive(), "取消后线程必须收尾"
    assert result["code"] == -1 and "取消" in result["out"]


#--- TaskRunner.cancel_all ---

def _wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_cancel_all_sets_event_snapshots_and_finalizes():
    """cancel_all:置 event、快照对、被取消的 bash 在档内收尾成 failed+取消文案"""
    r = TaskRunner()
    tid = r.submit_bash("sleep 30")
    assert _wait_until(lambda: r.running() == [tid]), "任务应先真跑起来"
    got = r.cancel_all()
    assert got == [tid]
    assert r.stop_event.is_set()
    assert _wait_until(lambda: not r.running()), "被取消的 bash 应在档内收尾"
    rec = r.collect()[tid]
    assert rec["status"] == "failed" and "取消" in rec["output"]


def test_cancel_all_idempotent():
    """重复调用是 no-op:event 仍置位,不炸、快照可空(已停的不算)"""
    r = TaskRunner()
    tid = r.submit_bash("echo quick")
    _wait_until(lambda: not r.running())
    assert r.cancel_all() == []     # 没有在跑的,快照空,event 仍被置位
    assert r.stop_event.is_set()
    r.cancel_all()                  # 再调不炸
    assert r.stop_event.is_set()


def test_shared_event_reaches_second_runner():
    """共享 event:cancel 主 runner,从 runner 名下的 bash 同样被杀(进程级广播)"""
    shared = threading.Event()
    main = TaskRunner(stop_event=shared)
    other = TaskRunner(id_prefix="bg_other", stop_event=shared)
    tid = other.submit_bash("sleep 30")
    assert _wait_until(lambda: other.running() == [tid])
    assert main.cancel_all() == []  # 主 runner 侧广播(自己没有在跑的)
    assert _wait_until(lambda: not other.running()), \
        "共享同一 event 的 runner 必须同样收到停止信号"
    rec = other.collect()[tid]
    assert "取消" in rec["output"]


#--- ClaudeMini.run() 检查点 ---

def test_run_checkpoint_stops_before_llm():
    """信号已置位:run() 第一圈就返回取消说明,不发请求、trace_turn 不动。

    __new__ 绕过 __init__:检查点在 collect/trace 之前,只需要 task_runner。
    """
    import claude as claude_mod
    cm = claude_mod.ClaudeMini.__new__(claude_mod.ClaudeMini)
    cm.task_runner = TaskRunner()
    cm.trace_turn = 0
    cm.task_runner.stop_event.set()
    out = cm.run([])
    assert "取消" in out
    assert cm.trace_turn == 0, "检查点应拦在第一圈 LLM 请求之前"


def test_run_checkpoint_not_triggered_when_clear():
    """信号未置位:检查点必须放行(否则所有正常对话全挂)。

    放行后会走到 collect —— drains_results 为 False 时不碰 runner,
    再往后才碰 LLM,所以这里用异常('测试桩')证明检查点没有拦它。
    """
    import claude as claude_mod
    cm = claude_mod.ClaudeMini.__new__(claude_mod.ClaudeMini)
    cm.task_runner = TaskRunner()
    cm.trace_turn = 0
    cm.drains_results = False
    try:
        cm.run([])
    except AttributeError:
        pass    # 走过检查点后,缺后续属性而炸 = 检查点放行了(符合预期)
    else:
        raise AssertionError("信号未置位时检查点不该拦,应继续往后跑")
    assert cm.trace_turn == 1, "放行后 trace_turn 应已 +1"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
