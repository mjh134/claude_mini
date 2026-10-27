#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ui._input_with_timeout 的回归测试（管道模式弹窗带表读取）。

    python -m pytest test_ask_timeout.py -q
    python test_ask_timeout.py          # 不装 pytest 也能跑,自己打印结果

## 为什么这个文件值得存在

这个函数改的是**阻塞语义**：修前弹窗的 fallback 是裸 input()，应答被读线程
抢走后主线程永久冻死（e2e 评测 2026-10-26 实测，360s 驱动超时才收场）。
"开着但不喂数据的 stdin + 限时必须返回"就是这个 bug 的最小复刻 ——
case_open_no_input 那条断言挂了 = 冻死回归了。三条 case 分别守三种返回：
answered / timeout / eof，对应 ask_yes_no 收窄后的三种 why。
"""

import subprocess
import sys
import time

CODE = """
import sys, ui
answer, why = ui._input_with_timeout("?(y/n): ", float(sys.argv[1]))
print(f"RESULT:{why}:{answer}", flush=True)
"""


def run_case(name, stdin_mode, timeout_arg, feed=None, wait_cap=30):
    """stdin_mode: 'pipe'(开着不喂/喂 feed) | 'close'(立刻 EOF)"""
    proc = subprocess.Popen(
        [sys.executable, "-c", CODE, str(timeout_arg)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    t0 = time.time()
    if stdin_mode == "pipe":
        if feed is not None:
            time.sleep(0.3)          #先让 input() 挂上,再喂——模拟"人在超时窗口内回答"
            proc.stdin.write(feed)
            proc.stdin.flush()
    else:
        proc.stdin.close()
    #★ 绝不调 communicate() —— 它会顺手关掉 stdin,把 open_no_input 偷偷变成
    #  stdin_closed,被测场景就没了。子进程自己 1.5s 就该打印 RESULT 退出,
    #  这里只 wait;输出量远小于管道缓冲,不会堵
    try:
        proc.wait(timeout=wait_cap)
    except subprocess.TimeoutExpired:
        proc.kill()
        return name, "FROZEN(超过 %ss 没返回 —— 挂死回归)" % wait_cap, time.time() - t0
    dt = time.time() - t0
    out = proc.stdout.read()
    result = [l for l in out.splitlines() if "RESULT:" in l]
    return name, (result[-1].split("RESULT:")[1] if result else "无输出:" + out[-120:]), dt


def main():
    fails = []
    cases = [
        #名字                  stdin  超时参数 喂入       期望(why:answer)
        ("answered",           "pipe",  10,   "y\n",     "fallback:y"),
        ("open_no_input",      "pipe",  1.5,  None,      "timeout:None"),
        ("stdin_closed",       "close", 10,   None,      "eof:None"),
    ]
    for name, mode, tmo, feed, want in cases:
        got, dt = run_case(name, mode, tmo, feed)[1:]
        ok = got == want
        print(f"  [{'ok' if ok else 'FAIL'}] {name:16} -> {got}  ({dt:.1f}s)")
        if not ok:
            fails.append((name, want, got))

    #挂死专项:开着不喂的场景必须在超时参数附近返回,而不是永远不返回
    #(1.5s 的表,允许 5s 上限 —— 超过了就是冻死)
    print(f"\n{len(fails)} failed" if fails else "\nall pass")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
