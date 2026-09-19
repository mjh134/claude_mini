import threading
import time

from claude import ClaudeMini
from task_runner import TaskRunner
from TOOLS import ROLE_MAIN, TEAM_TOOLS

#轮询间隔(秒):多久看一眼 scheduler 队列里有没有到点的任务
POLL_INTERVAL_SECONDS = 5

#喂给定时任务agent的开场白,接在任务内容前面。
#它要说清三件事,都是"这次执行"和"用户对话"不一样的地方:
#- 没人在场:别反问、别等确认,把事做完给结论(旧设计里任务是主agent自己领的,
#  跑在用户的会话里,所以能反问 —— 现在不能了,不说清它会写一堆"请问您需要…");
#- 敏感操作会被拒:需要用户批准的(写记忆、覆盖已有文件等)由 permission_hook 判定,
#  而确认窗口只能在主线程弹 —— 任务跑在后台线程,那道判断会直接拒(HOOKS.py)。
#  要提前讲,否则它会反复重试同一条被拒的命令,白烧 token;
#- 结果去哪:没人当场看,这是留给日志和任务状态的
JOB_PROMPT = (
    "你正在执行一条**定时任务**,不是和用户对话:此刻没有人在终端,没有人能回答问题。\n"
    "把任务做完、给出结论即可,不要反问、不要等确认。\n"
    "需要用户批准的操作(写记忆、覆盖已有文件等)会被直接拒绝,遇到就换一条不需要批准的做法。\n"
    "你的最终回复会作为这条任务的执行结果记录下来。\n"
    "任务内容:\n"
)


