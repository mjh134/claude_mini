#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""session_store(ROADMAP 项 9)的回归测试:落盘/恢复的保真与降级。

    python -m pytest test_session_store.py -q     # pytest 模式
    python test_session_store.py                  # 直接运行模式

全程不烧 LLM。
"""

#直跑自举:把 src/ 加进 import 路径(pytest 模式走 tests/conftest.py,不靠这两行)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "src"))

import json

from anthropic.types import TextBlock, ToolUseBlock

from session_store import save_session, load_session


def test_roundtrip_plain_text(tmp_path):
    """纯文本消息:原样往返"""
    p = tmp_path / "s.json"
    history = [{"role": "user", "content": "你好"},
               {"role": "assistant", "content": "你好,有什么可以帮你?"}]
    save_session(history, p)
    assert load_session(p) == history


def test_roundtrip_tool_use_pairs(tmp_path):
    """tool_use 与 tool_result 成对的消息:块 dict 原样往返(关键约束 1 的形状)"""
    p = tmp_path / "s.json"
    history = [
        {"role": "user", "content": "跑一下 ls"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "好的"},
            {"type": "tool_use", "id": "tu_01", "name": "bash",
             "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_01",
             "content": "file_a.txt\nfile_b.txt"},
        ]},
    ]
    save_session(history, p)
    assert load_session(p) == history


def test_roundtrip_thinking_keeps_signature(tmp_path):
    """thinking 块连 signature 一起保真:截掉反而制造非法 history"""
    p = tmp_path / "s.json"
    history = [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "先想一下", "signature": "sig-abc"}]}]
    save_session(history, p)
    loaded = load_session(p)
    assert loaded[0]["content"][0]["signature"] == "sig-abc"


def test_sdk_block_objects_dumped(tmp_path):
    """混入真实 SDK 块对象:save 时 model_dump() 转 dict,load 回来是纯 JSON 结构"""
    p = tmp_path / "s.json"
    history = [{"role": "assistant", "content": [
        TextBlock(type="text", text="开工"),
        ToolUseBlock(type="tool_use", id="tu_02", name="bash",
                     input={"command": "echo hi"}),
    ]}]
    save_session(history, p)
    # 盘上必须是纯 JSON(直接 dumps 不炸 = save 没漏对象)
    raw = json.loads(p.read_text(encoding="utf-8"))
    blocks = raw[0]["content"]
    assert blocks[0]["type"] == "text" and blocks[0]["text"] == "开工"
    assert blocks[1]["name"] == "bash" and blocks[1]["id"] == "tu_02"
    assert load_session(p) == raw


def test_missing_file_returns_none(tmp_path):
    assert load_session(tmp_path / "nope.json") is None


def test_corrupt_json_renamed_and_none(tmp_path):
    """坏 JSON:返回 None,文件改名 .bad 留证(下次启动不撞同一堵墙)"""
    p = tmp_path / "s.json"
    p.write_text('{"tasks": [broken', encoding="utf-8")
    assert load_session(p) is None
    assert not p.exists() and p.with_name("s.json.bad").exists()


def test_wrong_shape_renamed_and_none(tmp_path):
    """形状不对(不是消息列表):同样当坏文件降级,别让脏数据混进对话"""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"role": "user", "content": "我不是列表"}),
                 encoding="utf-8")
    assert load_session(p) is None
    assert p.with_name("s.json.bad").exists()


def test_save_overwrite_always_latest(tmp_path):
    """全量覆盖写:连存两次,盘上是最后一次的完整状态(不是追加)"""
    p = tmp_path / "s.json"
    save_session([{"role": "user", "content": "第一轮"}], p)
    save_session([{"role": "user", "content": "第一轮"},
                  {"role": "assistant", "content": "好"}], p)
    loaded = load_session(p)
    assert len(loaded) == 2 and loaded[1]["content"] == "好"


if __name__ == "__main__":
    import tempfile
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(tempfile.mkdtemp())
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
