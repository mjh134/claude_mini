import queue
import threading
import HOOKS
import ui
from claude import ClaudeMini
from dispatcher import JobDispatcher
from llm import explain_error
import skill


#把一轮对话跑起来,**并且保证它失败时不会把整个会话带走**。
#
#为什么需要它(实测的洞):llm.py 那边给请求加了超时之后,一次"网络卡住"会变成
#抛异常,而 user_loop 的两个调用点**都没有兜底** —— 异常一路穿到 __main__,
#进程直接带 traceback 死掉,还会跳过下面的收尾(后台任务被硬杀、MCP 子进程只剩 atexit 兜底)。
#加超时之前这条路是"卡 30 分钟",加完是"当场死",所以这个兜底是**和超时配套的**,
#不是顺手加的保险。
#
#为什么放在这里、而不是塞进 run() 里一次搞定:run() 的调用方有五种,每种该有不同反应 ——
#前台子agent 的失败要变成一条工具错误回给父agent(claude.py 已经这么做),
#后台子agent 要变成一条"已失败"的通知(task_runner 已经这么做),而**主对话**要的是
#"报个错、然后接着聊"。在 run() 里统一吞掉,前台子agent 的失败就会变成一段普通文本,
#父agent 会把它当成子agent 的结论 —— 那是比崩溃更坏的事。所以只补唯一没兜底的那条路
#
#**只接 Exception,不接 BaseException**:KeyboardInterrupt 必须原样穿出去。
#最坏仍然要等 6 分钟,按 Ctrl+C 是用户主要的逃生手段,接宽了会变成"按了没反应"
def run_turn(claude_mini, history):
    try:
        claude_mini.run(history)
    except Exception as e:
        #失败时 history 的末尾必然是**一条 user 消息**:assistant 那段是在 llm.send()
        #**返回之后**才 append 的(claude.py),所以不会留下没有 tool_result 的 tool_use ——
        #下一次请求的历史是自洽的,直接重说一次就行,不需要修补历史
        ui.error(f"这一轮失败了,已经中断:{explain_error(e)}")
        ui.status("会话还在,可以直接重说一次。")



#用户输入循环
#主agent有三条唤醒来源:① 用户敲键盘 ② 团队成员发消息 ③ 后台任务跑完了。
#但 input() 会把主线程整个卡住,卡住期间谁也看不到消息总线和结果邮箱 ——
#所以把 input() 挪到一条读线程里,主循环改成"带超时地从队列取",
#超时的那一下就用来瞄一眼总线和邮箱:有东西就立刻把 agent 叫起来。
#
#①②③ 全挤在这**一个**地方,而且永远在主线程上 —— 这是刻意的:
#permission_hook 靠"是不是主线程"决定能不能弹窗问用户(HOOKS.py),
#唤醒者只要多出一个、跑在别的线程上,那条路径上的工具调用就会静默失去问人的能力。
#定时任务曾经就是第四个唤醒者(AgentRunner 在别的线程上调 run() 叫主agent去执行任务),
#它已经被移出这个循环:任务现在跑在自己的实例里,由 dispatcher.JobDispatcher 派发,
#主agent和它再无交集 —— 能不进这个循环的唤醒源,就不要进来
def user_loop(history,claude_mini:ClaudeMini):

    inbox = queue.Queue()

    #读线程:专职读输入(它阻塞没关系,主循环不等它),把读到的行塞进队列
    #
    #主体在 ui.read_input —— 它和 ui.ask_sync 是一对(一个拿走终端、一个借回来,
    #共用 PT_ACTIVE),必须放在一起改。这里只负责起线程。
    #
    #效果:主循环这条线程打东西时,patch_stdout 会先把提示符擦掉、打完再画回来,
    #不再是"输出从提示符身上碾过去"。代价是**终端的所有权归读线程** ——
    #主线程要反过来问用户(权限询问)就得走 ui.ask_sync 借,坑见那边的注释。
    threading.Thread(target=ui.read_input, args=(inbox,), daemon=True).start()

    while True:
        try:
            message = inbox.get(timeout=1)  #1秒没输入就超时,去处理团队消息
        except queue.Empty:
            #先处理生命周期控制消息(成员的下线确认等,纯代码、不叫模型),
            #再决定要不要把 agent 叫起来:有团队消息,或者后台任务跑完了
            #
            #③后台结果走这里:后台任务不再阻塞发起它的那一轮(claude.py 的 run()),
            #结果就靠这一秒一次的超时槽发现,再由 run() 开头的 collect() 领走。
            #两个条件必须写在**同一个 if** 里 —— 分开写会连着叫起两轮,第二轮无事可做
            claude_mini.process_control_messages()
            #看门狗:后台 agent 的线程要是没了(异常穿出去、被 SystemExit 带走),
            #它的结果永远不会自己出现 —— 这里把那种任务补成一条"已失败"塞进邮箱。
            #它**不是**第四个唤醒来源:补完的结果走的还是下面这个现成的条件,
            #所以这一行不加判断、只做事,if 里一个字都不用动
            claude_mini.task_runner.reap_dead()
            if claude_mini.has_team_message() or claude_mini.task_runner.has_completed():
                run_turn(claude_mini, history)    #run_turn 里会把 <team_message> 和 <task_notification> 收进 history
            continue
        except KeyboardInterrupt:           #Ctrl+C 落在主线程
            ui.status("退出。")
            break

        if message is None or message.lower() in ["q"]:
            ui.status("退出。")
            break
        #斜杠命令(目前只有 /展开、/折叠列表、/折叠、/帮助)。
        #★ **只拦 ui 认领的那几条**,认不出就往下走、原样发给模型 ——
        #这个项目里 /ask 这类本来就是普通文本,不能因为加了个命令层就把它吞掉。
        #命令不进 history:它是"看"的动作,不是对模型说的话,
        #进历史只会让模型下一轮莫名其妙地收到一句 /展开
        if ui.run_command(message):
            continue
        history.append({"role": "user", "content": message})
        #agent循环
        run_turn(claude_mini, history)

