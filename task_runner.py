import threading
import time
from bash_exec import execute_bash

#后台 agent 的并发上限。
#bash 任务便宜(一条子进程),agent 任务**烧 token** —— 每个后台子agent 都是一整个 LLM 循环。
#没有这道闸,模型一句话就能拉起 N 个,账单敞着。它在 submit_agent 里卡,不卡 bash
MAX_BACKGROUND_AGENTS = 3

#"线程没了、结果却没留下"时补进邮箱的那条说明(reap_dead 用它)。
#刻意写成"诊断结论"的口气:模型看到它要知道**该重新提交**,而不是以为任务还在跑
_THREAD_DIED_OUTPUT = (
    "这条后台任务的线程在写出结果之前就终止了,没有留下输出。"
    "它没做完的活没有人接手(线程级异常不会走到这里来),需要的话请重新提交一次。"
)


#任务记录的**唯一**形状定义。两处共用是因为 submit_agent 必须把"查名额"和"登记"
#放在同一把锁里,没法复用 _new_task(那个自己拿锁,而 Lock 不可重入)—— 各写一份 dict
#迟早会漂移,而缺字段是看门狗那边先炸(它读 thread)
#
#started_at 用单调时钟(不是 wall clock):它只用来算"已经跑了多久",
#而 wall clock 会被系统对时/夏令时往回拨,算出来一个负数或突然跳几小时。
#记录时刻就是建记录的时刻(提交那一刻),不存在"起跑才开始计时"的问题
def _record(kind, on_done=None):
    return {"kind": kind, "status": "running", "thread": None,
            "started_at": time.monotonic(), "on_done": on_done}


