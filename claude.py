from datetime import datetime
import threading
from llm import LLM
from TOOLS import (Tools, create_default_registry, tool_filter, check_roles,
                   ROLE_MAIN, ROLE_SUBAGENT, ROLE_MEMBER, ROLE_DENIED, ROLE_HINT,
                   JOB_TOOLS)
import HOOKS
from PROMPT import build_system_prompt, check_prompts
from PROMPT import SUBAGENT_PROMPT
from PROMPT import OFFLINE_CONFIRM_PROMPT
from skill import  SkillLoader
from compact import CompactManager
from memory import MemoryManager
from task import TaskStore,create_task_handlers
from task_runner import TaskRunner, MAX_BACKGROUND_AGENTS
from corn_job import Scheduler
from message import Message, MessageBus
from mcp import MCPManager
from team import (TeamRuntime, KIND_CHAT, KIND_OFFLINE_REQ, KIND_OFFLINE_AGREE,
                  KIND_OFFLINE_REFUSE, ALIVE, EXITING, OFFLINE, DEAD,
                  is_chat, is_control)
import json
import time
import ui

#下线握手的等待上限。设计.md 里的握手本来没有上限:成员不回话,主agent 就永远停在 exiting
#(压测里成员线程崩掉后,主agent 就空等着,整整 13 分钟没人发现)。但"没回话"不等于"它死了",
#所以分两步走:到点先查状态,空闲就重发;再等一轮还没回话,才判定它失灵。
OFFLINE_ACK_TIMEOUT_SECONDS = 120   #每一轮等多久
OFFLINE_MAX_ROUNDS = 2              #总共给几轮:第一轮可以重发,第二轮结束就判定失灵(所以重发最多 1 次)
#请求文本要和重发的一模一样:成员侧靠 kind 触发、不看内容,但日志和排查要靠它可读、可对比
OFFLINE_REQ_TEXT = ("主agent判断当前不再需要你,请做最终确认:如果还有没做完的工作、"
                    "还在等别人回复,就拒绝下线;确认可以结束了就同意下线。")


#把秒数说成人话。"已跑 3 分 12 秒"比"已跑 192.4 秒"更容易让人一眼看出是不是异常 ——
#这个数字唯一的用处就是给"它卡住了没有"当参照,读起来费劲就等于没用
def _fmt_elapsed(seconds):
    total = int(seconds)
    if total < 60:
        return f"{total} 秒"
    if total < 3600:
        return f"{total // 60} 分 {total % 60} 秒"
    return f"{total // 3600} 小时 {(total % 3600) // 60} 分"


