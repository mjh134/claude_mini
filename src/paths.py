"""项目根锚点:所有"数据/资源放哪"的路径统一从 PROJECT_ROOT 解析。

为什么存在:memory.py / skill.py / compact.py / PERMISSIONS.py 原来各自拿
Path(__file__).parent 拼数据目录,代码一搬家(进 src/),memory/、SKILLS/ 就必须
跟着搬,否则静默失联。收敛到一个常量后,代码归 src/,数据归项目根,两边搬家互不牵连。

覆盖范围只限 __file__ 锚定的那类路径。.task/、llm_request.json、ui_ask.log 这些
**cwd 相对路径**不在此列 —— 它们跟进程工作目录走,靠"从项目根启动"的纪律保证。
"""
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#Git Bash 风格盘符路径:/单字母/还有后续(如 /d/foo/bar)。只匹配正斜杠 ——
#反斜杠的 \d\foo 是合法的 Windows"当前盘根下 d 目录"语义,不是 MSYS 约定,不能转
_MSYS_DRIVE = re.compile(r"^/([A-Za-z])/(.+)")

def msys_to_win(path):
    """Git Bash 风格盘符路径(/d/foo/bar)转 Windows 形式(d:/foo/bar),其余原样返回。

    为什么存在:bash 工具的命令整个交给 Git Bash 执行,MSYS runtime 自己会把
    /d/x 映射成 D:\\x;而 read_file/write_file 是 Python 直接 open(),不经过 bash ——
    Windows 对 /d/x 的解析是"当前盘根目录下的 d 目录",读=永远"文件不存在",
    写=静默落到错误的盘。模型的路径习惯是从 bash 输出里学来的(全是 /d/... 风格),
    拿去调文件工具就撞上这套不一致(实测:e2e 评测里 read_file 连报"文件不存在",
    agent 每次都得降级 bash cat,白耗一轮)。

    只转"/单字母段/还有后续"这一种形态:
      /d/foo   → d:/foo   (MSYS 盘符约定)
      /d、/d/  不转        (歧义:也可能真有叫 d 的目录;单段或空尾段几乎不会传进文件工具)
      /usr/bin 不转        (两个以上字母不是盘符;MSYS 的安装目录映射 Python 无从知道)
    相对路径、盘符齐全、UNC(server/share)都不匹配,原样返回。

    ★ 权限一致性:PERMISSIONS.touches_memory 和 run_write 必须用同一个函数解析
      (见 touches_memory 的"解析方式必须和 run_write 落盘一致"不变量)——
      只改工具不改权限,模型就能用 /d/.../memory/x.md 绕开记忆写保护。
    """
    if not isinstance(path, str):
        return path
    m = _MSYS_DRIVE.match(path)
    if m:
        return m.group(1).lower() + ":/" + m.group(2)
    return path