#后台执行器:进程内统一承接"不在当前线程跑"的工作。
#
#职责边界(这个类存在的全部理由):
#- 只管**怎么执行** —— 开一条线程、记状态、收结果、等人来领。
#- 不管**执行什么** —— 执行体有几种,调用方按需选入口(submit_bash / submit_agent)。
#- **不认识 Job** —— 定时任务的状态汇报由 Scheduler 自己接回去,不从这里走。
#  cron 概念一旦渗进来,这个类就没法给别的调用方用了。
#
#它替换掉了原来的 BackgroundManager:那次抽象把"是不是后台"和"是什么任务"焊死了
#(is_background 只长在 bash 的 schema 上、执行分支写死 block.name == "bash"、
# 执行体写死 execute_bash),想给别的任务加后台能力无处可加。
class TaskRunner:

    #id_prefix:task_id 的前缀。进程里可能有不止一个 runner(主对话一个、
    #定时任务派发器一个),各自从 1 开始计数,不加区分就会出现两个 bg_0001 ——
    #通知、banner、日志全对不上号。默认 "bg",派发器用 "bg_job"
    def __init__(self, id_prefix="bg"):
        self.id_prefix = id_prefix
        self.tasks = {}         # task_id -> _record() 那份形状(含 thread / on_done)
        self.ready = []         # 已完成、等待领取的 task_id(邮箱)
        self.results = {}       # task_id -> {"kind", "status", "output"}
        self.counter = 0
        self.agents_running = 0 #当前在跑的后台 agent 数,对着 MAX_BACKGROUND_AGENTS 卡
        self.lock = threading.Lock()

    #登记一条新任务,占一个 task_id
    #kind 只用于区分执行体(日志和排查要靠它),不参与任何分支判断
    def _new_task(self, kind):
        with self.lock:
            self.counter += 1
            task_id = f"{self.id_prefix}_{self.counter:04d}"
            self.tasks[task_id] = _record(kind)
        return task_id

    #把线程句柄挂到任务上。**必须在 start() 之后挂**:start 之前 is_alive() 是 False,
    #万一那一刻被 reap_dead 看到,一条还没起跑的任务就会被当成死了的。
    #(start 到 attach 之间线程可能已经跑完、甚至被 collect 领走 —— 所以用 get 容忍缺席)
    def _attach_thread(self, task_id, thread):
        with self.lock:
            task = self.tasks.get(task_id)
            if task is not None:
                task["thread"] = thread

    #提交一条后台 bash 命令,立即返回 task_id
    #
    #权限检查(PreToolUse)**不在这里做**,是在 excute_tool 的调用线程里、进到这条分支之前就做完了。
    #别"顺手补上"一个 trigger_hooks:permission_hook 靠"是不是主线程"决定能不能弹窗问用户
    #(HOOKS.py:45),而这里开的是另一条线程 —— 在这儿过一遍,主agent自己的正常命令也会被当成
    #"后台线程弹不了窗"直接拒掉。结论:权限判定属于**发起调用的线程**。
    #
    #这里收的是 command 字符串,不是工具调用块:执行器不该知道"工具"长什么样,
    #否则将来同一个执行体换个工具入口(比如 job 跑一条命令)就得再造一个入口。
    def submit_bash(self, command):
        task_id = self._new_task("bash")
        thread = threading.Thread(target=self._run_bash, args=(task_id, command), daemon=True)
        thread.start()
        self._attach_thread(task_id, thread)
        return task_id

    #执行体:bash
    def _run_bash(self, task_id, command):
        try:
            output, exit_code = execute_bash(command)
            status = "completed" if exit_code == 0 else "failed"
        except BaseException as e:      #BaseException 是故意的,理由见 _run_agent
            output = f"{type(e).__name__}: {e}"
            status = "failed"
        self._finalize(task_id, status, output)

    #提交一个后台 agent,立即返回 task_id;**已达并发上限则返回 None**
    #
    #work 是个"收 task_id、返回结果文本"的可调用对象,在本类开的线程里执行。
    #本类不负责建 agent、不认识 ClaudeMini —— 一旦认识,它就没法给别的调用方用了。
    #task_id 传进去是给调用方当标识用的(比如拿它当 agent_name,banner 和日志里能对上号)
    #
    #on_done(可选)是"跑完之后通知谁"的回调,收 (ok: bool, output: str)。
    #它让调用方能拿回执行结果,而本类仍然**不认识调用方是什么** ——
    #定时任务靠它把结果汇报给 Scheduler,本类全程不知道"job"这个词。
    #回调里抛异常不会影响收尾(结果已经在邮箱里了),只打一行日志
    def submit_agent(self, work, on_done=None):
        #查名额和登记必须在**同一把锁里** —— 分成两步,两个调用方就会同时看到"还有名额",
        #一起把 agents_running 顶过上限
        with self.lock:
            if self.agents_running >= MAX_BACKGROUND_AGENTS:
                return None     #满了。调用方必须把这件事告诉模型/放回任务,不能静默丢弃
            self.counter += 1
            task_id = f"{self.id_prefix}_{self.counter:04d}"
            #on_done 记进任务里,不塞进线程参数:看门狗要能**替**一条没来得及收尾的任务
            #去通知调用方 —— 回调只捏在 _run_agent 手里的话,线程一没,那条通知就永远发不出去
            self.tasks[task_id] = _record("agent", on_done)
            self.agents_running += 1
        thread = threading.Thread(target=self._run_agent, args=(task_id, work), daemon=True)
        thread.start()
        self._attach_thread(task_id, thread)
        return task_id

    #执行体:agent
    def _run_agent(self, task_id, work):
        try:
            output = work(task_id)
            status = "completed"
        #**BaseException 是故意的,不是手滑**:只接 Exception 的话,SystemExit 这类
        #BaseException 会直接穿出去把线程带走,而线程的默认异常钩子只往 stderr 打一行 ——
        #结果就是任务永远停在 running,没有人知道它已经没了(实测:主agent 会一直空等)。
        #后台线程的异常没有别的地方能接住,所以这里必须是最宽的那一档
        except BaseException as e:
            #后台 agent 崩了不该打死进程,也不该静默:异常文本当结果送回去,
            #主agent 会在 <task_notification> 里看到"已失败"和原因
            output = f"{type(e).__name__}: {e}"
            status = "failed"
        self._finalize(task_id, status, output)

    #收尾:记状态 → 还名额 → 存结果 → 挂进邮箱 → 通知回调。返回是否真的收了尾
    #
    #这是**唯一的终态写入口**:正常跑完(_run_agent / _run_bash)和看门狗补账(reap_dead)
    #都走它,所以"回调只发一次""名额只还一次""结果只进邮箱一次"只用在这里保证一遍
    #
    #status 不是 running 就直接返回:已经有人收过尾了。看门狗和 worker 会抢这件事
    #(看门狗判定线程没了,而 worker 那条路可能刚好抢先写完结果),幂等让先到的那个胜出 ——
    #真结果和补账结果不会同时进邮箱
    #
    #回调必须在**锁外面**调:on_done 那头会去碰 Scheduler(自己的锁),
    #派发器还会在回调里回头调 collect()(本类的锁)—— 持锁调就是自找死锁
    def _finalize(self, task_id, status, output):
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None or task["status"] != "running":
                return False
            task["status"] = status
            #先还名额:名额是"在跑"这种资源,收尾只是记账,不该占着它
            if task["kind"] == "agent":
                self.agents_running -= 1
            self.results[task_id] = {
                "kind": task["kind"],
                "status": status,
                "output": output,
            }
            self.ready.append(task_id)
            on_done, task["on_done"] = task["on_done"], None   #回调只准发一次
        if on_done is not None:
            try:
                on_done(status == "completed", output)
            except Exception as e:
                print(f"[task_runner] {task_id} 的完成回调出错:{type(e).__name__}: {e}")
        return True

    #看门狗:把"线程已经没了、结果却没留下"的任务补成一条**失败结果**塞进邮箱。
    #返回这次补了哪几条(调用方打日志、测试断言都要用)
    #
    #为什么必须有它(实测的洞):_run_agent 是那条线程的唯一出口,线程只要不是从那里走出来,
    #self.tasks 里的 status 就**永远**停在 running —— 邮箱是空的,has_completed() 是 False,
    #主agent 于是既不醒、也没有任何可查的东西,就那么空等一条不会来的通知。
    #而"线程没了"本来是可观测的(thread.is_alive()),前提是句柄得留着 —— submit_* 现在留着。
    #
    #为什么补成"结果"而不是新加一个判断条件:结果一进邮箱,主agent 就顺着**现成的**
    #has_completed() → <task_notification> 那条路知道了,main.py 的唤醒条件一个字都不用改。
    #(main.py 那两个条件"必须写在同一个 if 里"是有原因的,能不加探针就不加)
    #
    #谁来调:主循环那个 1 秒超时槽,和 process_control_messages 并列。
    #不自己开线程 —— 唤醒源必须留在主线程上(main.py 顶部那段是承重墙)
    def reap_dead(self):
        with self.lock:
            dead = [tid for tid, task in self.tasks.items()
                    if task["status"] == "running"
                    and task["thread"] is not None
                    and not task["thread"].is_alive()]
        for task_id in dead:
            #_finalize 是幂等的,谁先收到尾谁负责喊这一声
            if self._finalize(task_id, "failed", _THREAD_DIED_OUTPUT):
                print(f"[task_runner] {task_id} 的后台线程在写出结果之前就终止了,"
                      f"已按失败补一条通知")
        return dead

    #领取已完成任务的结果(主线程在 run() 开头调)
    #返回 {task_id: {"kind", "status", "output"}} —— 带上 kind 和 status 是为了让通知能说清
    #"这是 bash 还是子agent""跑成了还是失败了",模型据此判断要不要补救
    def collect(self):
        with self.lock:
            ready = self.ready.copy()
            self.ready.clear()

            results = {}

            for task_id in ready:
                results[task_id] = self.results.pop(task_id)
                self.tasks.pop(task_id)

            return results

    #有没有"已完成但还没被领走"的结果
    #**只看不取** —— 和 has_team_message() 同一个约定:判断该不该把 agent 叫起来,
    #真正的领取留给 run() 开头的 collect()。主agent 是唯一领取者,两边都是主线程,不用加锁防重
    def has_completed(self):
        with self.lock:
            return len(self.ready) > 0

    #当前还在跑的 task_id 列表(快照)
    def running(self):
        with self.lock:
            return [tid for tid, task in self.tasks.items()
                    if task["status"] == "running"]

    #正在跑的后台任务快照,**纯读、不改任何状态**(给"问一句后台怎么样了"用)。
    #返回的是数据不是文本:怎么措辞是工具那一层的事,本类不认识工具长什么样
    #
    #每条都带上已跑时长,因为这是唯一能自己看出"卡住了没有"的线索:
    #光报一串 id,模型没法和"正常在跑"区分开 —— 而这正是它要问的东西
    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            return {
                "agents_running": self.agents_running,
                "max_agents": MAX_BACKGROUND_AGENTS,
                "running": [
                    {"task_id": tid, "kind": task["kind"],
                     "elapsed": now - task["started_at"]}
                    for tid, task in self.tasks.items()
                    if task["status"] == "running"
                ],
            }

    #还有没有后台 agent 的名额(供调用方在**提交之前**问)
    #为什么要提前问:定时任务一旦被派发器取走就变成 running,这时候才发现没名额,
    #就得再走一条"把 running 放回待执行"的回退路径。回退路径本身是必要的
    #(主对话和派发器会抢名额,问了到提交之间仍可能被抢),但能不走就不走
    def has_agent_slot(self):
        with self.lock:
            return self.agents_running < MAX_BACKGROUND_AGENTS

    #退出前的有界等待:返回仍**在跑**的 task_id 列表(空 = 全跑完了)
    #和"等结果"是两回事:已经跑完但没被领走的那批不在此列 —— 它们已经落在 self.results 里
    #不会再变,退出丢掉它们只是丢一条通知,不算中断工作
    #
    #每轮先 reap:已经没了的线程不算"还在跑",不 reap 的话下面那句报数会把它们算成
    #"退出会中断它们",对着一条早就停了的线程说"会被中断"
    def wait_running(self, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            self.reap_dead()
            running = self.running()
            if not running:
                return running
            if time.monotonic() > deadline:
                return running
            time.sleep(0.3)
