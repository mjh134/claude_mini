from datetime import datetime
import threading
from llm import LLM
from TOOLS import Tools,create_default_registry
import HOOKS
from PROMPT import SYSTEM_PROMPT
from PROMPT import SUBAGENT_PROMPT
from PROMPT import TEAM_MEMBER_PROMPT
from PROMPT import OFFLINE_CONFIRM_PROMPT
from skill import  SkillLoader
from compact import CompactManager
from memory import MemoryManager
from task import TaskStore,create_task_handlers
from background import BackgroundManager
from corn_job import Scheduler
from message import Message
from team import (TeamRuntime, KIND_CHAT, KIND_OFFLINE_REQ, KIND_OFFLINE_AGREE,
                  KIND_OFFLINE_REFUSE, ALIVE, EXITING, OFFLINE, DEAD,
                  is_chat, is_control)
import time

#下线握手的等待上限。设计.md 里的握手本来没有上限:成员不回话,主agent 就永远停在 exiting
#(压测里成员线程崩掉后,主agent 就空等着,整整 13 分钟没人发现)。但"没回话"不等于"它死了",
#所以分两步走:到点先查状态,空闲就重发;再等一轮还没回话,才判定它失灵。
OFFLINE_ACK_TIMEOUT_SECONDS = 120   #每一轮等多久
OFFLINE_MAX_ROUNDS = 2              #总共给几轮:第一轮可以重发,第二轮结束就判定失灵(所以重发最多 1 次)
#请求文本要和重发的一模一样:成员侧靠 kind 触发、不看内容,但日志和排查要靠它可读、可对比
OFFLINE_REQ_TEXT = ("主agent判断当前不再需要你,请做最终确认:如果还有没做完的工作、"
                    "还在等别人回复,就拒绝下线;确认可以结束了就同意下线。")

