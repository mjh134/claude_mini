

import os
import shutil
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


def execute_bash(command, timeout=300):
    """同步执行一条 bash 命令,返回 (output, exit_code),永不抛错。"""
    bash = resolve_bash()

    if bash is None:
        return (
            "错误: 未找到 Git Bash。"
            "命令工具需要 Git for Windows 提供的 bash.exe"
        ), -1

    try:
        result = subprocess.run(
            [bash, "-c", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
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
    except subprocess.TimeoutExpired:
        return f"命令执行超时({timeout}s)", -1
    except Exception as e:
        return str(e), -1

    # 失败时把 stdout 一并带上:命令常常先有输出再报错,只给 stderr 会丢掉前半截现场
    if result.returncode == 0:
        output = result.stdout
    else:
        output = result.stdout + result.stderr

    return output, result.returncode
