import sys
import queue
import threading
import HOOKS
from claude import ClaudeMini
from agent_runner import AgentRunner
import skill




#用户输入循环
#主agent有两条唤醒来源:① 用户敲键盘 ② 团队成员发消息。
#但 input() 会把主线程整个卡住,卡住期间谁也看不到消息总线 ——
#所以把 input() 挪到一条读线程里,主循环改成"带超时地从队列取",
#超时的那一下就用来瞄一眼总线:有成员消息就把 agent 叫起来处理。
def user_loop(history,claude_mini:ClaudeMini):

    inbox = queue.Queue()

    #读线程:专职阻塞在 input() 上(它阻塞没关系,主循环不等它),把读到的行塞进队列
    def read_input():
        while True:
            try:
                inbox.put(input("User: "))
            except (EOFError, KeyboardInterrupt):
                inbox.put(None)     #None 当作退出信号
                return

    threading.Thread(target=read_input, daemon=True).start()

    while True:
        try:
            message = inbox.get(timeout=1)  #1秒没输入就超时,去处理团队消息
        except queue.Empty:
            #先处理生命周期控制消息(成员的下线确认等,纯代码、不叫模型),
            #再决定要不要把 agent 叫起来处理普通团队消息
            claude_mini.process_control_messages()
            if claude_mini.has_team_message():
                claude_mini.run(history)    #run() 开头会把 <team_message> 收进 history
            continue
        except KeyboardInterrupt:           #Ctrl+C 落在主线程
            print("\nExiting the assistant. Goodbye!")
            break

        if message is None or message.lower() in ["q"]:
            print("Exiting the assistant. Goodbye!")
            break
        history.append({"role": "user", "content": message})
        #agent循环
        claude_mini.run(history)

if __name__ == "__main__":

    #初始化agent。消息总线、任务库、调度器、MCP 都由构造函数自己备好,
    #调用方不需要知道有这些东西(要用同一个实例时才显式传,比如 team_spawn 给成员注入总线和成员表)
    claude_mini = ClaudeMini(show_thinking=True, agent_name="main")

    #加载狗子
    HOOKS.register_hook("PreToolUse", HOOKS.permission_hook)
    HOOKS.register_hook("BefSubAgent",HOOKS.before_agent_hook)
    HOOKS.register_hook("AftSubAgent",HOOKS.after_agent_hook)

    #定时任务:扫描线程由 ClaudeMini 启动(只负责入队,每分钟一次),
    #AgentRunner 负责唤醒 agent 去执行队列里的任务(用户不说话也能跑)
    agent_runner = AgentRunner(claude_mini, claude_mini.scheduler)
    agent_runner.start()

    print("Welcome to the Claude Mini Assistant!")

    history = []
    user_loop(history,claude_mini)

    # 整个会话结束:经验记忆达到阈值时,交给模型做一次去重整理(LLM 驱动;失败不阻断退出)
    claude_mini.memory_manager.consolidate_if_due(claude_mini.llm)

    # 收掉 MCP 服务器子进程。Python 退出不会顺手杀子进程,不收它们会挂在后台等 stdin
    #(MCPManager 里还挂了 atexit 兜底,这里显式收一次是为了"正常退出就干净",不指望兜底)
    claude_mini.mcp.close()
    