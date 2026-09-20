import threading
import PERMISSIONS
import ui

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
        #把主线程的输入卡死(表现出来就是整个终端没反应);后台任务那条线程更没人可问 ——
        #用户可能早就走了,挂在这里等一个不在场的人确认,只会把任务永远吊住
        if threading.current_thread() is not threading.main_thread():
            return ("❌ 该命令需要人工确认,但当前不在主线程(后台任务/团队成员线程),"
                    "无法弹窗询问,已阻止执行。"
                    f"命令:{shown}")
        #问人这一步**不能**直接 input()。改造之后终端的所有权在读线程手上
        #(prompt_toolkit),主线程再开一个 input() 就是两边抢同一个控制台 ——
        #提示是打出来了,但按键会被读线程吃掉,表现成"按什么都没反应"。
        #ui.ask_yes_no 负责把终端借过来,借不到就返回 False:
        #读不到用户输入(EOFError、stdin 关了、管道跑、cron 唤醒时没终端)是**正常**情况,
        #不是异常 —— 没有人在场就当作"没批准"。绝不能让异常穿出去:
        #这个 prompt 在 _call_tool 的 try 之外,穿出去就是把整个 agent 带崩
        approved, why = ui.ask_yes_no(
            f"⚠️ The command '{shown}' may be dangerous. Do you want to proceed? (y/n): ")

        if approved:
            return None
        if why == "answered":
            #真问到了,用户明确说了不(y 之外的任何回答都算不,回车也算)
            return "❌ Command execution canceled by the user,权限不允许，不要再尝试执行重复命令"
        if why == "timeout":
            return ("❌ 需要人工确认,但等不到用户回答(询问超时),已按拒绝处理。"
                    f"命令:{shown}")
        #'eof' / 'no-app' —— 压根没问到人
        return ("❌ 需要人工确认,但当前没有可用的交互终端(读不到用户输入),已按拒绝处理。"
                f"命令:{shown}")
    else:
        return None

def before_agent_hook(prompt):
    #子agent的输出夹在这两行之间 —— 它们同时是"开始了"和"到哪结束"的界桩。
    #带一句任务摘要:子agent经常跑很久,回头翻屏幕得知道这段是谁在干什么
    #
    #这两行也登记进折叠处:屏幕上压成一句,但**派给子agent的原话**和**它交回来的
    #全文**正是事后最想回看的东西 —— 子agent没有别的输出通道(它的正文不显示),
    #不登记就真的只剩这一句摘要了
    ui.status("🤖 子agent 开始:" + ui.one_line(prompt),
              fold=("子agent", "开始:" + ui.one_line(prompt), prompt))


def after_agent_hook(result):
    ui.status("✓ 子agent 结束:" + ui.one_line(result),
              fold=("子agent", "结束:" + ui.one_line(result), result))