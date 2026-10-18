

import os
import shutil
import signal
import subprocess


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


def execute_bash(command, timeout=300):
    """同步执行一条 bash 命令,返回 (output, exit_code),永不抛错。"""
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
        stdout, stderr = proc.communicate(timeout=timeout)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        if proc is not None:
            _kill_tree(proc.pid)
            try:
                #树杀掉以后把管道收干净,免得留下半开的句柄。
                #超时只给 3s:实测被杀的子进程遗下的管道句柄要过一会儿才真的关,
                #communicate 会一直等到超时。给 10s 就是白等 10s(实测一次超时总耗时 16.7s)
                #—— 超时本来就已经是坏事,别在它后面再挂一段没用的等待。
                proc.communicate(timeout=3)
            except Exception:
                pass
        return f"命令执行超时({timeout}s)", -1
    except Exception as e:
        if proc is not None:
            _kill_tree(proc.pid)
        return str(e), -1

    # 失败时把 stdout 一并带上:命令常常先有输出再报错,只给 stderr 会丢掉前半截现场
    if returncode == 0:
        output = stdout
    else:
        output = stdout + stderr

    return output, returncode