class ClaudeMini():

    def __init__(self,show_thinking=False,slient = True,role=ROLE_MAIN,task_store=None,scheduler=None,message_bus=None,agent_name="main",agent_id=None,team_runtime=None,mcp=None,task_runner=None,drains_results=None,deny_tools=None):

        self.skill_loader = SkillLoader()
        self.compact_mannager = CompactManager()
        self.memory_manager = MemoryManager()

        self.task_runner = task_runner if task_runner is not None else TaskRunner()

        #谁是这份邮箱的收件人。两种情况:
        #① 邮箱是自己建的(没传 task_runner)——自己就是唯一的读者,直接收。
        #   团队成员走这条:它有自己的 runner,run_forever 里照常收自己提交的后台任务。
        #② 邮箱是别人给的 —— 默认**不收**,必须显式说明。给的情况有两种:
        #   a. 共用的(子agent共享主agent那份):收了就会抢,绝不该收(见 _build_subagent);
        #   b. 专给一个人的(定时任务agent拿到的那份):它是唯一读者,该收 —— 传 True。
        #   两种从参数上看不出来,所以默认选"不收":忘了标只是少一条通知,标错了是通知跑到别人家去
        self.drains_results = (task_runner is None) if drains_results is None else drains_results

        #角色决定能调用的工具
        self.role = role
        #deny_tools 是调用方追加的禁用名单,叠在角色之上。
        #给的是**名字集合**不是角色(和 tool_filter 同一个约定):禁的理由不一定来自身份,
        #比如定时任务agent —— 身份是 ROLE_MAIN,但它不拥有一棵长命的树,团队工具对它没意义
        self.denied_tools = set(ROLE_DENIED[role]) | set(deny_tools or ())

        #注册表也提前到这里:下面要往里注册 MCP 工具,而 self.llm 又得先拿到 self.tools
        self.tool_registry = create_default_registry()

        #MCP:外部进程提供的工具,清单在同目录 mcp_servers.json。
        #和 Scheduler / TaskStore 一样,整个 agent 树**共享同一个实例** ——
        #每个 server 背后是一个子进程加一条读线程,这两样都复制不了(见 mcp.MCPManager)
        self.mcp = mcp if mcp is not None else MCPManager()

        #工具表的两个来源**先合并、再统一过滤一次**。顺序不能反:
        #只滤 Tools 的话,MCP 那批是后追加的,会整批绕过过滤。
        #Tools 是 TOOLS.py 里的模块级列表,所有 agent 共用同一个对象 ——
        #list() 出新列表,绝不能就地改它(以前 MCP 那批就地 += 污染了全局列表:
        #每建一个 agent 就再追加一遍,同一个工具名在 schema 里出现多次)
        schemas = list(Tools) + self._register_mcp_tools()
        self.tools = tool_filter(schemas, self.denied_tools)

        #运行时那道保险用的集合:模型看得见什么,就只允许调什么。
        #它和 self.tools 同源,所以防的是"绕过构造函数的调用路径",不是"两处配置漂移"
        self.allowed_tools = {t["name"] for t in self.tools}

        #角色表里写了不存在的工具名(拼错)只会静默失效:不报错,也不禁任何东西。
        #只在主agent上对一次 —— 它是启动时唯一确定会被建出来的实例,没必要每个成员都刷一遍
        if role == ROLE_MAIN:
            for problem in check_roles(t["name"] for t in schemas):
                ui.warn(f"[role] {problem}")

        # 加载长期记忆并注入 system prompt
        session_memory = self.memory_manager.load_session_memory()

        # (只在启动时注入一次;会话跨天后会过期,job_create 的工具描述里已提示可用 bash date 复核)
        _now = datetime.now()
        current_time = f"\n当前时间:{_now.strftime('%Y-%m-%d %H:%M')} 周{'一二三四五六日'[_now.weekday()]}"

        #★ 按角色取提示词 —— 成员和子agent**拿不到**主agent那套"怎么派活、怎么管定时任务"
        #的说明。以前是"所有人先拿主agent那份,成员再追加一段纠偏",模型读到的是完整的
        #主agent说明,补的那句压不住它(实测:成员照着「怎么组建团队」去做,还谎报
        #"已启动成员",见 9b37de0)。这里和 tool_filter(role) 认的是**同一个 role 值** ——
        #"给哪段提示词"和"给哪些工具"至此才真的合成一套
        head = build_system_prompt(role)
        if session_memory:
            head = head + "\n" + session_memory
        self.system_prompt = head + current_time + "\n" + "你可以使用以下skill解决相关问题:\n" + self.skill_loader.catalog()

        #提示词自检:每个角色看到的那份里点名的工具,必须在它自己的工具表里。
        #和 check_roles 一样只在主agent上跑一次 —— 但**三个角色都查**:主agent是启动时
        #唯一确定会被建出来的实例,成员/子agent要等真派活了才存在,那时候再发现就晚了。
        #(不查"拥有但没提"那个方向:工具描述自己讲得清怎么用,提示词没提不算错)
        if role == ROLE_MAIN:
            _universe = {t["name"] for t in schemas}
            for _r in (ROLE_MAIN, ROLE_MEMBER, ROLE_SUBAGENT):
                _owned = {t["name"] for t in tool_filter(schemas, ROLE_DENIED[_r])}
                for problem in check_prompts(_r, build_system_prompt(_r), _owned, _universe):
                    ui.warn(f"[prompt] {problem}")

        self.llm = LLM(self.system_prompt,self.tools)

        self.show_thinking = show_thinking   #是否展示思考
        self.slient = slient        #是否展示子agent消息

        # 任务系统:库文件落在当前目录 .task/tasks.json(跨会话保留);主/子agent共享同一 store
        self.task_store = task_store if task_store is not None else TaskStore(".task/tasks.json")

        # 定时任务:库文件落在当前目录 .task/jobs.json(跨会话保留)
        # 扫描线程与锁都不可复制,主/子agent必须共享同一个 scheduler 实例 ——
        # 否则会各起一条扫描线程、各持一把锁去写同一个文件,导致文件交错写坏
        self.scheduler = scheduler if scheduler is not None else Scheduler(".task/jobs.json")
        self.scheduler.start()          #重复调用是 no-op,共享实例时子agent不会起第二条线程

        # 消息总线:和 Scheduler / TaskStore / MCP 一样,整个 agent 树共享同一条 ——
        # 每个成员背后是一条线程,大家靠同一条总线互相寻址,各建各的就谁也找不到谁。
        # 默认自己建一条,调用方(main.py)不需要知道有这东西;
        # 成员由 team_spawn 注入主agent那一条,子agent**不**注入(见 run_subagent)
        self.message_bus = message_bus if message_bus is not None else MessageBus()
        self.agent_name = agent_name
        #通信身份是 ID,不是名字:名字可以重复(只用于展示/日志/模型理解),
        #ID 唯一且成员下线后不回收(设计.md 三)
        self.agent_id = agent_id or agent_name

        #成员管理归 TeamRuntime(由主agent持有),MessageBus 只管收发(设计.md 五)
        self.team_runtime = team_runtime
        if role == ROLE_MAIN and self.team_runtime is None:
            self.team_runtime = TeamRuntime(owner_id=self.agent_id)

        self.running = False        # run_forever 的生命周期开关(确认下线时才置 False)
        self.state = "idle"         # idle / work / exit,成员自己报的执行状态(仅供展示)
        self._team_history = []     # 团队对话历史(跨消息保留)
        self._notices = []          # 待送达模型的团队通知(成员上下线),存 (文本, 是否紧急)
        self._deferred_control = [] # run() 期间收到、按"完成当前执行边界再处理"推迟的控制消息
        self._offline_decision = None   # 成员侧的最终确认结果:(agree, reason)
        #下线握手看门狗(只有主agent用):member_id -> {"deadline": 单调时钟绝对时刻, "rounds_done": 轮次}
        #存绝对时刻而不是倒计时:检查来晚了(主agent正卡在一次长工具调用里)也不会把窗口越推越长
        self._offline_watch = {}

        #原生工具的处理函数:先集中成一张表,再按身份**一次性**注册。
        #以前这里是每个工具组一个 if/else,禁用时注册一个"守卫桩"返回提示语;
        #现在桩没了 —— 被禁的工具干脆不存在:模型看不到(不在 self.tools),
        #运行时也调不动(不在 registry)。两处用的是同一个 denied_tools,不可能对不上
        #(mcp.py 里那条不变量:注册了没进 schema、进了 schema 没注册,都是 bug)
        native_handlers = {
            "subagent": self.run_subagent,
            "bg_status": self.bg_status,
            "load_skill": self.skill_loader.load,
            "recall_memory": self.recall_memory,
            **self.memory_manager.create_handlers(),
            **create_task_handlers(self.task_store),
            **{name: getattr(self, name) for name in JOB_TOOLS},
            "send_message": self.send_message,
            "team_spawn": self.team_spawn,
            "team_list": self.team_list,
            "team_stop": self.team_stop,
            "team_offline_confirm": self.team_offline_confirm,
        }
        for name, handler in native_handlers.items():
            if name not in self.denied_tools:
                self.tool_registry.register(name, handler)


    #agent循环
    def run(self,history):

        #prompt过长重试次数
        reactive_retries = 0
        #跨轮保存最近一次有正文的回复,最后一轮无正文时兜底返回
        last_text = ''

        
        while True:
            #收集后台任务执行结果
            #收集后台任务执行结果。**只有收件人本人能领**,见 __init__ 的 drains_results
            results = self.task_runner.collect() if self.drains_results else {}
            notification = self.build_task_notifications(results)
            if notification:
                history.append(notification)
            #收团队成员发来的消息(主agent由用户输入驱动,不能阻塞等消息,只能主动收件)
            team_message = self.collect_team_messages()
            if team_message:
                history.append(team_message)
            #流式思考的活行。★ 只有主agent建得起:show_thinking 全项目只有
            #main.py 传了 True,而子agent/团队成员/定时任务都是默认的 False ——
            #所以进程里最多一条活行,不存在两个 agent 抢同一行的问题
            live = ui.thinking_live() if self.show_thinking else None
            #发送消息
            try:
                response = self.llm.send(
                    history,
                    on_thinking=(live.feed if live is not None else None),
                )
            except Exception as e:
                #★ 必须先把活行收掉。不然:① 错误信息会打在"思考中…"那条**没换行**的行上,
                #挤成一团;② 那一行永远等不到换行,后面所有输出都跟着乱
                if live is not None:
                    live.abort()
                text = str(e).lower()
                too_long = any(k in text for k in
                               ("prompt_too_long", "prompt is too long", "too many tokens"))
                if not too_long or reactive_retries >= 1:
                    raise
                ui.status("[reactive compact] prompt过长,压缩后重试")
                history[:] = self.compact_mannager.reactive_compact(history, self.llm)
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
                        #思考交给 ui 折叠:默认只出一行"思考 N 行",想看整段再开
                        #UI_VERBOSE=1。以前是把整段 thinking 直接糊上来,
                        #它经常比正文还长,一屏正文全被它顶出去了
                        #live 给过去 = 流式时那一行**已经在了**,就地换成这一行,
                        #不再另打一行(否则屏幕上会出现两条"思考 N 行")
                        ui.thinking(block.thinking, live=live)
                    else:
                        continue
                elif block.type == "text" :
                    if self.slient:
                        ui.assistant(block.text)
                    final_text += block.text

            #一个思考块都没出(简单问题直接答、纯工具调用)时,活行没人认领 ——
            #得把那一行让回给提示符。已定格的会自己 no-op,所以无条件调
            if live is not None:
                live.abort()

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
                #后台任务不在此处等待
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
            history[:] = self.compact_mannager.snip_compact(history)

            #模型总结上下文
            history[:] = self.compact_mannager.llm_compact(history,self.llm)

    #agent teams
    # 成员的生命周期:idle(真阻塞) → work → idle ... 直到主agent请求下线并完成最终确认
    def run_forever(self):

        #常驻循环是**团队成员**的存在方式:要有成员表才知道自己归谁管、跟谁协作。
        #总线不用查 —— 它现在由构造函数自己建,不可能是 None
        if self.team_runtime is None:
            raise RuntimeError("没有成员表,这个 agent 不是团队的一员,不能跑常驻循环")

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
            ui.debug(f"\n[{self.agent_id}] 收到来自 {msg.sender} 的消息")
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
            ui.debug(f"[{self.agent_id}] 下线,丢弃 {len(left)} 条来不及处理的消息")

    def send_message(self, to: str, content: str) -> str:
        """团队协作:给另一个成员(或主agent)发消息。用 ID 寻址(名字只在唯一时可用)。

        发之前先确认收件人属于当前团队 —— 不存在/已下线的成员直接拒绝,
        不能让消息留在总线上等着被"未来某个 agent"错误消费(设计.md 七)
        """
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
        #不属于任何团队(子agent)就没有团队消息可言:它有自己的空总线,别去 drain
        if self.team_runtime is None:
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
        if self.team_runtime is None:
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
        name = (name or "").strip()
        if not name:
            return "❌ 成员名字不能为空。"
        if not (prompt or "").strip():
            return "❌ 初始任务不能为空。"

        runtime = self.team_runtime
        #重名是允许的:身份是 ID,名字只用于展示和模型理解(设计.md 三·name)
        member = runtime.register(name=name, task=prompt)

        #成员:身份是 team_member,所以它天然没有"建团队/起子agent/写记忆/领定时任务"这些工具
        #(表在 TOOLS.ROLE_DENIED);共享总线、成员表、任务库、调度器、MCP
        teammate = ClaudeMini(role=ROLE_MEMBER, agent_name=name, agent_id=member.id,
                              message_bus=self.message_bus, team_runtime=runtime,
                              task_store=self.task_store, scheduler=self.scheduler,
                              mcp=self.mcp)
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
        runtime = self.team_runtime
        if runtime is None:
            return "❌ 当前没有成员表,没有团队可管。"
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
        self._offline_decision = (bool(agree), (reason or "").strip())
        if agree:
            return "✅ 已确认下线:你的线程会在这一轮结束后停止,之后不再收到消息。"
        return f"✅ 已回复主agent:暂不下线。理由:{reason or '(未说明)'}。你可以继续当前工作。"

    #===== 生命周期控制面(设计.md 九)=====
    #方向固定:主agent发起下线意图 → 成员最终确认 → 成员实际停止 → 主agent更新状态。
    #这些消息不经过 LLM;只有"这份工作是否还需要我"这种业务判断才问模型(设计.md 十)

    def process_control_messages(self) -> int:
        """只处理控制面消息,不叫模型 —— 用户没输入时,主循环靠它推进下线握手"""
        if self.team_runtime is None:
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
            ui.debug(f"[{self.agent_id}] 收到未知控制消息 {msg.kind},忽略")

    #----- 成员侧:收到下线请求 → 最终确认 -----

    def _confirm_offline(self, msg):
        """成员收到主agent的下线请求,做最后一次确认(设计.md 九)。

        不能无条件立即退出:先看确定性事实(还有没有待处理的活),再让模型做业务判断。
        """
        runtime = self.team_runtime
        if runtime is not None and runtime.is_owner(self.agent_id):
            ui.debug(f"[{self.agent_id}] 忽略发给主agent的下线请求")
            return
        #已经确认过下线、正在收尾(self.running 由 _reply_offline 置 False):
        #不要把第二份请求(主agent的重发)当成新活再干一轮
        if not self.running:
            ui.debug(f"[{self.agent_id}] 已在退出流程中,忽略重复的下线请求")
            return

        ui.status(f"\n[{self.agent_id}] 主agent请求下线,做最终确认")
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
            ui.status(f"[{self.agent_id}] 已确认下线,线程退出")
        else:
            self.state = "idle"
            ui.status(f"[{self.agent_id}] 拒绝下线:{reason}")

    #----- 主agent侧:收到成员的最终确认 -----

    def _on_offline_reply(self, msg, agreed: bool):
        """成员的最终确认。逻辑状态要等它真的停下来才置为 offline(设计.md 三/四)"""
        runtime = self.team_runtime
        if runtime is None or not runtime.is_owner(self.agent_id):
            ui.debug(f"[{self.agent_id}] 忽略不属于自己团队的下线确认")
            return
        member = runtime.get(msg.sender)
        if member is None:
            ui.debug(f"[{self.agent_id}] 下线确认来自未知成员 {msg.sender},忽略")
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
        ui.status(f"[team] {text}")
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
                ui.warn(f"[timeout] {member.id} 等满 {OFFLINE_ACK_TIMEOUT_SECONDS} 秒没回应下线请求,"
                      f"而它是空闲的,已重发一次")
            else:
                #它在干活(很可能正是在做下线确认):重发会让它重复干活,只续期
                ui.warn(f"[timeout] {member.id} 等满 {OFFLINE_ACK_TIMEOUT_SECONDS} 秒没回应下线请求,"
                      f"但它正在 {member.self_state},不重发(免得重复干活),再等一轮")

    def _give_up_on_member(self, member):
        """两轮都等不到回信:判定这成员失灵,标 dead 并让模型知道。

        它的线程可能还活着(Python 杀不掉线程)—— 这是有意的:dead 在这里的意思是
        "这个成员不再可信、不再被管理"。所以文案要说清:它没做完的活没人接手;
        万一它只是慢,稍后补上回信,状态会被自动纠正。
        """
        self.team_runtime.update(member.id, status=DEAD, note="下线请求超时无回应")
        left = self.message_bus.drain(member.id)
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
        for msg in self.message_bus.drain(self.agent_id, is_control):
            self._handle_control(msg)

        for member in runtime.reconcile():
            left = self.message_bus.drain(member.id)
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
                ui.debug(f"[{self.agent_id}] 已在退出流程中,丢弃 {len(deferred)} 条推迟的控制消息")
                return
            self._handle_control(msg)

    #子agent的实例工厂:前台(run_subagent)和后台(run_subagent_background)共用一份。
    #抽出来是因为下面那段注释是**承重的** —— 复制两份迟早会漂移,而这个类最怕的
    #就是"某条路径悄悄多注入了一个共享服务"
    def _build_subagent(self, agent_name="main"):
        #子agent:身份是 subagent,天然没有"起子agent/写记忆/recall_memory/定时任务/团队"这些工具。
        #**不注入总线** —— 它是一次性的、没有信箱:真让它共享总线,它 run() 里的
        #collect_team_messages 就会去 drain 主agent的收件箱,把主人的消息抢走。
        #反过来说,task_runner **必须**共享:它是**结果邮箱**,谁自建一个,谁提交的后台任务
        #跑完就落进没人读的邮箱(主agent提交后台任务时也共享,理由一样)。
        #邮箱共享了,领取者就唯一 —— 由 drains_results 挡住,子agent 不领(见 __init__)。
        #scheduler 同样必须共享同一实例(锁与扫描线程不可复制)
        return ClaudeMini(slient=False, role=ROLE_SUBAGENT,
                          task_store=self.task_store,
                          scheduler=self.scheduler,
                          task_runner=self.task_runner,
                          drains_results=False,
                          mcp=self.mcp,
                          agent_name=agent_name)

    #子agent(前台):**阻塞**在调用线程上跑,跑完当场把结果当工具结果返回
    #
    #**_ignored 必须留着:schema 里还有 is_background,模型把它显式写成 false 时
    #它会一起出现在 block.input 里,一路传到 handler(**input) —— 签名里不接住就是
    #TypeError(报错还会说"参数有误",把模型带偏)。true 的那条在 excute_tool 的后台
    #分支就分流掉了,根本走不到这。run_bash 用的是同一个写法
    def run_subagent(self, prompt: str, **_ignored):
        """启动一个独立子agent执行指定任务"""
        #只有主agent能起子agent。别的角色在工具层已经拦住了(看不到 subagent),
        #这里必须再拦一道,因为 recall_memory 会**直接内部调用**本方法 ——
        #那条路径不经过 excute_tool,也就没有运行时的那道检查
        if self.role != ROLE_MAIN:
            return "❌ 只有主agent能起子agent。自己做,或把需求带回父agent。"

        HOOKS.trigger_hooks("BefSubAgent",prompt)

        #子agent的 agent_name 保持默认 "main"(和以前一样)
        subagent = self._build_subagent()

        history = [
            {
                "role": "user",
                "content": SUBAGENT_PROMPT + "\n" + prompt
            }
        ]

        result = subagent.run(history)

        HOOKS.trigger_hooks("AftSubAgent",result)

        return result

    #子agent(后台):建一个全新实例丢给 TaskRunner,立刻返回 task_id
    #
    #和 run_subagent 的关系:共用"子agent是什么"(工厂、SUBAGENT_PROMPT、注入哪些共享服务),
    #不同的是**怎么跑** —— 执行体交给 TaskRunner 在自己的线程里跑,调用方(主agent这一轮)
    #立刻拿到一句"已提交"就继续干活,结果跑完由 <task_notification> 送回主对话
    def run_subagent_background(self, prompt: str):
        """起一个后台子agent执行任务:立刻返回,结果稍后由 <task_notification> 送回"""
        #和 run_subagent 同样的理由:角色检查必须在方法里再拦一道,
        #因为 recall_memory 会绕过 excute_tool 直接调进来
        if self.role != ROLE_MAIN:
            return "❌ 只有主agent能起子agent。自己做,或把需求带回父agent。"

        #work 是个闭包,在 TaskRunner 的线程里执行 —— 捕获的 self 是主agent,
        #只用到它的共享服务(task_store/scheduler/task_runner/mcp),不碰它的历史
        task_id = self.task_runner.submit_agent(
            lambda tid: self._run_background_subagent(tid, prompt))

        if task_id is None:
            #满了必须**说出来**。静默丢弃的话模型以为已经跑上了,永远不会去补那个任务
            return (f"❌ 后台子agent 已达并发上限({MAX_BACKGROUND_AGENTS} 个在跑),这条没有提交。"
                    f"等前面的跑完再试,或者改用前台 subagent 直接做。")

        return f"🔄 子agent {task_id} 已在后台执行,跑完我会收到结果。"

    #后台子agent的执行体 —— **跑在 TaskRunner 的线程里,不是主线程**。
    #两件事因此而不一样,都不是 bug:
    #① 问不了人:permission_hook 靠"是不是主线程"判断能否弹窗问用户(HOOKS.py:45),
    #   这条线程上所有 ASK 类操作(写 memory/、覆盖已有文件…)一律被拒。这是决策:
    #   后台任务没人守着屏幕,不该挂在那里等一个可能已经走开的用户。
    #② 打印会和用户输入交错(hook 的 banner 是从这条线程打出去的)。
    #   子agent本体是 slient=False,不会刷模型的正文,只有 banner 会串行;
    #   省掉 hook 是更坏的选择 —— 那等于这个子agent在观测面上根本不存在。
    def _run_background_subagent(self, task_id, prompt):
        HOOKS.trigger_hooks("BefSubAgent", prompt)

        #agent_name 用 task_id:进程里可能同时有好几个后台子agent,名字是事后对账用的
        #(banner、异常栈、提交时回给模型的那句 "子agent bg_0002 已在后台执行" 指的是同一个)
        subagent = self._build_subagent(agent_name=task_id)

        history = [
            {
                "role": "user",
                "content": SUBAGENT_PROMPT + "\n" + prompt
            }
        ]

        result = subagent.run(history)

        HOOKS.trigger_hooks("AftSubAgent", result)

        return result

    #后台任务查询(工具 bg_status):让模型能自己问一句「我起的那些后台任务怎么样了」。
    #
    #它补的是看门狗够不着的那一半。看门狗只认"线程没了"(_finalize 那条路压根没走到),
    #而"线程还活着、但一直不返回"在进程内**没有任何办法自动发现**:Python 杀不掉线程,
    #也就没人能替它下结论(LLM 调用没超时,一次网络卡顿就能让一个后台 agent 僵住十分钟)。
    #能做的就是把可观测的东西如实报出来,让模型自己判断要不要放弃它
    def bg_status(self):
        #先补账、再报数。顺序不能反:先报数的话,一条线程早就没了的任务会被报成
        #"正在跑",而下一秒主循环又收到它的失败通知 —— 同一件事两个说法。
        #反过来先 reap,报出来的就一定是"已经发生过的事",和通知对得上
        reaped = self.task_runner.reap_dead()
        snap = self.task_runner.snapshot()

        running = snap["running"]
        used, cap = snap["agents_running"], snap["max_agents"]

        lines = []
        if reaped:
            lines.append(f"⚠️ 有 {len(reaped)} 条后台任务的线程已经终止、没留下结果,"
                         f"已按失败记录,稍后会收到 <task_notification>:{'、'.join(reaped)}")
        if not running:
            #空了也要报,而且要把名额一起报:模型问这个常常是为了"还能不能再交一个",
            #而"没有在跑的任务"本身已经回答了它
            lines.append(f"当前没有正在跑的后台任务。后台子agent 还能再交 {cap} 个。")
        else:
            #名额只卡 agent,bash 不受它限制 —— 所以要按 kind 分开说。
            #含混地说"还能再交 N 个",模型会以为那是总数,于是不敢交 bash(或反过来,
            #以为交 bash 也会被拒而白改方案)
            lines.append(f"后台子agent {used}/{cap} 在跑(还能再交 {max(cap - used, 0)} 个;"
                         f"后台 bash 命令不受这个上限限制):")
            #按已跑时长从长到短:最可能卡住的那条排在最上面,不用往下找
            for t in sorted(running, key=lambda t: -t["elapsed"]):
                label = "子agent" if t["kind"] == "agent" else "bash命令"
                lines.append(f"- {t['task_id']} {label} 已跑 {_fmt_elapsed(t['elapsed'])}")
        return "\n".join(lines)

    #记忆召回
    def recall_memory(self, query: str = None) -> str:
        """通过子 agent 召回相关记忆"""
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
        """工具调用的唯一入口,顺带负责让它在屏幕上**看得见**。

        以前工具跑起来是全黑的:下面那句工具输出的 print 是注释掉的,模型调了什么、
        跑到第几个、回来了没有,屏幕上一点动静都没有,只能干等。

        包成一层外壳,而不是在下面每个 return 前各插一句:这里面有六个出口
        (角色不允许 / 被 hook 拦 / 后台 / 未知工具 / 参数错 / 正常返回),
        漏掉任何一个,那个出口在屏幕上就永远只有半句话 —— 报"开始调"却永远等不到"回来"
        是最难受的状态,因为它看起来像卡住了
        """
        if block.type != "tool_use":
            raise ValueError(f"Invalid block type: {block.type}. Expected 'tool_use'.")

        who = self._who()
        #brief 只给屏幕看(一个代表参数,content 还截到 40 字);full 是完整 input,
        #登记进折叠处供 /展开 调出来 —— 这两件事要的东西不一样,所以分开传
        ui.tool_call(block.name, self._args_brief(block), who=who,
                     full=json.dumps(block.input, ensure_ascii=False, indent=2)
                     if isinstance(block.input, dict) else block.input)

        t0 = time.perf_counter()
        output = self._excute_tool(block)
        ui.tool_result(output, ms=int((time.perf_counter() - t0) * 1000), who=who)
        return output

    def _who(self):
        """工具行前面那个身份标签。主agent不标 —— 它就是默认那个。

        **不能直接拿 agent_id 当标签**:前台子agent 的 agent_name 是默认的 "main"
        (见 _build_subagent),那是**原有约定**,不是能给外人看的名字。直接用它,
        一个子agent就会顶着自己主agent的名字干活 —— 标错人比不标更糟,
        等于把"这行是谁打的"这个唯一目的搞反了。

        所以分三种:
            主agent          None      —— 不需要
            子agent          "子"      —— 它的任务在"🤖 子agent 开始"那行里,不在这重复
            团队成员/后台任务  自己的 id  —— 这些**真的**能区分,不标就分不清

        后台子agent(_build_subagent(agent_name=task_id))id 是有意义的,bg_0002 这种,
        所以下面认的是"名字还是不是那个默认哨兵",不是"是不是子agent"
        """
        if self.role == ROLE_MAIN:
            return None
        if self.role == ROLE_SUBAGENT and self.agent_name == "main":
            return "子"
        return self.agent_id

    def _args_brief(self, block):
        """把工具参数压成给屏幕看的那一行。

        取一个**代表性的参数**,而不是把整个 input 打出来:bash 要看的是 command、
        write_file 是 path;整字典打出来会带上 is_background 这类噪音,把真正的参数挤没
        """
        data = block.input
        if not isinstance(data, dict):
            return ui.one_line(data)
        for key in ("command", "path", "prompt", "query", "pattern", "content"):
            if key in data:
                #content 通常是整段文件正文,不截断会把这一行撑爆
                return ui.one_line(data[key], 40 if key == "content" else None)
        if not data:
            return ""
        key = next(iter(data))      #都不匹配:报第一个,总比不报强
        return ui.one_line(f"{key}={data[key]}")

    def _excute_tool(self, block):
        #运行时那道保险:看不见的工具模型不会去调,所以正常路径下不会命中。
        #但幻觉出来的名字、压缩后重放的历史、以后新增的调用路径都可能绕过来,调之前再对一次。
        if block.name not in self.allowed_tools:
            return f"❌ {block.name} 对「{self.role}」不可用。{ROLE_HINT[self.role]}"

        #PreToolUse 必须在**下面每一条分支之前**过一遍。原来"后台 bash"那条分支在它上面
        #直接 return,等于后台命令完全不做权限检查 —— 加个 is_background=True,
        #"rm -rf /" 就把 DENY 表绕过去了(task_runner.py 自己也不调 hook,它拿到 command
        #就直接 execute_bash)。
        #也**不能**改成"挪进 TaskRunner 的线程里再过":permission_hook 是靠
        #"是不是主线程"来决定能不能弹窗问用户的(HOOKS.py:45),在后台线程里问,
        #主agent自己那条正常命令也会被当成"后台线程弹不了窗"直接拒掉。
        #结论:权限判定属于**发起调用的线程**,所以必须在这里做。
        hook_result = HOOKS.trigger_hooks("PreToolUse", block)
        if hook_result is not None:
            ui.debug(f"Hook result: {hook_result}")
            return hook_result

        #后台执行:同一个"后台"策略,按工具选执行体。
        #schema 里长着 is_background 的工具才可能走到这里(bash / subagent),
        #所以这里的名字判断是**白名单**,不是"谁都能后台跑"
        if block.input.get("is_background"):
            if block.name == "bash":
                task_id = self.task_runner.submit_bash(block.input.get("command"))
                return f"🔄 工具 {task_id} 已在后台执行。"
            if block.name == "subagent":
                return self.run_subagent_background(block.input.get("prompt"))

        #获取工具处理函数
        handler = self.tool_registry.get(block.name)
        if handler is None:
            return f"❌ Unknown tool: {block.name}"

        return self._call_tool(handler, block)

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

    def _register_mcp_tools(self):
        """注册 MCP 工具,并返回它们的 schema(要交给 LLM)。放在 __init__ 里被调一次。

        两件事缺一不可:注册处理函数(调得动)+ schema 进 self.tools(模型看得见)。
        只做前一半,模型不知道有这个工具;只做后一半,模型一调就是 Unknown tool。

        哪些工具最终留得下(和原生工具撞名的会被剔掉)由 MCPManager.collect 统一裁决,
        所以注册和 schema 天然是同一个集合 —— 不会出现"注册了却没说"或"说了却调不动"。

        reserved 传的是**完整**的原生工具名,而不是本角色过滤后的:
        防撞名是安全措施,不该因为某个角色看不见某个工具,就给 MCP 留出顶替它名字的机会。
        """
        schemas, handlers = self.mcp.collect(reserved={t["name"] for t in Tools})
        for name, handler in handlers.items():
            if name in self.denied_tools:
                continue        #角色表点名禁掉的 MCP 工具,连 handler 也不注册
            self.tool_registry.register(name, handler)
        return schemas

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
    #kind/status 都要报:模型看到"子agent 已失败"才会去补救,只报"已完成"它会当成成功
    def build_task_notifications(self, results):
        if not results:
            return None

        KIND_LABEL = {"bash": "bash命令", "agent": "子agent"}
        STATUS_LABEL = {"completed": "已完成", "failed": "已失败"}

        notifications = []

        for task_id, record in results.items():
            kind = KIND_LABEL.get(record["kind"], record["kind"])
            status = STATUS_LABEL.get(record["status"], record["status"])
            notifications.append({
                "type": "text",
                "text": (
                    f"<task_notification>\n"
                    f"后台{kind} {task_id} {status}。\n"
                    f"输出：\n{record['output']}\n"
                    f"</task_notification>"
                )
            })

        return {
            "role": "user",
            "content": notifications
        }

    #===== 定时任务工具 handler =====

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

    #注:这里**没有** job_take / job_update_status。
    #到点的任务由派发器(dispatcher.JobDispatcher)自动取走并在独立的后台 agent 里跑,
    #跑完由派发器把结果汇报回 Scheduler —— 全程不需要模型参与。
    #这两个工具在"模型自己领任务"的旧设计里是必需的,现在不但多余,还有害:
    #模型一旦领走一条就会把它置成 running,而那条任务正在后台跑,状态被搅乱。
    #详见 PROMPT.py 的「定时任务」一节

    