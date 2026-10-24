#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""task_runner.py 后台任务日志(kill 恢复)的回归测试。

    python -m pytest test_task_runner.py -q
    python test_task_runner.py          # 不装 pytest 也能跑

## 这个文件守住什么

项 7 的验收面:后台任务"经得起一次强杀"。三组不变量:

  · 登记即落盘——提交的命令/描述原样进日志,中断交代才谈得上"含原命令";
  · 收尾即删盘——"盘上存在 = 未收尾"是 _load 零分支的前提,
    正常跑完的任务不许被误报成中断;
  · 重启即交代——遗留条目转成邮箱通知(顺现成 <task_notification> 通道),
    领走后盘清空不重复交代;坏文件降级改名,不许炸构造函数。

测试全部走临时目录,不碰真 .task/;trace 单例注入临时目录,不污染项目 trace/。
"""

import inspect
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import trace as trace_mod
from trace import Trace
from task_runner import TaskRunner


def new_runner(tmp_dir, **kw):
    """store_path 指进临时目录的 runner;trace 单例同步隔离,emit 不落项目 trace/"""
    trace_mod.instance = Trace(trace_dir=str(Path(tmp_dir) / "trace"))
    return TaskRunner(store_path=str(Path(tmp_dir) / "bg_tasks.json"), **kw)


def read_store(path):
    """读盘上还挂着的未收尾任务(文件不存在视为空库)"""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))["tasks"]


#============================================================
# ① 登记即落盘:命令/描述原样进文件 —— 中断交代"含原命令"的数据来源
#============================================================

def test_submit_bash_persists_command(tmp_path):
    r = new_runner(tmp_path)
    tid = r._new_task("bash")
    r._persist_add(tid, "bash", 'python -c "import time; time.sleep(300)"')
    entry = read_store(tmp_path / "bg_tasks.json")[tid]
    assert entry["desc"] == 'python -c "import time; time.sleep(300)"', "原命令必须原样落盘"
    assert entry["kind"] == "bash"
    assert entry["submitted_at"], "提交时刻必须有值(格式不锁死,给人看)"


def test_submit_agent_persists_desc(tmp_path):
    r = new_runner(tmp_path)
    tid = r._new_task("agent")
    r._persist_add(tid, "agent", "帮我整理笔记")
    entry = read_store(tmp_path / "bg_tasks.json")[tid]
    assert entry["desc"] == "帮我整理笔记", "agent 任务的描述(子agent 的 prompt)必须落盘"


#============================================================
# ② 收尾即删盘:正常跑完的任务不许被下次启动误报成中断
#============================================================

def test_finalize_removes_entry(tmp_path):
    r = new_runner(tmp_path)
    tid = r._new_task("bash")
    r._persist_add(tid, "bash", "sleep 300")
    assert tid in read_store(tmp_path / "bg_tasks.json")
    assert r._finalize(tid, "completed", "done") is True
    assert read_store(tmp_path / "bg_tasks.json") == {}, "收尾后盘上不许留条目"
    #幂等:再收一次尾走 False 路径,不许动盘(先收尾的那个已经删过了)
    assert r._finalize(tid, "failed", "again") is False
    assert read_store(tmp_path / "bg_tasks.json") == {}


#============================================================
# ③ 重启即交代:遗留条目 → 邮箱通知(含原命令) → 领走后盘清空
#============================================================

def test_leftover_becomes_notification(tmp_path):
    store = tmp_path / "bg_tasks.json"
    store.write_text(json.dumps({"tasks": {
        "bg_0002": {"kind": "bash", "desc": "sleep 300",
                    "submitted_at": "2026-10-24 12:00:00"},
    }}, ensure_ascii=False), encoding="utf-8")
    r = new_runner(tmp_path)                    #构造即加载
    assert r.has_completed(), "遗留条目必须转成可领取的通知"
    got = r.collect()
    assert set(got) == {"bg_0002"}
    rec = got["bg_0002"]
    assert rec["status"] == "interrupted"
    assert rec["kind"] == "bash"
    assert "sleep 300" in rec["output"], "中断交代必须含原命令(验收硬要求)"
    assert "中断" in rec["output"], "正文要说清是中断,不是失败也不是完成"
    #领走后盘清空:下次启动不许把同一批中断再交代一遍
    assert read_store(store) == {}


def test_no_repeat_across_restarts(tmp_path):
    store = tmp_path / "bg_tasks.json"
    store.write_text(json.dumps({"tasks": {
        "bg_0001": {"kind": "agent", "desc": "整理笔记",
                    "submitted_at": "2026-10-24 09:00:00"},
    }}, ensure_ascii=False), encoding="utf-8")
    first = new_runner(tmp_path)
    assert first.has_completed()
    first.collect()
    #第二次"重启":盘已清,不再有中断可交代 —— 同一件事只说一次
    second = new_runner(tmp_path)
    assert not second.has_completed()
    assert second.collect() == {}


def test_interrupted_not_counted_as_running(tmp_path):
    """interrupted 记录不许混进 running()/看门狗的视野:它已经死了,不是在跑"""
    store = tmp_path / "bg_tasks.json"
    store.write_text(json.dumps({"tasks": {
        "bg_0003": {"kind": "bash", "desc": "sleep 300", "submitted_at": "2026-10-24 12:00:00"},
    }}, ensure_ascii=False), encoding="utf-8")
    r = new_runner(tmp_path)
    assert r.running() == [], "中断任务不是在跑的任务"
    assert r.reap_dead() == [], "看门狗不许对中断任务补账(它已有通知)"


#============================================================
# ④ 坏文件降级:不炸构造、无通知、改名 .bad 留证
#============================================================

def test_corrupt_file_degrades(tmp_path):
    store = tmp_path / "bg_tasks.json"
    store.write_text("{not json", encoding="utf-8")
    r = new_runner(tmp_path)
    assert not r.has_completed(), "读不出来的日志给不出通知,但构造不许炸"
    assert not store.exists(), "坏文件改名让路,免得每次启动撞同一堵墙"
    assert Path(str(store) + ".bad").exists(), "原样留证"


#============================================================
# ⑤ 完整生命周期(真线程):submit_agent 带 desc → 落盘 → 跑完 → collect → 盘删
#============================================================

def test_agent_full_lifecycle(tmp_path):
    r = new_runner(tmp_path)
    entered = threading.Event()     #work 已开跑:保证断言"盘上有"时线程活着
    release = threading.Event()     #放行 work 返回:收尾时机由测试攥着,不赌线程快慢

    def work(tid):
        entered.set()
        release.wait(5)
        return "产出"

    tid = r.submit_agent(work, desc="帮我整理笔记")
    assert tid is not None
    assert entered.wait(5), "后台线程必须跑起来"
    entry = read_store(tmp_path / "bg_tasks.json")[tid]
    assert entry["desc"] == "帮我整理笔记", "跑完之前盘上必须挂着登记"

    release.set()
    for _ in range(200):            #等收尾(线程侧 finalize),上限 10 秒
        if r.has_completed():
            break
        time.sleep(0.05)
    got = r.collect()
    assert got[tid]["status"] == "completed"
    assert got[tid]["output"] == "产出"
    assert read_store(tmp_path / "bg_tasks.json") == {}, "跑完的任务盘上不许留条目"


#============================================================
# ⑥ 不传 store_path 就完全不碰盘:bench/eval 的隔离保证
#============================================================

def test_no_store_touches_nothing(tmp_path):
    trace_mod.instance = Trace(trace_dir=str(tmp_path / "trace"))
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        r = TaskRunner()            #默认 store_path=None
        tid = r._new_task("bash")
        r._persist_add(tid, "bash", "whatever")     #必须 no-op
        assert r._finalize(tid, "completed", "ok") is True
        stray = [str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")
                 if not str(p.relative_to(tmp_path)).startswith("trace")]
        assert stray == [], f"除 trace/ 外不许产生任何文件,实际:{stray}"
    finally:
        os.chdir(old)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        d = Path(tempfile.mkdtemp(prefix="task_runner_test_"))
        try:
            fn(d)
            print(f"{name}: PASS")
        except AssertionError as e:
            fails += 1
            print(f"{name}: FAIL {e}")
        except Exception as e:
            fails += 1
            print(f"{name}: ERROR {type(e).__name__}: {e}")
    print("全绿" if fails == 0 else f"{fails} 个失败")
    sys.exit(1 if fails else 0)
