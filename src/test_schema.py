#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""schema.check_schema 的回归测试:格式校验比被测代码更确定,全部用固定字典喂,不启 agent、不烧 LLM。
    python -m pytest test_schema.py -q
    python test_schema.py            # 不装 pytest 也能跑,自己打印结果
断言只认 errors/warnings 的有无和条数,不锁错误消息措辞。"""

import sys

from schema import check_schema, attach_warnings


BASH = {"properties": {"command": {"type": "string"},
                       "is_background": {"type": "boolean"}},
        "required": ["command"]}
STATUS = {"properties": {"status": {"type": "string",
                                   "enum": ["pending", "in_progress", "completed"]}}}
DEPS = {"properties": {"depends_on": {"type": "array", "items": {"type": "string"}}},
        "required": ["depends_on"]}


def test_valid_call_passes():
    errors, warnings = check_schema(BASH, {"command": "ls"})
    assert errors == [] and warnings == []


def test_missing_required_is_error():
    errors, _ = check_schema(BASH, {})
    assert len(errors) == 1


def test_unknown_key_is_warning_not_error():
    errors, warnings = check_schema(BASH, {"command": "ls", "timeout": 5})
    assert errors == [] and len(warnings) == 1


def test_numeric_string_for_integer_is_error():
    errors, _ = check_schema({"properties": {"limit": {"type": "integer"}}}, {"limit": "20"})
    assert len(errors) == 1


def test_boolean_not_accepted_as_integer():
    # isinstance(True, int) 为真:不排除 bool,True 会蒙过 integer
    errors, _ = check_schema({"properties": {"n": {"type": "integer"}}}, {"n": True})
    assert len(errors) == 1


def test_string_true_rejected_as_boolean():
    # "true" 放过就会被 .get("is_background") 当真值走后台分支,是最隐蔽的静默错
    errors, _ = check_schema(BASH, {"command": "ls", "is_background": "true"})
    assert len(errors) == 1


def test_enum_out_of_range_is_error():
    errors, _ = check_schema(STATUS, {"status": "im_done"})
    assert len(errors) == 1


def test_enum_in_range_passes():
    errors, warnings = check_schema(STATUS, {"status": "pending"})
    assert errors == [] and warnings == []


def test_array_item_type_checked():
    errors, _ = check_schema(DEPS, {"depends_on": ["task_1", 2]})
    assert len(errors) == 1
    errors, _ = check_schema(DEPS, {"depends_on": ["task_1", "task_2"]})
    assert errors == []


def test_non_dict_arguments_is_error():
    errors, _ = check_schema(BASH, "ls")
    assert len(errors) == 1


def test_none_for_required_string_is_error():
    errors, _ = check_schema(BASH, {"command": None})
    assert len(errors) == 1


def test_empty_schema_degrades_to_warnings():
    errors, warnings = check_schema({}, {"whatever": 1})
    assert errors == [] and len(warnings) == 1


def test_attach_warnings_preserves_type_when_clean():
    result = {"k": "v"}
    assert attach_warnings(result, []) is result          # 无警告:原样返回,不改类型
    out = attach_warnings("done", ["未声明的参数:foo(已忽略)"])
    assert isinstance(out, str) and "foo" in out          # 有警告:键名要出现在回给模型的话里


#============================================================
# 不装 pytest 也能直接跑
#============================================================

def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}\n{e}")
    print()
    print(f"{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