if __name__ == "__main__":

    #初始化agent。消息总线、任务库、调度器、MCP 都由构造函数自己备好,
    #调用方不需要知道有这些东西(要用同一个实例时才显式传,比如 team_spawn 给成员注入总线和成员表)
    claude_mini = ClaudeMini(show_thinking=True, agent_name="main")

    #加载狗子
    HOOKS.register_hook("PreToolUse", HOOKS.permission_hook)
    HOOKS.register_hook("BefSubAgent",HOOKS.before_agent_hook)
    HOOKS.register_hook("AftSubAgent",HOOKS.after_agent_hook)

    #定时任务:扫描线程由 ClaudeMini 启动(只负责把到点的任务入队,每分钟一次),
    #JobDispatcher 负责把队列里的任务派进独立的后台 agent 执行(用户不说话也能跑)。
    #执行过程完全不经过主agent:任务有自己的实例、自己的线程、自己的历史,
    #跑完由派发器把结果汇报回 Scheduler —— 所以下面这三条唤醒来源里没有"定时任务"这一条
    dispatcher = JobDispatcher(claude_mini, claude_mini.scheduler)
    dispatcher.start()

    ui.banner([("Claude Mini", "bold"), ("   输入 q 退出", "dim")])

    history = []
    user_loop(history,claude_mini)

    #退出前给后台任务留一个有界等待。后台任务跑在守护线程上,进程一退就被硬杀 ——
    #写到一半的文件/记忆可能只落了一半。以前 run() 里那道最多等 300s 的闸就是顺手挡这个的,
    #现在改成真异步,那道闸没了,所以必须在这里补一道,否则用户随时能按 q 把正在写盘的任务斩断。
    #两条通道各等各的:定时任务是派发器名下那份执行器,不在主对话那份里。
    #先报一声再等 —— 否则按 q 之后会静默卡住几秒,看起来像死机
    pending_main = claude_mini.task_runner.running()
    pending_jobs = dispatcher.runner.running()
    if pending_main or pending_jobs:
        ui.status("还有后台任务在跑,等它们收尾(每条通道最多 10 秒)…")
        left_main = claude_mini.task_runner.wait_running(timeout=10)
        left_jobs = dispatcher.runner.wait_running(timeout=10)
        left = left_main + left_jobs
        if left:
            ui.warn(f"还有 {len(left)} 个后台任务没跑完({'、'.join(left)}),退出会中断它们")
        #被打断的定时任务要留个交代:盘上会停在 running,下次启动时 _load 的僵尸回收
        #把它放回待执行(崩掉的这一次不补跑)。说一声,免得用户以为任务就此丢了
        if left_jobs:
            ui.status(f"其中 {len(left_jobs)} 个是定时任务,会在下次启动时放回待执行")

    # 整个会话结束:经验记忆达到阈值时,交给模型做一次去重整理(LLM 驱动;失败不阻断退出)
    claude_mini.memory_manager.consolidate_if_due(claude_mini.llm)

    # 收掉 MCP 服务器子进程。Python 退出不会顺手杀子进程,不收它们会挂在后台等 stdin
    #(MCPManager 里还挂了 atexit 兜底,这里显式收一次是为了"正常退出就干净",不指望兜底)
    claude_mini.mcp.close()
    