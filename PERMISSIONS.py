import os
from enum import Enum
from pathlib import Path

class Permission(Enum):
    ALLOW = "allowed"
    ASK = "ask"
    DENY = "deny"


#明确拒绝的命令
DENY_COMMANDS = [
        "rm -rf /",
        "mkfs",
        "dd if=",
        "> /dev/sda",
        "shutdown",
        "reboot",
    ]    
 #需要二次询问的命令
ASK_COMMANDS = [
    "rm",
    "rmdir",
    "del",
    "format",
    "move",
    "mv",
    "cp",
    "rename",
    "ren"
]
   
#检查bash命令
def check_bash_permission(command: str):
    command_lower = command.lower().strip()

    for denied in DENY_COMMANDS:                 # DENY 保持子串匹配(如 "rm -rf /")
        if denied.lower() in command_lower:
            return Permission.DENY

    # ASK 改为按"命令词"精确匹配,不再子串误伤:
    #   "echo renaming log" / "echo format complete" → 现在 ALLOW
    #   "ren old.txt new.txt" / "format d:"        → 仍 ASK
    tokens = set(command_lower.split())
    for dangerous in ASK_COMMANDS:
        if dangerous in tokens:
            return Permission.ASK

    return Permission.ALLOW

#长期记忆库的根目录。算法必须和 memory.py 一致:
#   memory.py:15  BASE_DIR = Path(__file__).resolve().parent
#   memory.py:19  memory_dir = os.getenv("MEMORY_DIR", "memory")
#两个文件在同一个目录,所以这里的 parent 就是它的 BASE_DIR。
#**每次现算,不在模块加载时算**:.env 是在 MemoryManager.__init__ 里 load_dotenv() 的
#(memory.py:18),而本模块被 claude.py 的 `import HOOKS` 更早地拉起来 —— 模块级算的话,
#那时 os.getenv("MEMORY_DIR") 还是 None,一律退化成 "memory"。今天 MEMORY_DIR 恰好
#就是 "memory",所以错了也不报错;等谁哪天改了它,就会变成"规则护着 A、memory.py 写的是 B"。
def _memory_root():
    return Path(__file__).resolve().parent / os.getenv("MEMORY_DIR", "memory")


def touches_memory(path):
    """这个写入目标是不是落在长期记忆库里。

    解析方式必须和 run_write 真正落盘的方式一致:run_write 用的是 Path(path),
    相对路径 ⇒ 相对于**进程 cwd**。所以这里也走 abspath,而不是拿 _memory_root() 去拼字符串 ——
    这样 "memory/x.md" 和 "D:/…/memory/x.md" 两种写法会归到同一个绝对路径,
    "./a/../memory/x.md" 这种绕法也会在 abspath 里被规范化掉。
    normcase:Windows 路径不分大小写,"MEMORY/" 和 "memory/" 是同一个目录。
    比前缀时带上分隔符,免得 "…/memory_backup/" 被误判成 "…/memory/" 的子目录。
    解析失败按"碰了"处理(交给调用方 ASK):权限检查自己出错时该往严的方向倒,不能放行。
    """
    try:
        target = os.path.normcase(os.path.abspath(path))
        root = os.path.normcase(os.path.abspath(_memory_root()))
    except Exception:
        return True
    return target == root or target.startswith(root + os.sep)

#统一检查
def check_permission(tool_name: str, arguments: dict):
    """工具调用级判定:这一次调用准不准。返回 ALLOW / ASK / DENY。

    规则放哪一层,按这个分:
      - 硬性不变量(如 .env 永不覆盖)写在工具自己的 handler 里 —— 谁都绕不过;
      - 需要人来判断的("这看着不对,你确定吗")写在这里,交给 permission_hook:
        主线程会弹窗问用户,成员线程弹不了窗就直接拦下(见 HOOKS.py 的 permission_hook)。
    """

    if tool_name == "bash":
        command = arguments.get("command", "")
        return check_bash_permission(command)

    #write_file 是成员绕过 write_memory 的现成通道:成员手里就有 write_file,
    #直接往 memory/ 写文件就能改长期记忆,一个技巧都不用。
    #这里管的是"路径落在记忆库里",不是"这个工具危险" —— 写源码、报告、临时文件一概 ALLOW,
    #不打扰。合法改记忆的路径是 write_memory(它自己调 memory.py,不走 write_file 工具),
    #所以这条规则不会碍着正事。
    if tool_name == "write_file" and touches_memory(arguments.get("path", "")):
        return Permission.ASK

    return Permission.ALLOW
