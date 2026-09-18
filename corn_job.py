import json
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

# 调度器扫描间隔(秒):每分钟扫一次,与 cron 的最小粒度一致
SCAN_INTERVAL_SECONDS = 60

# 推算下次触发时刻的搜索上界(天):要覆盖闰日任务(如 "0 0 29 2 *",最长隔 8 年)
_SEARCH_DAYS = 366 * 8

# 任务五态(待执行 / 执行中 / 已完成 / 已失败 / 已取消)
JOB_PENDING = "pending"        # 已创建,等待到点派发
JOB_RUNNING = "running"        # 已被 agent 取走执行
JOB_COMPLETED = "completed"    # 本次执行成功
JOB_FAILED = "failed"          # 本次执行失败
JOB_CANCELLED = "cancelled"    # 已取消:不再被调度,记录保留(可 resume 恢复)

# 合法状态流转表:当前状态 -> 允许切换到的状态(状态修改的唯一依据)
JOB_TRANSITIONS = {
    JOB_PENDING:   {JOB_RUNNING, JOB_CANCELLED},  # 被 agent 取走执行 / 被取消
    JOB_RUNNING:   {JOB_COMPLETED, JOB_FAILED},   # 执行成功 / 失败(执行中不可取消)
    JOB_COMPLETED: {JOB_PENDING, JOB_CANCELLED},  # 周期任务:下一轮重新待执行 / 被取消
    JOB_FAILED:    {JOB_PENDING, JOB_CANCELLED},  # 同上:失败也等下一轮再跑 / 被取消
    JOB_CANCELLED: {JOB_PENDING},                 # 恢复:重新放回调度池(resume_job)
}

# 工具(job_update_status)允许模型汇报的状态白名单
# 不含 pending:completed/failed → pending 是 tick 给周期任务开新一轮用的内部边,
# 放开它模型就能把跑完的任务「复活」。流转表管「能不能变」,这里管「准不准报」
JOB_REPORTABLE = {JOB_COMPLETED, JOB_FAILED}

# schedule 五段及各自取值区间:分 时 日 月 周(周:0=周日,7 也写作周日)
_CRON_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31),
                ("month", 1, 12), ("weekday", 0, 7))


