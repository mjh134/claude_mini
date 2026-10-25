import json
import threading
import time
from datetime import datetime
from pathlib import Path
from bash_exec import execute_bash
import trace
import ui

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


#上次进程被强杀时没来得及收尾的任务,重启后转成邮箱通知的正文(_load 用它)。
#口气对齐 _THREAD_DIED_OUTPUT:诊断结论 + 指出该重新提交 —— 模型看到要知道任务已经
#死了、不会再来通知,而不是继续等。只交代不重放:和 cron 僵尸回收同一个哲学,
#副作用任务(发消息/改文件)重放是危险的,宁可漏一次也不能重放
#task_id 不用传进正文:通知外壳(build_task_notifications)已经带了
def _interrupted_output(kind, desc, submitted_at):
    label = "命令" if kind == "bash" else "任务描述"
    return (
        "这条后台任务在上次进程运行期间被中断(进程没等到它收尾就没了),它没有跑完。\n"
        f"提交时刻:{submitted_at}\n"
        f"当时的{label}:{desc}\n"
        "它没做完的活没有人接手。需要的话先确认现在还该不该做,再重新提交一次。"
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
    #store_path(可选):后台任务日志的落盘文件。**显式 opt-in**:None(默认)完全不碰盘 ——
    #进程里 runner 有好几个(主对话/派发器/定时任务内部),不是每个都值得落:
    #派发器那份邮箱重启后没人读,落了也白落(cron 任务中断由 Scheduler._load 的
    #僵尸回收交代);bench/eval 更不能把评测垃圾写进日常库。谁需要恢复能力谁显式传。
    #只存"未收尾"的任务:提交时登记、收尾时删条目 —— 盘上存在 = 被中断,_load 零分支。
    #已完成未领取的结果**不落盘**:工作已经做完,丢的只是通知,不算中断
    #stop_event(可选):取消信号。不传自建(行为不变);传了就用外部的那个 ——
    #“要退出了”是**进程级**的事实,但 runner 有好几份(主对话/派发器/每个成员/
    #每个 job agent 各一份),各持各的 event 的话,主线程 cancel_all 置了自己那份,
    #别的 runner 名下在跑的任务一个都收不到。所以建 runner 时把共享的那个传进来
    #(主对话/子 agent 共享整个 runner 实例,天然同一个;成员/Job agent 走构造参数)
    def __init__(self, id_prefix="bg", store_path=None, stop_event=None):
        self.id_prefix = id_prefix
        self.store_path = Path(store_path) if store_path else None
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.tasks = {}         # task_id -> _record() 那份形状(含 thread / on_done)
        self.ready = []         # 已完成、等待领取的 task_id(邮箱)
        self.results = {}       # task_id -> {"kind", "status", "output"}
        self.counter = 0
        self.agents_running = 0 #当前在跑的后台 agent 数,对着 MAX_BACKGROUND_AGENTS 卡
        self.lock = threading.Lock()
        #落盘专用锁,和内存锁分开:盘上的 read-modify-write 要整段串行(两个线程同时
        #收尾两条任务,各自读到旧文件再写回就会互相覆盖),但不该让内存锁陪着文件 IO。
        #锁序永远是 lock -> _io_lock 单向,没有反向获取,无死锁环
        self._io_lock = threading.Lock()
        self._load()            #上次进程留下的未收尾任务,转成邮箱通知(见 _load)

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
        #落盘必须在线程 start 之前:线程跑得再快,删盘(收尾)也只会发生在加盘之后 ——
        #反过来的话,一条瞬完成的任务会在盘上留下幽灵条目,下次启动被误报成中断
        self._persist_add(task_id, "bash", command)
        #agent_id 用通道名:本类不认识 agent(职责边界契约),runner 前缀本就是为区分主对话/派发器设计的
        trace.get_trace().emit("task_event",
                               {"action": "submit", "kind": "bash", "command": command},
                               agent_id=f"runner-{self.id_prefix}", correlation_id=task_id)
        thread = threading.Thread(target=self._run_bash, args=(task_id, command), daemon=True)
        thread.start()
        self._attach_thread(task_id, thread)
        return task_id

    #执行体:bash。取消信号传下去:execute_bash 在轮询档里看到置位就杀树返回
    def _run_bash(self, task_id, command):
        try:
            output, exit_code = execute_bash(command, stop_event=self.stop_event)
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
    #desc(可选):这个 agent 任务是"干什么"的一段人类可读描述(主对话传子agent 的 prompt)。
    #work 是闭包,本类看不见里面的内容 —— 没有这段描述,中断交代只能说"一个 agent 任务没了",
    #模型和用户没法判断丢了什么。它只进日志和中断交代,不参与任何执行逻辑
    def submit_agent(self, work, on_done=None, desc=""):
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
        #锁过了才记——名额满返回 None 的路径不该有这条事件
        trace.get_trace().emit("task_event",
                               {"action": "submit", "kind": "agent"},
                               agent_id=f"runner-{self.id_prefix}", correlation_id=task_id)
        #和 submit_bash 同理:start 之前落盘,防"先删后加"的幽灵条目
        self._persist_add(task_id, "agent", desc)
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
        #终态事件放锁外(锁里做 IO 是死锁素材),且只有真收尾才记——幂等保证它只响一次
        trace.get_trace().emit("task_event",
                               {"action": "finalize", "status": status, "output": output},
                               agent_id=f"runner-{self.id_prefix}", correlation_id=task_id)
        #盘上条目同步删掉(False 路径不动盘:先收尾的那个已经删过了)。删了它,
        #"盘上存在 = 未收尾"这个不变量才成立,下次启动才不会把正常跑完的任务误报成中断
        self._persist_remove(task_id)
        if on_done is not None:
            try:
                on_done(status == "completed", output)
            except Exception as e:
                ui.warn(f"[task_runner] {task_id} 的完成回调出错:{type(e).__name__}: {e}")
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
                ui.warn(f"[task_runner] {task_id} 的后台线程在写出结果之前就终止了,"
                      f"已按失败补一条通知")
        return dead

    #--- 后台任务日志(落盘/加载) ---
    #文件形状:{"tasks": {task_id: {"kind", "desc", "submitted_at"}}},只存未收尾的任务。
    #submitted_at 用 wall clock:它要跨进程读给人看,monotonic 一重启就归零没有意义;
    #内存里那份 started_at(monotonic) 不受影响 —— 两套时钟各管各的(算时长/跨进程)

    #登记一条到盘上。写失败只警告不抛:持久化是恢复面,不能因为它挂掉任务提交主流程
    def _persist_add(self, task_id, kind, desc):
        if self.store_path is None:
            return
        entry = {"kind": kind, "desc": desc,
                 "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        try:
            with self._io_lock:
                data = {"tasks": {}}
                if self.store_path.exists():
                    data = json.loads(self.store_path.read_text(encoding="utf-8"))
                data.setdefault("tasks", {})[task_id] = entry
                self.store_path.parent.mkdir(parents=True, exist_ok=True)
                self.store_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError) as e:
            ui.warn(f"[task_runner] 后台任务日志写不进去(中断后将无法交代 {task_id}):"
                    f"{type(e).__name__}: {e}")

    #从盘上删一条(_finalize 的真收尾路径调)
    def _persist_remove(self, task_id):
        if self.store_path is None:
            return
        try:
            with self._io_lock:
                if not self.store_path.exists():
                    return
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                if data.get("tasks", {}).pop(task_id, None) is None:
                    return
                self.store_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError) as e:
            ui.warn(f"[task_runner] 后台任务日志删不掉 {task_id}"
                    f"(下次启动可能误报中断):{type(e).__name__}: {e}")

    #把盘清成空库:启动加载把遗留条目全转成通知之后调 —— 不清的话,
    #下次启动会把同一批中断再交代一遍
    def _persist_reset(self):
        try:
            with self._io_lock:
                self.store_path.parent.mkdir(parents=True, exist_ok=True)
                self.store_path.write_text(
                    json.dumps({"tasks": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError) as e:
            ui.warn(f"[task_runner] 后台任务日志清不掉(下次启动会重复交代):"
                    f"{type(e).__name__}: {e}")

    #启动加载:上次进程留下的未收尾任务,每条转成一个"中断交代"塞进邮箱。
    #主 agent 下一轮顺着现成的 has_completed() -> collect() -> <task_notification>
    #看到它,main.py 的唤醒条件一个字不用改(这正是选择"补成结果"而不是新探针的理由,
    #与 reap_dead 同一套思路)
    def _load(self):
        if self.store_path is None or not self.store_path.exists():
            return
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
            entries = dict(raw.get("tasks", {}))
        except (OSError, ValueError) as e:
            ui.warn(f"[task_runner] 后台任务日志 {self.store_path.name} 读不出来,"
                    f"中断交代放弃:{type(e).__name__}: {e}")
            try:    #坏文件改名留证,免得每次启动都撞同一堵墙;失败就留着,下次再报
                self.store_path.rename(
                    self.store_path.with_name(self.store_path.name + ".bad"))
            except OSError:
                pass
            return
        for task_id, entry in entries.items():
            kind = entry.get("kind", "?")
            output = _interrupted_output(kind, entry.get("desc", ""),
                                         entry.get("submitted_at", "未知时刻"))
            rec = _record(kind)
            #status 必须不是 running:reap_dead / running() 只认 running,
            #interrupted 让这条"记录"不会被当成在跑的任务;而 tasks 里必须有它,
            #collect() 领取时那句 tasks.pop 才不会 KeyError
            rec["status"] = "interrupted"
            self.tasks[task_id] = rec
            self.results[task_id] = {"kind": kind, "status": "interrupted", "output": output}
            self.ready.append(task_id)
            ui.warn(f"[task_runner] 发现上次中断的后台任务 {task_id}({kind}),已转为通知")
        if entries:
            self._persist_reset()

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

    #广播取消(退出路径的第一步,ROADMAP 项 8):置 stop_event,本 runner 及共享同一
    #event 的所有 runner 名下的执行体在下个检查点/轮询档里停下来 —— bash 最坏一档
    #(_POLL_SECONDS)内被杀树;后台 agent 在途的 LLM 请求让它落地,落地后由 run() 开头
    #的检查点兜住,不再发起新请求。返回此刻仍在跑的 task_id 快照(调用方报数用)。
    #幂等:event 重复 set 是 no-op。配套用法见 wait_running:先 cancel_all 再等待,
    #等不进窗口的任务盘上登记还在,重启转中断交代(和项 7 同一条恢复线)
    def cancel_all(self):
        self.stop_event.set()
        return self.running()

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
