

import os
import shutil
import signal
import subprocess
import time


def resolve_bash():
    """定位一个可用的 Git Bash,排除 WSL 启动器(C:\\Windows\\System32\\bash.exe)。

    优先: PATH 上的非 System32 bash;
    其次: 由 PATH 上的 git.exe 推断 Git 安装目录下的 bin/bash.exe(本机即 C:\\Program Files\\Git)。
    返回可执行路径或 None。
    """
    # 1) PATH 上的 bash,但排除 WSL 启动器(没装发行版会报错,且文件系统不同)
    found = shutil.which("bash")
    if found:
        norm = os.path.normcase(os.path.abspath(found))
        if "system32" not in norm:
            return found

    # 2) 由 git.exe 所在位置推断 Git 安装目录
    git = shutil.which("git")
    if git:
        git_root = os.path.dirname(os.path.dirname(os.path.abspath(git)))  # .../Git
        for rel in ("bin/bash.exe", "usr/bin/bash.exe"):
            cand = os.path.join(git_root, *rel.split("/"))
            if os.path.isfile(cand):
                return cand
    return None


def _kill_tree(pid):
    """杀掉一整棵进程树,不只是直接子进程。

    ★ 为什么不能只 proc.kill():Windows 上它终止的只有**直接子进程**(bash.exe),
      bash 再挂出来的孙进程(curl、python fuzz2.py)会活下来变成孤儿。
      实测过一次:2026-09-29 跑分,bench_run 整个进程都死了,它起的
      `python fuzz2.py` 还在后台继续爆破靶机,又跑了 6 分钟才被发现。

      对安全 agent 这不是"进程没清干净"这种清洁问题 —— 你以为停了,它还在打靶机。
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass


#轮询档长:等待子进程时每隔多久醒一次去看取消信号,也是取消响应的上界 ——
#信号置位后最坏再等一个档就杀树。对 300s 的正常超时,这点精度损失可忽略
_POLL_SECONDS = 0.5


#execute_bash 的内部取消信号:_wait 杀完树后抛出,execute_bash 接住转成取消文案。
#不复用 TimeoutExpired:取消和超时是两回事,文案要分得清(模型/用户要看懂为什么停)
class _Cancelled(Exception):
    pass


def _drain(proc, timeout=3):
    """树杀掉以后把管道收干净,免得留下半开的句柄。

    超时只给 3s:实测被杀的子进程遗下的管道句柄要过一会儿才真的关,
    communicate 会一直等到超时。给 10s 就是白等 10s(实测一次超时总耗时 16.7s)
    —— 超时本来就已经是坏事,别在它后面再挂一段没用的等待。
    """
    try:
        proc.communicate(timeout=timeout)
    except Exception:
        pass


def execute_bash(command, timeout=300, stop_event=None):
    """同步执行一条 bash 命令,返回 (output, exit_code),永不抛错。

    stop_event(可选):取消信号(ROADMAP 项 8 取消传播)。给了的话,等待从
    "一次等满 timeout"改成按 _POLL_SECONDS 一档轮询,每档醒来先看信号 ——
    置位就杀整棵树并立即返回取消文案。不给(默认)行为与以前一致:
    eval/bench/前台正常路径零影响。
    """
    bash = resolve_bash()

    if bash is None:
        return (
            "错误: 未找到 Git Bash。"
            "命令工具需要 Git for Windows 提供的 bash.exe"
        ), -1

    #★ 这里从 subprocess.run 换成 Popen,唯一的目的就是**超时的时候还能拿到 pid**
    #  去杀整棵树。subprocess.run 超时时抛的 TimeoutExpired 只带 cmd,不带进程句柄,
    #  拿不到 pid 就没法收拾它挂出来的孙进程。
    proc = None
    try:
        proc = subprocess.Popen(
            [bash, "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            # 关键:不能让 bash 继承本进程的 stdin。
            # 本进程的 stdin 若是管道、又有线程正阻塞在读它(脚本驱动 main.py 时就是这样),
            # bash 会把那个句柄一起继承过去,命令可能挂到 timeout(实测 300s 满额)才回来。
            # 代价是 agent 的 bash 命令读不到交互式输入 —— 本来也没有这种场景。
            stdin=subprocess.DEVNULL,
            creationflags=getattr(
                subprocess,
                "CREATE_NO_WINDOW",
                0
            ),
        )
        stdout, stderr = _wait(proc, timeout, stop_event)
        returncode = proc.returncode
    except _Cancelled as e:
        return str(e), -1
    except subprocess.TimeoutExpired:
        return f"命令执行超时({timeout}s)", -1
    except Exception as e:
        return str(e), -1
    finally:
        #★ Ctrl+C 的落点。KBI 是 BaseException,上面几个 except 都接不住它,
        #但 finally 必跑:进程还活着(被 Ctrl+C 打断的那一刻正是活着)就杀整棵树,
        #然后让 KBI 照常穿出去走 main 的收尾 —— 不吞、不重抛、不干扰。
        #正常跑完/超时杀过/取消杀过的情况下 poll() 已返回退出码,None 才是"还活着",
        #所以这个判断同时保证了"该杀时杀、不该杀时不杀"(正常路径它不会动手)
        if proc is not None and proc.poll() is None:
            _kill_tree(proc.pid)

    # 失败时把 stdout 一并带上:命令常常先有输出再报错,只给 stderr 会丢掉前半截现场
    if returncode == 0:
        output = stdout
    else:
        output = stdout + stderr

    return output, returncode


def _wait(proc, timeout, stop_event):
    """等子进程跑完,返回 (stdout, stderr);超时/取消路径杀树后抛对应异常。

    ★ 为什么轮询而不是 communicate(timeout=timeout) 一次等满:取消信号
      最长只能等一个档,一次等满会把信号晾在整段 timeout 里。
      communicate 短超时抛 TimeoutExpired 不丢已收的输出(Python 3 保证
      重试 communicate 不会丢),所以按档重试是安全的。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return proc.communicate(timeout=_POLL_SECONDS)
        except subprocess.TimeoutExpired:
            if stop_event is not None and stop_event.is_set():
                _kill_tree(proc.pid)
                _drain(proc)
                raise _Cancelled("命令被取消(收到停止信号),没有执行完")
            if time.monotonic() > deadline:
                _kill_tree(proc.pid)
                _drain(proc)
                raise subprocess.TimeoutExpired(proc.args, timeout)