#任务数据类
@dataclass
class Job:
    id: str                         # 任务id
    content: str                    # 执行内容(交给 agent 的自然语言指令)
    schedule: str | None = None     # 周期触发:类 linux cron 五段 "分 时 日 月 周"
    once_at: str | None = None      # 一次性触发:"YYYY-MM-DD HH:MM"(跑过一次就不再触发)
    status: str = JOB_PENDING       # 状态:pending / running / completed / failed
    last_run: str | None = None     # 上次实际派发的时刻桶(补跑时记的是迟到的时刻,不是原定时刻)
    next_run: str | None = None     # 下次该派发的时刻桶;None = 已无后续触发(一次性任务跑过了)

    # 构造时即校验:两种触发方式二选一,格式非法立刻报错,不留到执行时才发现
    def __post_init__(self):
        if bool(self.schedule) == bool(self.once_at):
            raise ValueError(
                "任务必须且只能给一种触发方式:schedule(周期,如 '0 9 * * *')"
                "或 once_at(一次性,如 '2026-09-16 15:00')")
        if self.once_at:
            self.once_at = self._parse_once_at(self.once_at)   #归一化,便于后面直接比时间桶
        else:
            self._parsed = self._parse_schedule(self.schedule)

    #解析一次性时刻:必须是 "YYYY-MM-DD HH:MM",返回归一化结果(容忍 "2026-9-6 9:05" 这类写法)
    @staticmethod
    def _parse_once_at(value: str) -> str:
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            raise ValueError(
                f"once_at '{value}' 格式错误:需要 'YYYY-MM-DD HH:MM'(如 '2026-09-16 15:00')")

    #解析 schedule:按空白切五段,逐段展开成允许值集合
    @staticmethod
    def _parse_schedule(schedule: str) -> dict:
        if not schedule or not schedule.strip():
            raise ValueError("schedule 不能为空(格式:分 时 日 月 周,如 '0 9 * * *')")
        parts = schedule.split()
        if len(parts) != 5:
            raise ValueError(
                f"schedule '{schedule}' 格式错误:需要 5 段(分 时 日 月 周),实际 {len(parts)} 段")
        parsed = {}
        for (name, lo, hi), expr in zip(_CRON_FIELDS, parts):
            parsed[name] = Job._parse_field(expr, lo, hi, schedule)
        # 周日既写作 0 也写作 7,统一归到 0,避免匹配时两边都要判
        if 7 in parsed["weekday"]:
            parsed["weekday"].discard(7)
            parsed["weekday"].add(0)
        return parsed

    #展开单个字段:支持 *、a、a-b、*/n、a-b/n、以及逗号列表(如 "1,15,30")
    @staticmethod
    def _parse_field(expr: str, lo: int, hi: int, schedule: str) -> set:
        values = set()
        for part in expr.split(","):
            part = part.strip()
            if not part:
                raise ValueError(f"schedule '{schedule}' 格式错误:字段 '{expr}' 含空项")
            step = 1
            if "/" in part:
                part, _, step_text = part.partition("/")
                if not step_text.isdigit() or int(step_text) <= 0:
                    raise ValueError(
                        f"schedule '{schedule}' 格式错误:步长 '{step_text}' 必须是正整数")
                step = int(step_text)
            if part == "*":
                start, end = lo, hi
            elif "-" in part:
                start_text, _, end_text = part.partition("-")
                if not start_text.isdigit() or not end_text.isdigit():
                    raise ValueError(f"schedule '{schedule}' 格式错误:区间 '{part}' 必须是数字")
                start, end = int(start_text), int(end_text)
            elif part.isdigit():
                start = int(part)
                end = hi if step > 1 else start   # "5/10" 表示从 5 起每 10 一个
            else:
                raise ValueError(f"schedule '{schedule}' 格式错误:无法识别 '{part}'")
            if start < lo or end > hi or start > end:
                raise ValueError(
                    f"schedule '{schedule}' 格式错误:'{part}' 超出取值区间 {lo}-{hi}")
            values.update(range(start, end + 1, step))
        return values

    #下一次该派发的时刻桶(严格晚于 after);返回 None 表示已无后续触发
    #这是判断「该不该跑」的唯一依据:不看「此刻是否匹配」,只看「欠不欠一次执行」——
    #所以原定 9:00 而进程 10:00 才启动时,算出的仍是 9:00 那个过去的时刻,到点判定成立,补跑
    def compute_next_run(self, after: str) -> str | None:
        if self.once_at:
            return self.once_at if self.once_at > after else None   #一次性只有一个点,过了就没了
        p = self._parsed
        start = datetime.strptime(after, "%Y-%m-%d %H:%M") + timedelta(minutes=1)
        hours, minutes = sorted(p["hour"]), sorted(p["minute"])
        #先按天跳:整天不可能命中就跳过,免得一分钟一分钟地扫
        #(稀疏表达式如 '0 15 16 9 *' 傻扫要几十万次,而这段是在锁里跑的,会卡住 agent)
        for offset in range(_SEARCH_DAYS):
            day = start + timedelta(days=offset)
            if (day.month not in p["month"] or day.day not in p["day"]
                    or (day.weekday() + 1) % 7 not in p["weekday"]):   # Python:0=周一 → cron:0=周日
                continue
            for h in hours:         #这天命中,再在允许的时/分里找第一个晚于 after 的
                for m in minutes:
                    candidate = day.replace(hour=h, minute=m)
                    if candidate >= start:
                        return Job.bucket(candidate)
        return None     #超过搜索上界(见 _SEARCH_DAYS)视为不再触发

    #时间桶:把时刻截断到分钟,如 "2026-09-14 09:00"
    @staticmethod
    def bucket(now: datetime | None = None) -> str:
        return (now or datetime.now()).strftime("%Y-%m-%d %H:%M")

    #触发方式的可读文本(供 job_list 展示):周期显示 cron,一次性显示具体时刻
    @property
    def trigger(self) -> str:
        return f"一次性 {self.once_at}" if self.once_at else f"周期 {self.schedule}"


