import threading
import PERMISSIONS

HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
    "BefSubAgent":[],
    "AftSubAgent":[],
}

def register_hook(event, func):
    if event in HOOKS:
        HOOKS[event].append(func)
    else:
        raise ValueError(f"Unknown event: {event}")
    
def trigger_hooks(event, *args, **kwargs):
    if event in HOOKS:
        for func in HOOKS[event]:
            result = func(*args, **kwargs)
            if result is not None:
                return result
        return None
    else:
        raise ValueError(f"Unknown event: {event}")
            
def permission_hook(block):
    permission = PERMISSIONS.check_permission(block.name, block.input)
    if permission == PERMISSIONS.Permission.DENY:
        return "❌ Permission denied for the command,不要再尝试执行重复命令"
    elif permission == PERMISSIONS.Permission.ASK:
        #打"人类看得懂的那个值",不要打整个 input 字典(否则提示长成 {'command': 'rm -f ...'})。
        #非 bash 的工具也可能走到这(比如 write_file 写 memory/),它们没有 command 键,
        #所以退而取 path —— 再退才是整个字典。下面三条分支共用它,别各拼各的。
        shown = block.input
        if isinstance(block.input, dict):
            for key in ("command", "path"):
                if key in block.input:
                    shown = block.input[key]
                    break
        #只有主线程能弹交互式询问:团队成员在自己的线程里调 input() 会抢走 stdin,
        #把主线程的输入卡死(表现出来就是整个终端没反应)—— 这里直接拦下
        if threading.current_thread() is not threading.main_thread():
            return ("❌ 该命令需要人工确认,而当前是团队成员线程、无法弹窗询问,已阻止执行。"
                    f"命令:{shown}")
        #input() 会抛 EOFError(stdin 关了:管道跑、非交互、cron 唤醒时没有终端)。
        #它是**正常**情况,不是异常 —— 没有人在场就当作"没批准",绝不能让它穿出去:
        #这个 prompt 在 _call_tool 的 try 之外,穿出去就是把整个 agent 带崩。
        try:
            user_input = input(f"⚠️ The command '{shown}' may be dangerous. Do you want to proceed? (y/n): ")
        except (EOFError, KeyboardInterrupt):
            return ("❌ 需要人工确认,但当前没有可用的交互终端(读不到用户输入),已按拒绝处理。"
                    f"命令:{shown}")
        if user_input.lower() == 'y':
            return None
        else:
            return "❌ Command execution canceled by the user,权限不允许，不要再尝试执行重复命令"
    else:
        return None

def before_agent_hook(prompt):
    print()
    print("┌─ 🤖 SubAgent ─────────────────────────")
    print("│ 🚀 开始执行子任务")


def after_agent_hook(result):
    print("│◼ SubAgent 执行结束")
    print("└──────────────────────────────────────")