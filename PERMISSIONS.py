from enum import Enum

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

#统一检查
def check_permission(tool_name: str, arguments: dict):


    if tool_name == "bash":
        command = arguments.get("command", "")
        return check_bash_permission(command)

    return Permission.ALLOW
