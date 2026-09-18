#-------------Team 成员注册表:身份(ID)与生命周期状态的管理--------------
#职责边界(设计.md 三/五):
#  MessageBus 只管消息收发;成员是谁、现在什么状态,归这里管。
#  成员表的写权限只属于主agent —— 成员能读(team_list),不能改。


import re
import secrets
import threading
from dataclasses import dataclass


#===== 消息类型(设计.md 六)=====
#普通业务消息:交给 agent 的正常工作流程(会进 LLM)
KIND_CHAT = "chat"
#以下都是生命周期控制消息:属于 Team Runtime 的控制面,由代码处理,绝不进 LLM。
#不再用 content == "/exit" 这种字符串约定来判断生命周期(设计.md 十三·5)
KIND_OFFLINE_REQ = "offline_req"        # 主agent → 成员:请你下线
KIND_OFFLINE_AGREE = "offline_agree"    # 成员 → 主agent:同意下线,我要停了
KIND_OFFLINE_REFUSE = "offline_refuse"  # 成员 → 主agent:拒绝下线(还有活)


#===== 逻辑生命周期状态(设计.md 三·status)=====
#必须能区分"Main 希望它退出"和"它的线程实际上已经退出",
#所以 exiting(正在退出)和 offline(已经退出)是两个状态
ALIVE = "alive"      # 在岗:运行中或 idle 等消息
EXITING = "exiting"  # 正在退出:已收到下线请求,等它最终确认 + 真的停下来
OFFLINE = "offline"  # 已经退出:确认过,且线程确实停了
DEAD = "dead"        # 异常死亡:线程没了,但没走下线协议(由主agent对账时发现)


#消息类型过滤:以 predicate 形式传给 MessageBus 的 drain/has_message/count。
#这样 MessageBus 自己不需要知道"哪种 kind 算控制消息",保持纯基础设施(设计.md 五)
def is_chat(message) -> bool:
    return message.kind == KIND_CHAT


def is_control(message) -> bool:
    return message.kind != KIND_CHAT


@dataclass
class Member:
    """一个 Team 成员的记录。

    id 是通信身份,status 是主agent维护的逻辑状态,agent/thread 是物理实体。
    逻辑状态和线程状态可能暂时不一致(设计.md 四),所以这里不把两者绑死,
    而是提供 self_state / thread_alive 供对账使用。
    """
    id: str
    name: str
    task: str = ""
    status: str = ALIVE
    agent: object = None    # 该成员的 ClaudeMini 实例
    thread: object = None   # 跑 run_forever 的那条线程
    note: str = ""          # 最近一次下线确认的说明(同意/拒绝的理由)

    @property
    def self_state(self) -> str:
        """成员自己报的执行状态(idle/work/exit),只用于展示 ——
        线程的事只有成员自己知道,主agent不去猜(设计.md 四)"""
        return getattr(self.agent, "state", "?")

    def thread_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class TeamRuntime:
    """Team 成员注册表,由 Main Agent 持有(设计.md 三)。"""

    def __init__(self, owner_id: str = "main"):
        self.owner_id = owner_id        # 主agent的 ID:它不是成员,但永远可寻址
        self._members = {}              # {id: Member},插入顺序 = 创建顺序
        self._lock = threading.RLock()  # 成员表被多条线程并发访问(各自对账/描述)

    #---------- 身份 ----------

    def is_owner(self, agent_id: str) -> bool:
        return agent_id == self.owner_id

    def register(self, name: str, task: str = "") -> Member:
        """登记一个新成员并分配唯一 ID"""
        with self._lock:
            member = Member(id=self._new_id(name), name=name, task=task)
            self._members[member.id] = member
            return member

    def _new_id(self, name: str) -> str:
        """id = name + 短随机串。

        唯一,而且成员下线后**不回收** —— 新的同名成员一定拿到不同的 ID,
        所以不会出现"旧成员的历史消息被新成员继承"(设计.md 三·id / 七)
        """
        base = re.sub(r"\s+", "_", (name or "").strip()) or "member"
        while True:
            mid = f"{base}-{secrets.token_hex(2)}"
            if mid not in self._members and mid != self.owner_id:
                return mid

    def get(self, member_id: str):
        with self._lock:
            return self._members.get(member_id)

    def update(self, member_id: str, **fields):
        with self._lock:
            member = self._members.get(member_id)
            if member is None:
                return None
            for key, value in fields.items():
                setattr(member, key, value)
            return member

    def all(self) -> list:
        with self._lock:
            return list(self._members.values())

    def namesakes(self, name: str, exclude: str = "") -> list:
        """同名成员的 ID 列表(重名是允许的,但寻址时必须用 ID)"""
        with self._lock:
            return [m.id for m in self._members.values() if m.name == name and m.id != exclude]

    def resolve_member(self, ref: str):
        """把模型给的引用解析成成员:完整 ID 优先;名字只在唯一时才接受。

        返回 (member, error),error 是可以直接回给模型的说明文字。
        """
        ref = (ref or "").strip()
        if not ref:
            return None, "❌ 没有指定收件人。"
        with self._lock:
            member = self._members.get(ref)
            if member is not None:
                return member, None
            same = [m for m in self._members.values() if m.name == ref]
        if len(same) == 1:
            return same[0], None
        if len(same) > 1:
            ids = ", ".join(m.id for m in same)
            return None, f"❌ 名字 {ref} 对应多个成员,请改用 ID 指定:{ids}"
        return None, f"❌ 团队里没有成员 {ref}。当前成员:{self.brief()}"

    #---------- 展示 ----------

    def brief(self) -> str:
        """一行式成员摘要,用于错误提示"""
        with self._lock:
            if not self._members:
                return "无"
            return "; ".join(f"{m.id}({m.name}/{m.status})" for m in self._members.values())

    def describe(self) -> str:
        """成员列表(只读,不改任何状态)"""
        members = self.all()
        if not members:
            return "当前团队为空(用 team_spawn 创建成员)。"
        lines = [f"- {self.owner_id} | 主agent | alive | - | -"]
        for m in members:
            #逻辑状态已终结的成员,它自报的执行状态不再有意义
            state = m.self_state if m.status in (ALIVE, EXITING) else "-"
            lines.append(f"- {m.id} | {m.name} | {m.status} | {state} | {(m.task or '')[:40]}")
        return ("团队成员(ID | 名字 | 逻辑状态 | 执行状态 | 任务):\n" + "\n".join(lines) +
                "\n逻辑状态:alive 在岗 / exiting 已请求下线、等确认 / offline 已下线 / dead 线程异常终止。\n"
                "执行状态由成员自己报(idle 空闲等待 / work 工作中)。给成员发消息必须用 ID。")

    #---------- 对账 ----------

    def reconcile(self) -> list:
        """用线程事实给逻辑状态对账:状态还写着在岗、线程却已经没了 → 改成 dead(设计.md 四)。

        只补"非协议性死亡"。按协议下线的成员不走这里 ——
        那由主agent在收到最终确认、且线程确实停了之后置为 offline。
        """
        dead = []
        with self._lock:
            for m in self._members.values():
                if m.status in (ALIVE, EXITING) and m.thread is not None and not m.thread.is_alive():
                    m.status = DEAD
                    dead.append(m)
        return dead
