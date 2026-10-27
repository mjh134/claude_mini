#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paths.msys_to_win 的回归测试(含 read_file/write_file/touches_memory 的接入面)。

    python -m pytest test_msys_path.py -q
    python test_msys_path.py          # 不装 pytest 也能跑,自己打印结果

## 为什么这个文件值得存在

msys_to_win 改的是**路径语义**,而路径语义的错法是不对称的:

  · 转宽了(把不该转的转了)→ 相对路径/普通绝对路径被改写,文件落在错误位置;
  · 转窄了(/d/x 没转)→ 回到老 bug,read 报"不存在"、write 静默写错盘。

所以除了转换函数本身的边界用例,这里还有两条端到端断言守着"接入面":
read_file 用 /d/ 风格路径真能读到、touches_memory 用 /d/ 风格路径真能判中
—— 后者是权限一致性:工具和权限必须吃同一种解析,漏接一边就是绕过面。
"""

#直跑自举:把 src/ 加进 import 路径(pytest 模式走 tests/conftest.py,不靠这两行)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "src"))

import os
import sys
import tempfile

from paths import PROJECT_ROOT, msys_to_win


def drive_of(p):
    """取 PROJECT_ROOT 所在盘的字母(测试要可移植,不能写死 d:)"""
    return os.path.splitdrive(str(p))[0][0].lower()


#------------------------------------------------------------
# msys_to_win 的转换边界
CASES_CONVERT = [
    #输入                期望                     为什么
    ("/d/foo/bar.txt",   "d:/foo/bar.txt",       "标准 MSYS 盘符路径"),
    ("/D/foo",           "d:/foo",               "盘符大写也转,统一小写"),
    ("/c/Users/x",       "c:/Users/x",           "任意盘字母"),
    ("/usr/bin",         "/usr/bin",             "两字母以上不是盘符,MSYS 安装目录映射 Python 无从知道"),
    ("/d",               "/d",                   "单段:歧义(可能真有叫 d 的目录),不转"),
    ("/d/",              "/d/",                  "空尾段,不转"),
    ("d:/foo",           "d:/foo",               "已是 Windows 形式"),
    ("C:\\foo\\bar",     "C:\\foo\\bar",         "盘符齐全的原样返回"),
    ("out/file.txt",     "out/file.txt",         "相对路径原样"),
    ("./a/../memory/x",  "./a/../memory/x",      "绕行相对路径原样(abspath 的活,不归这里)"),
    ("//server/share",   "//server/share",       "UNC 不动"),
    ("",                 "",                     "空串无害通过"),
    (None,               None,                   "非字符串原样(防御,调用方校验之前也能安全过)"),
]


def test_convert():
    fails = []
    for raw, want, why in CASES_CONVERT:
        got = msys_to_win(raw)
        mark = "ok" if got == want else "FAIL"
        print(f"  [{mark}] {raw!r:24} -> {got!r:24} ({why})")
        if got != want:
            fails.append((raw, want, got))
    return fails


def test_read_endtoend():
    """read_file 用 /d/ 风格路径真能读到 —— 接入面断言,防未来有人把归一化从 run_read 摘掉"""
    import TOOLS
    d = tempfile.mkdtemp(prefix="msys_read_")
    target = os.path.join(d, "hello.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("MSYS_READ_OK")
    drive = drive_of(d)
    rest = os.path.splitdrive(target)[1].replace("\\", "/")   # /Users/.../hello.txt
    msys_style = f"/{drive}{rest}"
    got = TOOLS.run_read(msys_style)
    ok = got == "MSYS_READ_OK"
    print(f"  [{'ok' if ok else 'FAIL'}] run_read({msys_style!r}) = {got!r}")
    return [] if ok else [("run_read", msys_style, got)]


def test_touches_memory_endtoend():
    """touches_memory 对 /d/ 风格的记忆库路径真能判中 —— 权限一致性断言:
    run_write 接了 msys_to_win,权限这边不接,模型就能用 /d/.../memory/x.md 绕开写保护"""
    import PERMISSIONS
    root = str(PROJECT_ROOT)
    drive = drive_of(root)
    rest = os.path.splitdrive(root)[1].replace("\\", "/")     # /Learn-claude-code/claude_mini
    inside = f"/{drive}{rest}/memory/notes.md"                # /d/Learn-.../memory/notes.md
    outside = f"/{drive}{rest}/out/notes.md"

    fails = []
    #相对路径按"进程 cwd"解析(与 run_write 落盘一致的原语义),测试要在项目根跑才对得上
    old_cwd = os.getcwd()
    os.chdir(PROJECT_ROOT)
    try:
        for path, want, why in [
            (inside, True,  "/d/ 风格的记忆库内路径必须判中(漏判=写保护被绕)"),
            (outside, False, "记忆库外的正常写入不受影响"),
            ("memory/x.md", True, "相对路径写法保持原语义(相对项目根)"),
        ]:
            got = PERMISSIONS.touches_memory(path)
            mark = "ok" if got == want else "FAIL"
            print(f"  [{mark}] touches_memory({path!r}) = {got} ({why})")
            if got != want:
                fails.append((path, want, got))
    finally:
        os.chdir(old_cwd)
    return fails


def main():
    print("msys_to_win 转换边界:")
    fails = test_convert()
    print("run_read 接入面:")
    fails += test_read_endtoend()
    print("touches_memory 权限一致性:")
    fails += test_touches_memory_endtoend()
    print(f"\n{len(fails)} failed" if fails else "\nall pass")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