class ClaudeMini():

    def __init__(self,show_thinking=False,slient = True,allow_recall_memory=True,allow_subagent=True,allow_write_memory=True,task_store=None,scheduler=None,allow_jobs=True,message_bus=None,agent_name="main",allow_team=True,agent_id=None,team_runtime=None):

        self.skill_loader = SkillLoader()
        self.compact_mannager = CompactManager()
        self.memory_manager = MemoryManager()
        self.background_manager = BackgroundManager()
        self.tools = Tools

        # 加载长期记忆并注入 system prompt
        session_memory = self.memory_manager.load_session_memory()

        # (只在启动时注入一次;会话跨天后会过期,job_create 的工具描述里已提示可用 bash date 复核)
        _now = datetime.now()
        current_time = f"\n当前时间:{_now.strftime('%Y-%m-%d %H:%M')} 周{'一二三四五六日'[_now.weekday()]}"

        if session_memory:
            self.system_prompt = SYSTEM_PROMPT + "\n" + session_memory + current_time + "\n" + "你可以使用以下skill解决相关问题:\n" + self.skill_loader.catalog()
        else:
            self.system_prompt = SYSTEM_PROMPT + "\n" + current_time + "\n" + "你可以使用以下skill解决相关问题:\n" + self.skill_loader.catalog()

        #团队成员看到的规则和主agent不同(不能建团队、不能自己退出、要如实做下线确认),
        #不清不楚会让它照主agent的说明去"组建团队"甚至谎报结果(测试报告·问题6)
        if message_bus is not None and not allow_team:
            self.system_prompt += "\n" + TEAM_MEMBER_PROMPT

        self.llm = LLM(self.system_prompt,self.tools)

        self.tool_registry = create_default_registry()   
        self.tool_registry.register("subagent",self.run_subagent)
        self.tool_registry.register("load_skill",self.skill_loader.load)
        self.tool_registry.register("recall_memory",self.recall_memory)

        self.show_thinking = show_thinking   #是否展示思考
        self.slient = slient        #是否展示子agent消息
        self.allow_recall_memory = allow_recall_memory #是否允许调用 recall_memory(子agent默认False防递归)
        self.allow_subagent = allow_subagent       #是否允许(再)启动子agent(子agent默认False防递归)
        self.allow_write_memory = allow_write_memory #是否允许写入长期记忆(子agent默认False防污染共享库)

        # 注册 memory 工具;禁用的工具注册守卫桩,给出指引而非裸报错
        for name, handler in self.memory_manager.create_handlers().items():
            if getattr(self, "allow_" + name, True):
                self.tool_registry.register(name, handler)
            else:
                self.tool_registry.register(name, self._memory_guard(name))

        # 任务系统:库文件落在当前目录 .task/tasks.json(跨会话保留);主/子agent共享同一 store
        self.task_store = task_store if task_store is not None else TaskStore(".task/tasks.json")
        for name, handler in create_task_handlers(self.task_store).items():
            self.tool_registry.register(name, handler)

        # 定时任务:库文件落在当前目录 .task/jobs.json(跨会话保留)
        # 扫描线程与锁都不可复制,主/子agent必须共享同一个 scheduler 实例 ——
        # 否则会各起一条扫描线程、各持一把锁去写同一个文件,导致文件交错写坏
        self.scheduler = scheduler if scheduler is not None else Scheduler(".task/jobs.json")
        self.allow_jobs = allow_jobs    #是否允许领取定时任务(子agent默认False,由主agent统一执行)
        self.scheduler.start()          #重复调用是 no-op,共享实例时子agent不会起第二条线程

        # 注册定时任务的七个工具(Schema 在 TOOLS.py,实现在下面的 job_* 方法)
        # 子agent不碰定时任务:注册守卫桩,给出指引而非裸报错
        for name in ("job_create", "job_list", "job_cancel", "job_resume",
                     "job_delete", "job_take", "job_update_status"):
            if self.allow_jobs:
                self.tool_registry.register(name, getattr(self, name))
            else:
                self.tool_registry.register(name, self._job_guard(name))

        # agent_teams:接入消息总线才有团队身份(message_bus=None 即原来的单agent模式,行为不变)
        self.message_bus = message_bus
        self.agent_name = agent_name
        #通信身份是 ID,不是名字:名字可以重复(只用于展示/日志/模型理解),
        #ID 唯一且成员下线后不回收(设计.md 三)
        self.agent_id = agent_id or agent_name
        self.allow_team = allow_team    #是否允许创建/管理团队成员(成员默认False,防无限扩张)
        self.running = False        # run_forever 的生命周期开关(确认下线时才置 False)
        self.state = "idle"         # idle / work / exit,成员自己报的执行状态(仅供展示)
        self._team_history = []     # 团队对话历史(跨消息保留)
        self._notices = []          # 待送达模型的团队通知(成员上下线),存 (文本, 是否紧急)
        self._deferred_control = [] # run() 期间收到、按"完成当前执行边界再处理"推迟的控制消息
        self._offline_decision = None   # 成员侧的最终确认结果:(agree, reason)
        #下线握手看门狗(只有主agent用):member_id -> {"deadline": 单调时钟绝对时刻, "rounds_done": 轮次}
        #存绝对时刻而不是倒计时:检查来晚了(主agent正卡在一次长工具调用里)也不会把窗口越推越长
        self._offline_watch = {}

        #成员管理归 TeamRuntime(由主agent持有),MessageBus 只管收发(设计.md 五)
        self.team_runtime = team_runtime
        if self.message_bus is not None:
            # 只有团队模式才注册这些工具,单agent模式下模型看不到
            self.tool_registry.register("send_message", self.send_message)
            if self.allow_team:
                if self.team_runtime is None:
                    self.team_runtime = TeamRuntime(owner_id=self.agent_id)
                self.tool_registry.register("team_spawn", self.team_spawn)
                self.tool_registry.register("team_list", self.team_list)
                self.tool_registry.register("team_stop", self.team_stop)
            else:
                #成员只能读成员表;创建/下线成员的写权限属于主agent(设计.md 十一)。
                #禁用工具注册守卫桩,给指引而不是裸报错(也减少模型"谎报已组建团队")
                self.tool_registry.register("team_list", self.team_list)
                self.tool_registry.register("team_offline_confirm", self.team_offline_confirm)
                self.tool_registry.register("team_spawn", self._team_guard("team_spawn"))
                self.tool_registry.register("team_stop", self._team_guard("team_stop"))


    #agent循环
    def run(self,history):

        #prompt过长重试次数
        reactive_retries = 0
        #跨轮保存最近一次有正文的回复,最后一轮无正文时兜底返回
        last_text = ''

        
        while True:
            #收集后台任务执行结果
            results = self.background_manager.collect()
            notification = self.build_task_notifications(results)
            if notification:
                history.append(notification)
            #收团队成员发来的消息(主agent由用户输入驱动,不能阻塞等消息,只能主动收件)
            team_message = self.collect_team_messages()
            if team_message:
                history.append(team_message)
            #发送消息
            try:
                response = self.llm.send(history)
            except Exception as e:
                text = str(e).lower()
                too_long = any(k in text for k in
                               ("prompt_too_long", "prompt is too long", "too many tokens"))
                if not too_long or reactive_retries >= 1:
                    raise
                print("[reactive compact] prompt过长,压缩后重试")
                history = self.compact_mannager.reactive_compact(history, self.llm)
                reactive_retries += 1
                continue
            reactive_retries = 0

            #压缩模型查看过的工具调用结果
            history = self.compact_mannager.micro_compact(history)

            #最后一轮执行结果
            final_text = ''

            for block in response.content:
                if block.type == "thinking":
                    if self.show_thinking:
                        print(f"\n🧠...\n{block.thinking}\n") #默认不展示思考过程
                    else:
                        continue
                elif block.type == "text" :
                    if self.slient:
                        print(f"\n🤖 Assistant: {block.text}\n")
                    final_text += block.text

            if final_text:
                last_text = final_text

            history.append({"role": "assistant", "content": response.content})

            #收集tool_use
            tool_calls = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_calls.append(block)

            #没有工具调用需求退出循环   
            if not tool_calls:
                #等待后台任务完成
                if self.background_manager.has_background_tasks():
                    if self.background_manager.wait_background_tasks(timeout=300):
                        continue
                    return last_text
                return last_text  #返回子agent最后一轮执行结果(最后一轮无正文则退回上一轮)
            
            #调用本轮工具
            tool_results = []
            for block in tool_calls:
                output = self.excute_tool(block)          
                tool_result = {
                        "type":"tool_result",
                        "tool_use_id": block.id,
                        "content": str(output)
                    }
                
                #压缩单次工具调用结果
                tool_result = self.compact_mannager.tool_result_budget(tool_result)    
                tool_results.append(tool_result)                 

            
            history.append({
                "role": "user",
                "content": tool_results
            })

            #压缩消息数量
            history = self.compact_mannager.snip_compact(history)

            #模型总结上下文
            history = self.compact_mannager.llm_compact(history,self.llm)

    #agent teams
    # 成员的生命周期:idle(真阻塞) → work → idle ... 直到主agent请求下线并完成最终确认
    def run_forever(self):

        if self.message_bus is None:
            raise RuntimeError("未接入 MessageBus,无法进入团队模式(构造时传 message_bus=...)")

        self.running = True
        self._team_history = team_history = []   # 跨消息保留:agent 记得和同事聊过什么

        while self.running:
            # ↓ idle 阻塞点:没有发给自己的消息时,线程挂在 bus 的 receive 里(0 CPU),
            #   普通消息和控制消息都能把它唤醒 —— 不用轮询、不用定时唤醒(设计.md 十二)
            self.state = "idle"
            msg = self.message_bus.receive(self.agent_id)

            #控制面:生命周期控制消息交给代码处理,绝不进 LLM(设计.md 六)
            if is_control(msg):
                self._handle_control(msg)
                self._drain_deferred_control()
                continue

            #数据面:普通消息包成 user 输入,交给既有的 run() 跑一整个工作期。
            # run() 返回(模型不再调工具)= 这条消息处理完了
            self.state = "work"
            print(f"\n[{self.agent_id}] 收到来自 {msg.sender} 的消息")
            team_history.append({
                "role": "user",
                "content": (
                    f"<team_message sender=\"{msg.sender}\">\n{msg.content}\n</team_message>\n"
                    f"这是团队成员发来的消息(sender 是它的 ID),请处理。"
                    #这里原来写的是"如需回复对方,调用 send_message" —— 那是在邀请回复。
                    #实测后果:共识达成后成员之间又刷了 53 条消息(38 条成员↔成员),
                    #其中 42 条短于 25 字,最后十条是"喵~ 🐱"来回发,靠某次调用碰巧没调工具才停下。
                    #改成有条件的:没有新信息就别回,别把"回一句"当成默认动作
                    f"只有你确实有新的实质信息要告诉对方、或对方问了需要你回答的问题时,"
                    f"才调用 send_message 回复(to=\"{msg.sender}\")。"
                    f"只是收到/同意/感谢这类客套话不要回 —— 你回一句它再回一句,会没完没了。"
                )
            })
            self.run(team_history)

            #当前执行边界结束 → 处理期间被推迟的控制消息(设计.md 十二),
            #然后回到 idle:成员干完活不自动退出,继续在岗等消息(设计.md 二·1)
            self._drain_deferred_control()

        #退出:把自己收件箱里剩下的消息报出来,否则它们会一直躺在总线上(设计.md 七)
        self.state = "exit"
        left = self.message_bus.drain(self.agent_id)
        if left:
            print(f"[{self.agent_id}] 下线,丢弃 {len(left)} 条来不及处理的消息")

    def send_message(self, to: str, content: str) -> str:
        """团队协作:给另一个成员(或主agent)发消息。用 ID 寻址(名字只在唯一时可用)。

        发之前先确认收件人属于当前团队 —— 不存在/已下线的成员直接拒绝,
        不能让消息留在总线上等着被"未来某个 agent"错误消费(设计.md 七)
        """
        if self.message_bus is None:
            return "❌ 当前未接入消息总线,无法发送消息。"
        if not (content or "").strip():
            return "❌ 消息内容为空。"
        runtime = self.team_runtime
        if runtime is None:
            return "❌ 当前没有成员表,无法确认收件人(只有主agent能发起协作)。"

        to = (to or "").strip()
        #主agent不是团队成员,但它永远可寻址
        if runtime.is_owner(to):
            self.message_bus.send(Message(sender=self.agent_id, receiver=runtime.owner_id,
                                          content=content, kind=KIND_CHAT))
            return f"✅ 消息已发送给主agent({runtime.owner_id})。"

        member, err = runtime.resolve_member(to)
        if err:
            return err
        if member.status not in (ALIVE, EXITING):
            return (f"❌ 成员 {member.id} 当前状态是 {member.status},已不在岗,消息未送达。"
                    f"当前成员:{runtime.brief()}。若还需要它工作,请用 team_spawn 重新创建成员。")
        #exiting 的成员仍可收消息:它正在进行下线确认,收到消息后应当拒绝下线
        self.message_bus.send(Message(sender=self.agent_id, receiver=member.id,
                                      content=content, kind=KIND_CHAT))
        return f"✅ 消息已发送给 {member.id}({member.name})。对方空闲时会被立即唤醒。"

    def collect_team_messages(self):
        """取走总线里发给自己的消息,包成一条 user 消息(非阻塞,没有就返回 None)。

        分流(设计.md 六):
        - 普通消息 → 拼成 <team_message> 块,交给 LLM
        - 控制消息 → 就地交给代码处理,绝不进 LLM;其中下线请求推迟到本轮执行边界之后
        - 团队通知(成员上下线)→ 拼成 <team_notice> 块,让模型知道团队发生了什么
        多条消息会合并成一条 user 消息(队友与主agent走同一条路径,语义一致)
        """
        if self.message_bus is None:
            return None
        blocks = []
        for msg in self.message_bus.drain(self.agent_id):
            if is_control(msg):
                if msg.kind == KIND_OFFLINE_REQ:
                    #正在 run() 里:等当前执行边界结束再确认(设计.md 十二)
                    self._deferred_control.append(msg)
                else:
                    self._handle_control(msg)
                continue
            blocks.append({"type": "text",
                           "text": f"<team_message sender=\"{msg.sender}\">\n{msg.content}\n</team_message>"})
        #先处理完消息再对账,顺序反了会把"刚确认下线、线程刚停"的成员误判成异常死亡
        self._reconcile_team()
        if self._notices:
            blocks.insert(0, {"type": "text",
                              "text": "<team_notice>\n" + "\n".join(t for t, _u in self._notices)
                                      + "\n</team_notice>"})
            self._notices.clear()
        if not blocks:
            return None
        return {"role": "user", "content": blocks}

    def has_team_message(self) -> bool:
        """该不该把 agent 叫起来跑一轮工作:总线里有发给自己的**普通**消息,或有失败通知。

        只看不取。控制消息由 process_control_messages 就地处理,不该为它叫模型;
        但"成员失灵"这类通知必须专门叫一趟 —— 那时候往往没有别人再发消息了,
        只搭车就等于永远送不到(压测里主agent 就是这样静默停住的)
        """
        if self.message_bus is None:
            return False
        #带 wake 的通知必须专门叫一趟:成员出问题时往往没有别人再发消息,搭车就等于永远送不到
        if any(urgent for _text, urgent in self._notices):
            return True
        return self.message_bus.has_message(self.agent_id, is_chat)

    def team_spawn(self, name: str, prompt: str) -> str:
        """拉起一个常驻团队成员:分配唯一 ID → 起线程跑 run_forever → 派初始任务。

        成员干完当前的活不会自动退出:它回到 idle 继续等消息(设计.md 二·1)。
        真正让它下线的是 team_stop(主agent发起 → 成员最终确认)。
        """
        if self.message_bus is None:
            return "❌ 未接入消息总线,无法组建团队。"
        if not self.allow_team:
            return "❌ 团队成员不能再创建团队(避免无限扩张)。请自己完成,或把需求带回主agent。"
        name = (name or "").strip()
        if not name:
            return "❌ 成员名字不能为空。"
        if not (prompt or "").strip():
            return "❌ 初始任务不能为空。"

        runtime = self.team_runtime
        #重名是允许的:身份是 ID,名字只用于展示和模型理解(设计.md 三·name)
        member = runtime.register(name=name, task=prompt)

        #成员:禁再建团队/子agent、禁写记忆、禁领定时任务;共享任务库与调度器实例
        teammate = ClaudeMini(slient=True, show_thinking=False, allow_team=False,
                              allow_subagent=False, allow_write_memory=False,
                              allow_recall_memory=False, allow_jobs=False,
                              agent_name=name, agent_id=member.id,
                              message_bus=self.message_bus, team_runtime=runtime,
                              task_store=self.task_store, scheduler=self.scheduler)
        thread = threading.Thread(target=teammate.run_forever, name=member.id, daemon=True)
        thread.start()
        runtime.update(member.id, agent=teammate, thread=thread)

        #先把初始任务送进总线再返回:成员线程此刻可能还没跑到 receive,
        #但消息存在池子里等着,不丢 —— 这就是"存储转发"的好处
        self.message_bus.send(Message(sender=self.agent_id, receiver=member.id,
                                      content=prompt, kind=KIND_CHAT))
        twins = runtime.namesakes(name, exclude=member.id)
        warn = f"注意:团队里还有同名成员({', '.join(twins)}),给它发消息必须用 ID。\n" if twins else ""
        return (f"✅ 团队成员已启动:{member.id}(名字 {name}),初始任务已派发。\n" + warn +
                f"派发是异步的:它现在正在工作,完成后会发消息给你。"
                f"继续派活用 send_message(to=\"{member.id}\"),查看团队用 team_list。\n"
                f"它是常驻成员:干完活会回到 idle 等你继续派活,不会自己退出;"
                f"确实不再需要它时用 team_stop 请它下线。")

    def team_list(self) -> str:
        """查看团队成员(ID/名字/逻辑状态/执行状态)。
        成员也能调,但只能看,不能改 —— 成员表的写权限属于主agent(设计.md 十一)"""
        if self.team_runtime is None:
            return "当前没有成员表(单agent模式)。"
        self._reconcile_team()      #主agent顺手对账;成员侧是 no-op
        return self.team_runtime.describe()

    def team_stop(self, member: str) -> str:
        """请一个成员下线(设计.md 九)。它会做最终确认:有活就拒绝,没活才同意。

        这里只是"发出下线意图":逻辑状态置为 exiting,绝不假定它已经死了 ——
        要等它确认、并且线程确实停了,才由 _on_offline_reply 置为 offline。
        """
        if self.message_bus is None:
            return "❌ 未接入消息总线,没有团队可管。"
        runtime = self.team_runtime
        target, err = runtime.resolve_member(member)
        if err:
            return err
        if target.status == OFFLINE:
            return f"成员 {target.id} 已经下线了(需要它继续工作请重新 team_spawn)。"
        if target.status == DEAD:
            return f"成员 {target.id} 的线程已异常终止(dead),不用再发下线请求。"
        if target.status == EXITING:
            watch = self._offline_watch.get(target.id)
            if watch is None:
                #没有看门狗记录,而状态是 exiting:只有一种来路 —— 它已经确认过下线了,
                #只是线程还在收尾(见 _on_offline_reply)。不能再说"还在等它确认",那会让模型一直等
                return (f"成员 {target.id} 已经确认下线了,线程还在收尾(对账后会变成 offline),"
                        f"没有等待中的握手。")
            left = max(0, int(watch["deadline"] - time.monotonic()))
            return (f"已经向 {target.id} 发过下线请求,正在等它最终确认(它可能拒绝)。"
                    f"再过约 {left} 秒仍无回应,就会检查它的状态并重发或判定它失灵。")

        #先登记看门狗、再改状态、最后发消息:让"有记录 ⟺ 有一个等待中的握手"这条不变量成立
        self._arm_offline_watch(target.id)
        runtime.update(target.id, status=EXITING)   #正在退出 ≠ 已经退出
        self._send_offline_request(target)
        return (f"✅ 已向 {target.id}({target.name}) 发出下线请求(状态 exiting),等它最终确认。\n"
                f"它同意的话会回消息,届时状态变为 offline;若它拒绝,会说明理由并继续在岗。\n"
                f"结果出来之前不要给它派新活;想了解进展可以用 team_list 看状态。\n"
                f"若 {OFFLINE_ACK_TIMEOUT_SECONDS} 秒内毫无回应,会先查它的状态、空闲就重发一次,"
                f"再等 {OFFLINE_ACK_TIMEOUT_SECONDS} 秒仍无回应就判定它失灵(标 dead)并通知你。")

    def team_offline_confirm(self, agree: bool, reason: str = "") -> str:
        """成员对主agent下线请求的最终确认(只有成员能看到这个工具)"""
        if self.allow_team:
            return "❌ 这个工具只对团队成员有意义。"
        self._offline_decision = (bool(agree), (reason or "").strip())
        if agree:
            return "✅ 已确认下线:你的线程会在这一轮结束后停止,之后不再收到消息。"
        return f"✅ 已回复主agent:暂不下线。理由:{reason or '(未说明)'}。你可以继续当前工作。"

    #===== 生命周期控制面(设计.md 九)=====
    #方向固定:主agent发起下线意图 → 成员最终确认 → 成员实际停止 → 主agent更新状态。
    #这些消息不经过 LLM;只有"这份工作是否还需要我"这种业务判断才问模型(设计.md 十)

    def process_control_messages(self) -> int:
        """只处理控制面消息,不叫模型 —— 用户没输入时,主循环靠它推进下线握手"""
        if self.message_bus is None:
            return 0
        handled = 0
        for msg in self.message_bus.drain(self.agent_id, is_control):
            self._handle_control(msg)
            handled += 1
        self._reconcile_team()      #顺序:先处理消息,再对账
        return handled

    def _handle_control(self, msg):
        """控制消息分流:确定性判断(成员是否存在、有没有待处理消息、线程是否存活)都在代码里做"""
        if msg.kind == KIND_OFFLINE_REQ:
            self._confirm_offline(msg)          # 成员侧:做最终确认
        elif msg.kind == KIND_OFFLINE_AGREE:
            self._on_offline_reply(msg, agreed=True)    # 主agent侧:同步状态
        elif msg.kind == KIND_OFFLINE_REFUSE:
            self._on_offline_reply(msg, agreed=False)
        else:
            print(f"[{self.agent_id}] 收到未知控制消息 {msg.kind},忽略")

    #----- 成员侧:收到下线请求 → 最终确认 -----

    def _confirm_offline(self, msg):
        """成员收到主agent的下线请求,做最后一次确认(设计.md 九)。

        不能无条件立即退出:先看确定性事实(还有没有待处理的活),再让模型做业务判断。
        """
        runtime = self.team_runtime
        if runtime is not None and runtime.is_owner(self.agent_id):
            print(f"[{self.agent_id}] 忽略发给主agent的下线请求")
            return
        #已经确认过下线、正在收尾(self.running 由 _reply_offline 置 False):
        #不要把第二份请求(主agent的重发)当成新活再干一轮
        if not self.running:
            print(f"[{self.agent_id}] 已在退出流程中,忽略重复的下线请求")
            return

        print(f"\n[{self.agent_id}] 主agent请求下线,做最终确认")
        #一拿到请求就报 work:看门狗只在成员"空闲却没回应"时才重发,
        #"有请求在手 = work"这条不变量是它的安全依据,所以要在任何判断之前就置上
        self.state = "work"

        #① 确定性判断:收件箱里还有待处理的普通消息 → 直接拒绝,不用打扰模型
        pending = self.message_bus.count(self.agent_id, is_chat)
        if pending:
            self._reply_offline(False, f"收件箱里还有 {pending} 条待处理消息", msg.sender)
            return

        #② 非确定性判断:交给模型(一次 run(),带 team_offline_confirm 工具)
        self._offline_decision = None
        self._deferred_control = []     # 确认期间新到的控制消息已包含在本次判断里
        history = self._team_history
        history.append({"role": "user", "content": OFFLINE_CONFIRM_PROMPT})
        self.run(history)
        decision = self._offline_decision

        #③ 模型没给出明确确认 → 保守处理:不同意下线(绝不能无条件退出)
        if decision is None:
            self._reply_offline(False, "没有给出明确确认", msg.sender)
            return
        agree, reason = decision

        #④ 确认期间又来了新消息 → 事实变了,撤回同意
        if agree:
            pending = self.message_bus.count(self.agent_id, is_chat)
            if pending:
                self._reply_offline(False, f"确认期间又收到 {pending} 条新消息", msg.sender)
                return

        self._reply_offline(agree, reason, msg.sender)

    def _reply_offline(self, agree: bool, reason: str, to: str = None):
        """把最终确认回给主agent。同意时:先回消息,再让自己的循环停下来。

        顺序不能反 —— 反过来的话,主agent可能先看到线程停了、却还没收到确认。
        """
        runtime = self.team_runtime
        owner = runtime.owner_id if runtime is not None else "main"
        self.message_bus.send(Message(
            sender=self.agent_id, receiver=to or owner, content=reason or "",
            kind=KIND_OFFLINE_AGREE if agree else KIND_OFFLINE_REFUSE))
        if agree:
            self.running = False        # ← 真正停线程的是这里;主agent只改逻辑状态
            self.state = "exit"
            print(f"[{self.agent_id}] 已确认下线,线程退出")
        else:
            self.state = "idle"
            print(f"[{self.agent_id}] 拒绝下线:{reason}")

    #----- 主agent侧:收到成员的最终确认 -----

    def _on_offline_reply(self, msg, agreed: bool):
        """成员的最终确认。逻辑状态要等它真的停下来才置为 offline(设计.md 三/四)"""
        runtime = self.team_runtime
        if runtime is None or not runtime.is_owner(self.agent_id):
            print(f"[{self.agent_id}] 忽略不属于自己团队的下线确认")
            return
        member = runtime.get(msg.sender)
        if member is None:
            print(f"[{self.agent_id}] 下线确认来自未知成员 {msg.sender},忽略")
            return
        #回信到了,握手就结束了 —— 不管它同意还是拒绝,都撤掉看门狗
        #(这条是"拒绝下线的成员不会被超时判死"的保证)
        self._offline_watch.pop(member.id, None)

        if not agreed:
            runtime.update(member.id, status=ALIVE, note=msg.content)
            self._notice(f"成员 {member.id}({member.name}) 拒绝下线:{msg.content}。"
                         f"它仍在岗,可以继续派活。")
            return

        #同意:等它真的停下来,再把逻辑状态置为 offline —— 状态要建立在事实之上
        if member.thread is not None:
            member.thread.join(timeout=5)
        if member.thread_alive():
            self._notice(f"成员 {member.id} 已确认下线,但线程还没结束"
                         f"(状态保持 exiting,稍后对账)。")
            return
        runtime.update(member.id, status=OFFLINE, note=msg.content)
        left = self.message_bus.drain(member.id)
        extra = f",并清掉 {len(left)} 条来不及处理的遗留消息" if left else ""
        self._notice(f"成员 {member.id}({member.name}) 已下线,线程已停止{extra}。"
                     f"需要它继续工作请重新 team_spawn。")

    def _notice(self, text: str, wake: bool = False):
        """记一条给模型看的团队通知。默认不为此单独叫模型,搭下一条团队消息的车送过去;
        wake=True 用于"成员失灵"这种必须让模型知道的事 —— 那时往往没人再发消息了,搭车等于送不到。

        紧急与否记在通知自己身上,不在别处另存一个标志位:两者就不可能跑到不同步
        (一旦错开,主循环会每秒都以为有事、每秒叫一次模型)
        """
        print(f"[team] {text}")
        self._notices.append((text, wake))

    #----- 下线握手看门狗:等待必须有上限(设计.md 九的握手补一条兜底)-----

    def _arm_offline_watch(self, member_id: str):
        """登记一次等待中的下线握手"""
        self._offline_watch[member_id] = {
            "deadline": time.monotonic() + OFFLINE_ACK_TIMEOUT_SECONDS, "rounds_done": 0}

    def _send_offline_request(self, member):
        """发出下线请求。首次和重发走同一条,保证成员收到的内容一模一样"""
        self.message_bus.send(Message(sender=self.agent_id, receiver=member.id,
                                      kind=KIND_OFFLINE_REQ, content=OFFLINE_REQ_TEXT))

    def _check_offline_timeouts(self):
        """到点先查成员状态,再决定重发还是判定失灵。

        只在主agent侧跑(_reconcile_team 已经做了 owner 闸门)。三条判据:
        - 状态已经不是 exiting:握手早结束了(收到回信的那一刻就撤了记录),这里只是兜底清场
        - 线程已经没了:线程刚死的那种不抢 reconcile 的活(它会判 dead 并通知模型);
          但"根本没有线程"的幽灵 reconcile 管不了,只能在这里了结
        - 重发的前提是成员**空闲**:work 说明它在干活(很可能正是在做下线确认),
          重发会让它把同一件事干两遍 —— 成员侧靠 kind 触发、不看内容,没法去重
        """
        if not self._offline_watch:
            return
        now = time.monotonic()
        for member_id, watch in list(self._offline_watch.items()):   #循环里会删记录,先取快照
            if now < watch["deadline"]:
                continue
            member = self.team_runtime.get(member_id) if self.team_runtime else None
            if member is None or member.status != EXITING:
                self._offline_watch.pop(member_id, None)    #握手已经结束(或成员已经不在表里)
                continue
            if not member.thread_alive():
                self._offline_watch.pop(member_id, None)
                if member.thread is None:
                    #没有线程的幽灵成员(register 成功、线程还没起就出了错的残骸):
                    #reconcile 管不了它(它要求 thread 非空),只能自己了结,
                    #否则它会永远卡在 exiting,主agent 又变回无限等待
                    self.team_runtime.update(member.id, status=DEAD, note="成员没有线程")
                    self._notice(f"成员 {member.id}({member.name}) 根本没有线程(创建时就没起来),"
                                 f"不可能回应下线请求,已直接标记为 dead。", wake=True)
                #线程刚停的那种不抢 reconcile 的活:它会判 dead 并通知模型
                continue

            watch["rounds_done"] += 1
            if watch["rounds_done"] >= OFFLINE_MAX_ROUNDS:
                self._give_up_on_member(member)             #最后一轮也等不到 → 判定失灵
                self._offline_watch.pop(member_id, None)
                continue

            watch["deadline"] = now + OFFLINE_ACK_TIMEOUT_SECONDS
            if member.self_state == "exit":
                #它已经同意下线、正在收尾(exit 只在 _reply_offline 同意后置上,那时回信已经发出):
                #握手从它这侧已经结束,回信马上就会被取走 —— 撤记录,别再等着判它失灵
                self._offline_watch.pop(member_id, None)
                continue
            if member.self_state == "idle":
                #空闲却没回应:它手上没活,消息大概率没被处理 → 重发
                #(只有第一轮会走到这里,所以重发最多一次)
                self._send_offline_request(member)
                print(f"[timeout] {member.id} 等满 {OFFLINE_ACK_TIMEOUT_SECONDS} 秒没回应下线请求,"
                      f"而它是空闲的,已重发一次")
            else:
                #它在干活(很可能正是在做下线确认):重发会让它重复干活,只续期
                print(f"[timeout] {member.id} 等满 {OFFLINE_ACK_TIMEOUT_SECONDS} 秒没回应下线请求,"
                      f"但它正在 {member.self_state},不重发(免得重复干活),再等一轮")

    def _give_up_on_member(self, member):
        """两轮都等不到回信:判定这成员失灵,标 dead 并让模型知道。

        它的线程可能还活着(Python 杀不掉线程)—— 这是有意的:dead 在这里的意思是
        "这个成员不再可信、不再被管理"。所以文案要说清:它没做完的活没人接手;
        万一它只是慢,稍后补上回信,状态会被自动纠正。
        """
        self.team_runtime.update(member.id, status=DEAD, note="下线请求超时无回应")
        left = self.message_bus.drain(member.id) if self.message_bus else []
        self._notice(
            f"成员 {member.id}({member.name}) 等满 {OFFLINE_ACK_TIMEOUT_SECONDS * OFFLINE_MAX_ROUNDS} 秒"
            f"仍未回应下线请求(最后自报状态 {member.self_state},"
            f"线程{'还在' if member.thread_alive() else '已停'}),已判定失灵并标记为 dead"
            + (f",清理 {len(left)} 条遗留消息" if left else "")
            + "。它没做完的活没有人接手,需要的话请重新 team_spawn。"
              "若它只是慢、稍后补上了回信,状态会被自动纠正。",
            wake=True)

    def _reconcile_team(self):
        """对账:线程已经没了、状态却还写着在岗 → 改成 dead,并清掉它的遗留消息。

        只补"非协议性死亡"。调用前必须保证控制消息已处理,否则刚确认下线的成员
        会被误判成异常死亡(它线程刚停、确认消息还没被取走)。
        所以这里自己先把控制消息处理掉 —— 不能指望调用方保证顺序:
        team_list 是模型随时可能调的工具,它也会走到这里,而那一刻主循环
        根本没轮到取消息(实测:成员已正常同意下线,却被报成 dead/线程异常终止)。
        """
        runtime = self.team_runtime
        if runtime is None or not runtime.is_owner(self.agent_id):
            return      #成员只读成员表,不改状态(设计.md 十一)

        #按协议下线的成员是"先发确认消息、再停线程",所以在消息被取走之前,
        #它和异常死亡长得一模一样 —— 先取消息,再对账,顺序不能反。
        if self.message_bus is not None:
            for msg in self.message_bus.drain(self.agent_id, is_control):
                self._handle_control(msg)

        for member in runtime.reconcile():
            left = self.message_bus.drain(member.id) if self.message_bus else []
            #线程意外终止是必须让模型知道的事(它的活没人接手了),所以唤醒
            self._notice(f"成员 {member.id}({member.name}) 的线程已意外终止(状态改为 dead)"
                         + (f",清理 {len(left)} 条遗留消息" if left else "")
                         + "。它没做完的活没有人接手,需要的话请重新 team_spawn。", wake=True)

        #最后再看一眼有没有"等不到回信"的下线握手。必须排在 drain + reconcile 之后:
        #刚按协议确认下线的成员,要先让它的确认消息被取走,否则会被这里当成"没回应"
        self._check_offline_timeouts()

    def _drain_deferred_control(self):
        """处理被推迟的控制消息(只在 run() 期间收到下线请求时才会有)"""
        deferred, self._deferred_control = self._deferred_control, []
        for msg in deferred:
            #已经决定停了就不要再处理:确认期间新到的下线请求(比如主agent的重发)
            #会把成员重新叫起来干一整轮 —— 已经签过字的人不该被再拉起来上班
            if not self.running:
                print(f"[{self.agent_id}] 已在退出流程中,丢弃 {len(deferred)} 条推迟的控制消息")
                return
            self._handle_control(msg)

    def _team_guard(self, name):
        msg = (f"❌ 团队成员不能调用 {name}(成员表的写权限只属于主agent,避免无限扩张)。"
               f"你可以用 team_list 查看团队、用 send_message 和同事或主agent协作。")
        return lambda *_a, **_kw: msg      #兼容位置/关键字两种调用方式

    #子agent
    def run_subagent(self, prompt: str):
        """启动一个独立子agent执行指定任务"""
        if not self.allow_subagent:
            return ("❌ 子agent禁止再次启动子agent(避免无限递归)。"
                    "请在当前层级直接完成任务,或把需要分步的部分带回父agent处理。")

        HOOKS.trigger_hooks("BefSubAgent",prompt)

        subagent_prompt = SUBAGENT_PROMPT + "\n" + prompt

        #子agent:禁再起子agent、禁写记忆、禁recall_memory(防递归+防污染共享记忆库);共享同一任务库
        #scheduler 必须共享同一实例(锁与扫描线程不可复制),但禁其领定时任务(allow_jobs=False)
        subagent = ClaudeMini(slient=False, allow_recall_memory=False,
                              allow_subagent=False, allow_write_memory=False,
                              task_store=self.task_store,
                              scheduler=self.scheduler, allow_jobs=False)

        history = [
            {
                "role": "user",
                "content": subagent_prompt
            }
        ]

        result = subagent.run(history)

        HOOKS.trigger_hooks("AftSubAgent",result)

        return result

    #记忆召回
    def recall_memory(self, query: str = None) -> str:
        """通过子 agent 召回相关记忆"""
        if not self.allow_recall_memory:
            return "❌ 子agent禁止调用 recall_memory(会造成子agent递归)。如需检索记忆,请直接 read_file 读取 memory/experience/index.json 及各记忆文件。"
        all_tags = self.memory_manager.get_all_tags()
        tags_hint = "可用标签: " + ", ".join(all_tags) if all_tags else "暂无标签"

        subagent_prompt = f"""你是一个记忆召回专家。请根据以下查询召回相关的经验记忆。

            {tags_hint}

            查询：{query or "请根据上下文召回相关记忆"}

            执行步骤：
            1. 分析查询中的关键概念
            2. 从标签索引中选择最相关的标签
            3. 读取匹配的记忆文件
            4. 返回最相关的记忆内容

            如果没有找到相关记忆，请明确说明。
"""

        return self.run_subagent(subagent_prompt)

    def excute_tool(self, block):
        if block.type != "tool_use":
            raise ValueError(f"Invalid block type: {block.type}. Expected 'tool_use'.")

        if block.input.get("is_background") and block.name == "bash":
            # 后台执行工具
            task_id = self.background_manager.start(block)
            output = f"🔄 工具 {task_id} 已在后台执行。"
            return output
        #获取工具处理函数
        handler = self.tool_registry.get(block.name)
        if handler is None:
            return f"❌ Unknown tool: {block.name}"

        hook_result = HOOKS.trigger_hooks("PreToolUse", block)
        if hook_result is not None:
            output = hook_result
            print(f"Hook result: {output}")
        else:
            output = self._call_tool(handler, block)
            #print(f"Tool {block.name} executed with output: {output}")
        return output

    def _call_tool(self, handler, block):
        """调用工具 handler:把"调用姿势不对"退化成一条工具错误,绝不让异常穿出去。

        必须挡住:模型偶尔会把参数名写错(实测团队成员把 team_offline_confirm 的
        agree 写成了 content),handler(**block.input) 会当场抛 TypeError。
        这个异常以前会一路穿到线程外 ——
        成员线程直接死掉(状态变 dead,主agent的下线握手就永远等不到确认),
        主agent则会把整个会话带崩(user_loop 没有兜底)。
        退化成工具错误后,模型能看到正确的参数名,自己改对重试。
        """
        try:
            return handler(**block.input)
        except TypeError as e:
            return (f"❌ 工具 {block.name} 调用参数有误:{e}\n"
                    f"本工具接受的参数:{self._tool_params(block.name)}。请按这些参数名重新调用一次。")
        except Exception as e:
            return f"❌ 工具 {block.name} 执行出错:{type(e).__name__}: {e}"

    def _tool_params(self, name):
        """从工具 schema 里取参数名 —— 报错时要把它回给模型,模型记不住 schema 写了什么"""
        for tool in self.tools:
            if tool.get("name") == name:
                schema = tool.get("input_schema", {})
                props = schema.get("properties", {})
                required = schema.get("required", [])
                return ", ".join(
                    f"{k}(必填)" if k in required else k for k in props) or "(无参数)"
        return "(未知工具)"

    #结构化后台任务消息                      
    def build_task_notifications(self, results):
        if not results:
            return None

        notifications = []

        for task_id, output in results.items():
            notifications.append({
                "type": "text",
                "text": (
                    f"<task_notification>\n"
                    f"后台任务 {task_id} 已完成。\n"
                    f"输出：\n{output}\n"
                    f"</task_notification>"
                )
            })

        return {
            "role": "user",
            "content": notifications
        }

    #===== 定时任务工具 handler =====     

    #守卫桩工厂:被禁用的工具不裸报错,而是返回一句指引(lambda 捕获工具名)
    def _job_guard(self, name):
        msg = (f"❌ 子agent禁止调用 {name}(定时任务统一由主agent管理,避免重复创建/重复执行)。"
               f"请直接完成手头工作,或把需要定时的需求带回父agent。")
        return lambda *_a, **_kw: msg      #兼容位置/关键字两种调用方式

    def _memory_guard(self, name):
        msg = (f"❌ 子agent禁止调用 {name}(避免污染共享记忆库)。"
               f"如需保存记忆,请让父agent(主对话)调用 {name}。")
        return lambda **_kw: msg

    def job_create(self, content, schedule=None, once_at=None):
        try:
            #触发方式二选一,哪个不合规(缺、多给、格式错、时刻已过去)都在这里抛错
            job_id = self.scheduler.create_job(content, schedule, once_at)
        except ValueError as e:
            return str(e)
        when = f"一次性 {once_at}" if once_at else f"周期 {schedule}"
        return f"已创建定时任务 {job_id}:「{content}」({when})"

    def job_list(self):
        jobs = self.scheduler.list_jobs()
        if not jobs:
            return "当前没有定时任务。"
        lines = []
        for j in jobs:
            line = f"[{j.status}] {j.id} | {j.trigger} | {j.content}"
            if j.last_run:
                line += f" | 上次派发 {j.last_run}"
            lines.append(line)
        return "\n".join(lines)

    def job_cancel(self, job_id):
        try:
            self.scheduler.cancel_job(job_id)
            return (f"已取消任务 {job_id}(不再被调度,记录保留;"
                    f"要恢复用 job_resume,想彻底清掉用 job_delete)")
        except ValueError as e:
            return str(e)

    def job_resume(self, job_id):
        try:
            self.scheduler.resume_job(job_id)
            return f"已恢复任务 {job_id}(已放回调度池,下次到点照常执行)"
        except ValueError as e:
            return str(e)

    def job_delete(self, job_id):
        try:
            self.scheduler.delete_job(job_id)
            return f"已删除任务 {job_id}(记录已从任务库移除,不可恢复)"
        except ValueError as e:
            return str(e)

    def job_take(self):
        job = self.scheduler.take_job()     #出队并置为 running
        if job is None:
            return "暂无待执行任务。"
        return (f"已取出定时任务 {job.id}(状态 running)。\n"
                f"执行内容:{job.content}\n"
                f"执行完成后请调用 job_update_status,把 {job.id} 置为 completed 或 failed。")

    def job_update_status(self, job_id, status):
        try:
            self.scheduler.update_job_status(job_id, status)
            return f"任务 {job_id} 状态已更新为 {status}"
        except ValueError as e:
            return str(e)

    