#任务调度类:只做两件事 —— 扫描到点的任务、把它放进队列
class Scheduler:

    def __init__(self, store_path: str):
        self.store_path = Path(store_path)  #任务库文件:启动加载、每次变更写回
        self.jobs = {}      # id -> Job
        self.queue = []     #已到点、等待 agent 取走的任务(派生状态,不落盘)
        self.lock = threading.Lock()   #扫描线程与 agent 线程都会碰 jobs/queue/文件
        self._thread = None    #扫描线程,start() 靠它防重入
        self._load()

    #生成任务id:job_ + uuid(全局唯一,免跨会话/跨库撞号)
    def _new_id(self) -> str:
        return f"job_{uuid.uuid4().hex}"

    #启动时从文件加载(跨会话可读;文件不存在则视为空库)
    def _load(self) -> None:
        if not self.store_path.exists():
            return
        raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        for job_id, data in raw.get("jobs", {}).items():
            try:
                job = Job(**data)
            except (TypeError, ValueError) as e:
                #单条任务损坏(如手改坏了 schedule)只跳过它,不该拖垮整个调度器
                print(f"[scheduler] 跳过无法加载的任务 {job_id}: {e}")
                continue
            #旧版任务库没有 next_run 字段,从上次实际派发的时刻往后补算
            #(没派发过就从当前时刻算,同样只补一次 —— compute_next_run 只给一个点,
            # 推进发生在 take_job,所以堆积多久也不会变成一串补跑)
            if job.next_run is None:
                job.next_run = job.compute_next_run(after=job.last_run or Job.bucket())
            self.jobs[job_id] = job

    #变更即存:把整个任务表写回文件(文件写入不可并发,调用方必须已持有 lock)
    def _save(self) -> None:
        payload = {
            "jobs": {jid: asdict(j) for jid, j in self.jobs.items()},
        }
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    #创建任务:schedule / once_at 二选一,格式非法在 Job 构造时直接抛错;落库并返回其id
    def create_job(self, content: str, schedule: str = None, once_at: str = None) -> str:
        if not content or not content.strip():
            raise ValueError("创建任务必须提供 content(执行内容)")
        job = Job(id=self._new_id(), content=content,
                  schedule=schedule, once_at=once_at)   #解析触发方式放锁外,失败不碰库
        #已过去的一次性任务永远不会被派发(那个时间桶早走过去了),当场拦住而不是让它变僵尸。
        #这道校验只放 create 不放进 Job:从文件加载的旧任务本来就可能已经跑过,不能因此加载失败
        if job.once_at and job.once_at < Job.bucket():
            raise ValueError(
                f"once_at '{job.once_at}' 已经是过去的时间,不会被触发"
                f"(当前 {Job.bucket()});请给一个将来的时刻")
        #首次派发时刻:一次性任务就是那一刻(用 once_at 本身,不能用 compute_next_run ——
        #刚建好就赶上同一分钟时会算出 None,任务当场变哑巴);周期任务从当前时刻往后推
        job.next_run = job.once_at or job.compute_next_run(after=Job.bucket())
        with self.lock:
            self.jobs[job.id] = job
            self._save()
        return job.id

    #核心:扫描所有任务,把到点的放进队列;返回本轮新入队的任务
    #判定条件是「欠不欠一次执行」(next_run <= 当前时刻桶),不是「此刻是否匹配 cron」——
    #所以进程晚启动也照样把欠的那次补上;而 last_run/next_run 都在 take_job 才推进,
    #同一分钟内反复扫描、进程在派发后崩溃重启,都不会重复触发
    def tick(self, now: datetime | None = None) -> list:
        now_bucket = Job.bucket(now)
        with self.lock:
            dispatched = []
            for job in self.jobs.values():      #持锁迭代:防 create_job 在迭代中改字典大小
                if job.status in (JOB_RUNNING, JOB_CANCELLED):  #执行中不重复派发、已取消不再派发
                    continue
                if job.next_run is None:        #已无后续触发(一次性任务跑过了)
                    continue
                if job.next_run > now_bucket:   #还没到点
                    continue
                if any(j.id == job.id for j in self.queue):   #还在队列里没被取走,不再入队
                    continue
                if job.next_run < now_bucket:   #原定时刻已过去 → 这次是补跑,只提示不阻断
                    print(f"[scheduler] 补跑任务 {job.id}(原定 {job.next_run},"
                          f"现在 {now_bucket}):{job.content}")
                self._transition(job, JOB_PENDING)  #周期任务开启新一轮(completed/failed → pending)
                self.queue.append(job)
                dispatched.append(job)
            if dispatched:
                self._save()
            return dispatched

    #状态流转的唯一入口:校验合法性后改写(调用方必须已持锁,并自行负责落盘)
    def _transition(self, job, status: str) -> None:
        if job.status == status:
            return      #已是目标状态,幂等放行
        allowed = JOB_TRANSITIONS.get(job.status, set())
        if status not in allowed:
            raise ValueError(
                f"任务 {job.id} 不能从 {job.status} 变为 {status}"
                f"(当前状态只允许变为:{sorted(allowed) if allowed else '无,已是终态'})")
        job.status = status

    #agent 取走一个到点的任务:出队、置为执行中(running)并推进下次时刻;队列空则返回 None
    #时间指针在这里推进而不是在 tick:队列不落盘,派发后、取走前进程崩了,
    #next_run 仍是旧值,重启后再扫一次就自动补上 —— 「补跑」正是靠这一点成立的
    def take_job(self):
        with self.lock:
            if not self.queue:
                return None
            job = self.queue.pop(0)
            self._transition(job, JOB_RUNNING)   # pending → running
            now_bucket = Job.bucket()
            job.last_run = now_bucket            #记的是实际派发时刻(补跑时即迟到的时刻)
            job.next_run = job.compute_next_run(after=now_bucket)
            self._save()
            return job

    #按任务id修改状态(工具入口):只准汇报执行结果;状态值越界或流转非法都抛 ValueError
    def update_job_status(self, job_id: str, status: str) -> None:
        if status not in JOB_REPORTABLE:    #先卡状态值,再进锁查库
            raise ValueError(
                f"status 只接受 {sorted(JOB_REPORTABLE)},收到 '{status}'。"
                f"任务的下一次执行由调度器按 schedule 自动安排,不需要手动改回 pending。")
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise ValueError(f"任务 {job_id} 不存在")
            self._transition(job, status)
            self._save()

    #取消任务:置为 cancelled,不再被调度,记录保留(job_list 仍能看到,可 resume 恢复)
    #执行中(running)的任务取消不了 —— agent 已经领走在跑了,拦不住;
    #那种情况(含进程崩溃后卡在 running 的僵尸任务)用 delete_job 直接清掉
    def cancel_job(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise ValueError(f"任务 {job_id} 不存在")
            self._transition(job, JOB_CANCELLED)
            self._drop_from_queue(job_id)
            self._save()

    #恢复任务:把已取消的任务放回调度池(cancelled → pending),下次到点照常派发
    #只认已取消的任务 —— 其余状态要么本来就在调度里、要么正在跑,「恢复」无从谈起。
    #(流转表必须让 completed/failed → pending 合法,那是 tick 开新一轮用的内部边,
    # 所以这里像 JOB_REPORTABLE 一样,由方法自己再卡一道「准不准从这条路走」)
    def resume_job(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise ValueError(f"任务 {job_id} 不存在")
            if job.status != JOB_CANCELLED:
                raise ValueError(
                    f"任务 {job_id} 当前是 {job.status},只有已取消(cancelled)的任务才能恢复")
            self._transition(job, JOB_PENDING)
            self._save()

    #删除任务:连同记录一起从任务库移除,不可恢复(任何状态都可删)
    def delete_job(self, job_id: str) -> None:
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError(f"任务 {job_id} 不存在")
            del self.jobs[job_id]
            self._drop_from_queue(job_id)
            self._save()

    #把任务从待领取队列里摘掉:取消/删除后必须做,否则 agent 仍会把它领走执行
    #(job 早已派发进队列、只是还没被领走,状态仍是 pending,cancel 能成功但队列里还挂着)
    def _drop_from_queue(self, job_id: str) -> None:
        self.queue = [j for j in self.queue if j.id != job_id]

    #队列里是否还有待领取的任务(供 AgentRunner 轮询;持锁只读,不碰扫描逻辑)
    def has_pending(self) -> bool:
        with self.lock:
            return len(self.queue) > 0

    #列出所有任务(返回快照,调用方可安全遍历)
    def list_jobs(self):
        with self.lock:
            return list(self.jobs.values())

    #每分钟扫描一次(守护线程,随主进程退出);主/子agent共享实例,重复调用不会起第二条线程
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            self.tick()
            time.sleep(SCAN_INTERVAL_SECONDS)