#定时任务派发器:把 Scheduler 队列里到点的任务,派进独立的后台agent执行。
#
#它替换掉了原来的 AgentRunner。旧设计是"叫醒主agent,让它自己用 job_take 领任务"——
#任务因此在**用户的会话里**执行:占用主对话的历史、和用户抢话题、模型还能顺手把任务
#状态改坏。现在任务在自己的实例、自己的线程、自己的历史里跑,跑完把结果汇报回 Scheduler。
#
#职责边界(和 TaskRunner 对齐):Scheduler 只管"什么时候该跑",本类只管"跑起来,并回报"。
#所以这两件事都发生在这里,而 Scheduler 对线程、agent、模型一无所知。
class JobDispatcher:

    #claude_mini 只用来借三样**共享服务**:task_store / mcp,以及(通过 scheduler 参数)调度器。
    #任务agent必须是新建的实例 —— 不能借用传进来的这个:它是用户正在对话的那个,
    #历史、线程、工具表都是用户的
    def __init__(self, claude_mini, scheduler,
                 poll_interval: float = POLL_INTERVAL_SECONDS):
        self.claude_mini = claude_mini
        self.scheduler = scheduler
        self.poll_interval = poll_interval
        #派发器自己的执行器:**不**用主对话那一个。理由两条:
        #① 邮箱不能共用 —— 主对话是那份邮箱的收件人,任务结果一进去就会被当成
        #   <task_notification> 灌进用户的对话。一个每分钟跑的任务会把对话刷爆;
        #② 名额各算各的 —— 定时任务吃满后台名额时,用户的交互式子agent还能照跑。
        #   代价是总上限翻倍(进程里两个 runner,最坏 2 x MAX_BACKGROUND_AGENTS 个在跑),
        #   这是有意的:两条通道互不挤占,比一个共用的大池子更可预期
        self.runner = TaskRunner(id_prefix="bg_job")
        self._thread = None

    #启动派发线程(守护线程,随主进程退出);重复调用不会起第二条
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    #轮询循环:队列里有任务就派,派不动就等下一轮
    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as e:
                #轮询线程不能死:它是定时任务唯一的执行通道,死了就再没人跑任务,
                #而且不会有任何提示 —— 表现是"任务创建了却永远不执行",最难查的那种
                print(f"[dispatcher] 本轮派发出错: {type(e).__name__}: {e}")
            time.sleep(self.poll_interval)

    #把队列里到点的任务尽可能多地派出去,直到没名额或队列空
    def _tick(self) -> None:
        #先看门狗,再谈派发。任务agent的线程要是没了,它的 on_done 永远不会被调用,
        #那条任务就永远停在 running;更糟的是**它占的名额也不会还** ——
        #攒满 MAX_BACKGROUND_AGENTS 之后 has_agent_slot() 永久为 False,
        #下面那个 return 就成了永久出口,所有定时任务从此不再派发(而且一声不吭)
        self.runner.reap_dead()
        while self.scheduler.has_pending():
            #名额不够就**整个不取**:任务留在 Scheduler 队列里等下一轮。
            #不能先取再放回 —— 取走会同时推进 next_run、把状态改成 running,放回要还原两处
            #(requeue_job 就是干这个的),能不取就不取
            if not self.runner.has_agent_slot():
                return
            job = self.scheduler.take_job()     #出队、置 running、推进 next_run
            if job is None:
                return      #has_pending 与实际取之间队列空了(还有别的消费者时)
            print(f"[dispatcher] 派发任务 {job.id}:{job.content[:120]}")
            task_id = self.runner.submit_agent(
                lambda tid, j=job: self._run_job(tid, j),
                on_done=lambda ok, out, jid=job.id: self._report(jid, ok, out))
            if task_id is None:
                #问了名额、到提交之间被抢走了(同一 runner 上别的派发也在抢名额)。
                #任务必须原样放回:take_job 已经推进过 next_run,不放回这次派发就白丢,
                #一次性任务(once_at)更会因此永远不再触发
                print(f"[dispatcher] 名额被抢,任务 {job.id} 放回待执行")
                self.scheduler.requeue_job(job.id)
                return      #名额已满,本轮到此为止

    #执行体:在后台线程里跑一条定时任务(由 TaskRunner 调用,收 task_id)
    def _run_job(self, task_id, job):
        agent = ClaudeMini(
            slient=False,               #任务自己那些话不往终端刷(用户可能正在打字)
            role=ROLE_MAIN,             #任务要能写记忆、建任务、用全部工具
            agent_name=task_id,         #身份用派发 id(bg_job_0001):banner、日志、通知都对得上号
            task_store=self.claude_mini.task_store,
            scheduler=self.scheduler,
            mcp=self.claude_mini.mcp,
            #每个任务一份**自己的**执行器。共用派发器那一份会出事:两个任务同时在跑时,
            #谁先跑到 run() 开头就把对方提交的后台结果领走了(邮箱只能有一个读者,
            #子agent那边靠 drains_results 挡,这里靠"各用各的"挡)。
            #前缀用 task_id 是为了不撞号:任务里的后台任务长成 bg_job_0001_0002,一眼看出是谁的。
            #drains_results=True 是必须的:这份邮箱**只有它一个读者**(派发器读的是自己那份),
            #不显式说一声,构造函数会按"别人给的邮箱"默认当成不收,任务就再也看不见
            #自己起的后台任务跑出什么了
            task_runner=TaskRunner(id_prefix=task_id),
            drains_results=True,
            #团队工具必须挡掉:这个实例是 ROLE_MAIN,但它**不拥有一棵长命的树** ——
            #TeamRuntime 和 MessageBus 都是它自己新建的,任务一结束就随它一起没了。
            #真让它 team_spawn,members 会在一条没人读的总线上跑,产出的东西永远送不回来
            #(旧设计里任务跑在主agent实例上,建出来的成员是真成员,所以这条是新增的守卫)
            deny_tools=TEAM_TOOLS,
        )
        return agent.run([{"role": "user", "content": JOB_PROMPT + job.content}])

    #收尾回调:把结果汇报给 Scheduler,并领走自己邮箱里那条
    def _report(self, job_id, ok, output):
        self.scheduler.report_done(job_id, ok, output)
        #本 runner 的收件人就是派发器自己,结果已在上面汇报掉了。
        #不领的话 tasks/results 会随着每一轮任务一直涨 —— 长期跑下去就是内存泄漏
        self.runner.collect()
