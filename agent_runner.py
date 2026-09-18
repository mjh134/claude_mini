import threading
import time

#轮询间隔(秒):多久看一眼 scheduler 队列里有没有到点的任务
POLL_INTERVAL_SECONDS = 5

#唤醒冷却(秒):一次唤醒结束后,多久之内不再唤醒
#防模型没去领任务时 Runner 反复唤醒 —— 每次唤醒都是一次完整 agent run,会持续烧 API
WAKE_COOLDOWN_SECONDS = 60

#唤醒时喂给 agent 的提示:只是「叫醒」,任务内容仍由 agent 自己用 job_take 领取
WAKE_PROMPT = (
    "有定时任务已到执行时间。请调用 job_take 领取任务并执行,"
    "执行完成后调用 job_update_status 汇报结果(成功 completed / 失败 failed)。"
)


#定时任务执行者:负责唤醒 agent 去执行队列里的任务
#Scheduler 只管扫描入队,队列里的任务「怎么被执行」由本类承接
class AgentRunner:

    def __init__(self, claude_mini, scheduler,
                 poll_interval: float = POLL_INTERVAL_SECONDS,
                 cooldown: float = WAKE_COOLDOWN_SECONDS):
        self.claude_mini = claude_mini
        self.scheduler = scheduler
        self.poll_interval = poll_interval
        self.cooldown = cooldown
        self.last_wake = 0.0    #上次唤醒结束时刻(单调时钟),冷却计时用
        self._thread = None

    #启动唤醒线程(守护线程,随主进程退出);重复调用不会起第二条
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    #轮询循环:队列非空且已过冷却,就唤醒一次
    def _loop(self) -> None:
        while True:
            self._tick()
            time.sleep(self.poll_interval)

    def _tick(self) -> None:
        if not self.scheduler.has_pending():
            return
        if time.monotonic() - self.last_wake < self.cooldown:
            return      #冷却中,不重复唤醒
        self._wake()
        self.last_wake = time.monotonic()

    #唤醒 agent 执行定时任务
    #用一份独立 history,与用户在终端的对话完全隔离 —— 主线程阻塞在 input() 里,
    #若共用同一份 history,两个线程会同时改一个 list,且加锁护不住阻塞在 input() 的主线程
    def _wake(self) -> None:
        print("[agent_runner] 检测到待执行定时任务,唤醒 agent")
        history = [{"role": "user", "content": WAKE_PROMPT}]
        try:
            self.claude_mini.run(history)
        except Exception as e:
            #唤醒失败不该打死轮询线程(冷却结束后仍会重试)
            print(f"[agent_runner] 唤醒执行失败: {e}")
