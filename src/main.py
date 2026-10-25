import argparse
import queue
import threading
import HOOKS
import ui
import trace
from claude import ClaudeMini
from dispatcher import JobDispatcher
from llm import explain_error
from session_store import save_session, load_session
from task_runner import TaskRunner
import skill


#会话存档的落盘位置。跟随 .task/ 的 cwd 相对纪律(paths.py 注释:它们靠
#“从项目根启动”的纪律保证,不走 PROJECT_ROOT 锡定)
SESSION_PATH = ".task/session.json"


def run_turn(claude_mini, history, session_path=None):
    try:
        claude_mini.run(history)
    except Exception as e:
        ui.error(f"这一轮失败了,已经中断:{explain_error(e)}")
        ui.status("会话还在,可以直接重说一次。")
    finally:
        #写点 = 本轮出 run() 之后(ROADMAP 项 9):只有这里才是 tool_use 与
        #tool_result 成对、压缩完成的点 —— 盘上永远是合法 history,加载侧零修复分支。
        #代价:本轮中途被强杀时不落盘,丢的是正在跑的那一轮,与“只交代不重放”同哲学。
        #失败路径也保存:异常穿出 run() 时 history 仍是上一轮结束的合法状态
        if session_path:
            save_session(history, session_path)



#用户输入循环
#主agent有三条唤醒来源:① 用户敲键盘 ② 团队成员发消息 ③ 后台任务跑完了。
def user_loop(history,claude_mini:ClaudeMini, session_path=None):

    inbox = queue.Queue()

    #读线程:专职读输入,把读到的行塞进队列
    threading.Thread(target=ui.read_input, args=(inbox,), daemon=True).start()

    #Ctrl+C 的落点只有这一个 —— try 必须包住**整个循环体**,不能只包 inbox.get。
    #
    #以前这里的 try 只圈住 inbox.get,于是 Ctrl+C 被劈成两条完全不同的路:
    #  空闲时按(阻塞在 get 上)→ 被接住 → break → 照常走 main() 里的收尾
    #  干活时按(在 run_turn 里)→ run_turn 的 except 只接 Exception,而 KBI 是
    #        BaseException —— 那里**故意**放它穿出去(见 run_turn 的注释),于是它
    #        穿出循环、穿出本函数、一路穿到 __main__,后台任务等待/记忆整理/
    #        MCP 关闭**全部跳过**
    #而用户按 Ctrl+C 的动机几乎永远是后者:有个东西卡住了,想中断它。
    #包住整个循环之后两条路合流,收尾一定会跑到;顺带得到:
    #第一下走收尾(最长等 20 秒),收尾期间再按一下就立刻死 —— 不用额外写代码。
    try:
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

                claude_mini.task_runner.reap_dead()
                if claude_mini.has_team_message() or claude_mini.task_runner.has_completed():#只唤醒agent，消息在run的开头处理
                    run_turn(claude_mini, history, session_path)    #run_turn 里会把 <team_message> 和 <task_notification> 收进 history
                continue

            if message is None or message.lower() in ["q"]:
                ui.status("退出。")
                break

            #斜杠命令(目前只有 /展开、/折叠列表、/折叠、/帮助)。
            if ui.run_command(message):
                continue
            history.append({"role": "user", "content": message})
            #agent循环
            run_turn(claude_mini, history, session_path)
    except KeyboardInterrupt:           #Ctrl+C 落在主线程
        ui.status("退出。")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", action="store_true",
                    help="恢复上次会话(断点续聊),上下文从上次结束处继续")
    args = ap.parse_args()

    #显式初始化观测面(而不是等第一次建 agent 时懒建):
    #启动那一刻就把文件建出来,建不起来当场告警,别等对话到一半才发现没留 trace
    trace.get_trace()

    #初始化agent
    #主对话 runner 带落盘:唯一需要“重启后交代中断任务”的通道(它那份邮箱重启后由
    #新进程的主对话接着读)。派发器/子agent/评测的 runner 都不传 —— 见 TaskRunner 的 opt-in 注释
    claude_mini = ClaudeMini(show_thinking=True, silent=False, agent_name="main",
                             task_runner=TaskRunner(store_path=".task/bg_tasks.json"))

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
    if args.resume:
        #--resume:盘上是 run_turn 收尾点的合法 history,加载即用、零修复分支。
        #无文件/坏文件降级开新会话 —— 恢复面不能反过来把启动卡死
        loaded = load_session(SESSION_PATH)
        if loaded is not None:
            history = loaded
            ui.status(f"已恢复上次会话({len(history)} 条消息),接着聊。")
        else:
            ui.status("没有找到上次会话,开新会话。")
    user_loop(history,claude_mini, SESSION_PATH)

    #退出前先广播取消、再给有界等待(ROADMAP 项 8:先 cancel_all 再等待)。
    #后台任务跑在守护线程上,进程一退就被硬杀 —— 写到一半的文件/记忆可能只落一半。
    #以前 run() 里那道最多等 300s 的闸没了之后,这里就是唯一收尾点;q 和 Ctrl+C
    #两条退出路都汇到它,取消点也因此唯一 —— cancel_all 置进程级停止信号:
    #bash 在轮询档内被杀树,后台 agent 跑完在途请求后由 run() 检查点停住;
    #10 秒内停不下来的(多半是在途 LLM 请求没落地),盘上登记还在,下次启动转中断交代。
    #先报一声再等 —— 否则按 q 之后会静默卡住几秒,看起来像死机
    claude_mini.task_runner.cancel_all()
    dispatcher.runner.cancel_all()
    pending_main = claude_mini.task_runner.running()
    pending_jobs = dispatcher.runner.running()
    if pending_main or pending_jobs:
        ui.status("已发停止信号,等后台任务收尾(每条通道最多 10 秒)…")
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

    #关闭MCP 服务器子进程
    claude_mini.mcp.close()

    #关闭观测面。放在最后——上面 consolidate_if_due 还要调 LLM，那些请求也值得留痕
    trace.get_trace().close()
    