from pathlib import Path
import skill
from bash_exec import execute_bash

Tools = [
    {
        "name":"calculator",
        "description":"A simple calculator that can perform basic arithmetic operations.",
        "input_schema":{
            "type":"object",
            "properties":{
                "expression":{
                    "type":"string",
                    "description":"The arithmetic expression to evaluate."
                }
            },
            "required":["expression"],
        },
    },
    {
        "name":"bash",
        "description":(
            "在 Windows 上通过 Git Bash 执行 POSIX shell 命令。"
            "支持常见 bash 语法: ls / mkdir -p / cat / echo、管道、重定向、python -c '...' 等。"
            "不要使用 cmd.exe 特有语法(如 dir、del、^ 转义)。"
        ),
        "input_schema":{
            "type":"object",
            "properties":{
                "command":{
                    "type":"string",
                    "description":"Execute shell commands."
                },
                "is_background":{
                    "type":"boolean",
                    "description": 
                        "是否在后台执行。"
                        "对于预计执行时间较长、会阻塞Agent继续工作的命令,设置为true。"
                        "后台任务启动后会立即返回任务ID,Agent无需等待命令完成。"
                        "后台任务执行完成后,系统会把结果以 <task_notification> 自动推送给Agent,"
                        "Agent 不需要自行 sleep 或轮询等待。"
                        "短时间命令或需要立即获取输出的命令保持false。"
                }
            },
            "required":["command"],
        },
    },
    {
        "name":"read_file",
        "description":"用来读取文件.",
        "input_schema":{
            "type":"object",
            "properties":{
                "path":{
                    "type":"string",
                    "description":"The path to the file to read."
                },
                "limit":{
                    "type":"integer",
                    "description":"The maximum number of lines to read."
                }
            },
            "required":["path"],
        },
    },
    {
        "name":"write_file",
        "description":(
            "把内容写进文件,整个文件替换。UTF-8 编码,不留 BOM。"
            "父目录不存在会自动创建 —— out/xxx.md 这种路径可以直接写,不必先 mkdir。"
            "文件已存在会被直接覆盖,所以覆盖前先 read_file 看一眼,别把别人的内容冲掉;"
            "要改文件的一小部分,也是读出来改好再整个写回。"
        ),
        "input_schema":{
            "type":"object",
            "properties":{
                "path":{
                    "type":"string",
                    "description":"The path of the file to write."
                },
                "content":{
                    "type":"string",
                    "description":"The full content to write to the file."
                }
            },
            "required":["path","content"],
        },
    },
    {
        "name": "task_create",
        "description": (
            "创建一个任务节点并返回其id(task_开头)。"
            "多步骤任务请逐条创建;步骤有先后依赖时,"
            "用 depends_on 指定前置任务id(被依赖的任务需先完成,本任务才能被认领执行)。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": { "type": "string", "description": "任务名(简短标题)" },
                "description": { "type": "string", "description": "任务要做什么(可空)" },
                "depends_on": {
                    "type": "array",
                    "items": { "type": "string" },
                    "description": "依赖的任务id列表(须先完成,本任务才能执行)"
                }
            },
            "required": ["subject"]
        }
    },
    {
        "name": "task_list",
        "description": (
            "列出任务与状态,并标注每个任务的阻塞依赖。"
            "可加 status 只筛某种状态,便于规划下一步该认领哪个任务。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                    "description": "按状态过滤:待认领/进行中/已完成"
                }
            }
        }
    },
    {
        "name": "task_get",
        "description": "查看单个任务的完整信息(含依赖与阻塞)。",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": { "type": "string", "description": "任务id(task_开头)" }
            },
            "required": ["id"]
        }
    },
    {
        "name": "task_claim",
        "description": (
            "认领一个任务进入执行(in_progress)。"
            "仅 pending 任务可认领;若有依赖未完成会被拒绝并说明阻塞。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": { "type": "string", "description": "任务id" },
                "owner": { "type": "string", "description": "认领者标识(缺省 assistant)" }
            },
            "required": ["id"]
        }
    },
    {
        "name": "task_complete",
        "description": (
            "将已认领的任务标记为已完成。"
            "仅 in_progress 任务、且由认领者本人(actor 与认领时的 owner 一致)可完成。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": { "type": "string", "description": "任务id" },
                "actor": { "type": "string", "description": "完成者标识(缺省 assistant,须==认领者)" }
            },
            "required": ["id"]
        }
    },
    {
        "name": "job_create",
        "description": (
            "创建一个定时任务,到点后系统会自动唤醒你去执行它。"
            "触发方式二选一,必须给且只能给一个:"
            "① schedule —— 周期性触发,类 linux cron 五段式「分 时 日 月 周」。"
            "用户说「每天9点…」「每小时…」「每周五…」时用它:"
            "'0 9 * * *' 每天 9:00、'*/15 * * * *' 每 15 分钟、'30 18 * * 5' 每周五 18:30。"
            "每段支持 *、a、a-b、*/n 和逗号列表(如 '1,15,30');周日写作 0 或 7。"
            "② once_at —— 只触发一次,给具体时刻 'YYYY-MM-DD HH:MM'。"
            "用户说「明天下午3点提醒我」「下周一早上叫我」这类一次性需求时用它。"
            "必须是将来时刻,且要把日期算准:当前时间在系统提示词里,"
            "若本次会话已开了很久(跨天)可能已过期,可用 bash 执行 date +'%Y-%m-%d %H:%M' 复核。"
            "两种任务跑完一轮后都不需要你做任何事:周期任务下次到点会自动再派发,"
            "一次性任务则就此结束,你不要为了「让它继续跑」而重复创建任务。"
            "content 要写清楚「做什么」——到点后是照着这句话执行,不依赖当前对话上下文。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": { "type": "string", "description": "任务要做什么(自然语言,到点后按此执行)" },
                "schedule": { "type": "string", "description": "周期触发:cron 五段式「分 时 日 月 周」(与 once_at 二选一)" },
                "once_at": { "type": "string", "description": "一次性触发的时刻 'YYYY-MM-DD HH:MM'(与 schedule 二选一)" }
            },
            "required": ["content"]
        }
    },
    {
        "name": "job_list",
        "description": (
            "列出所有定时任务(状态、id、触发方式、执行内容、上次派发时间)。"
            "触发方式会标明「周期」(cron,跑完还会再来)还是「一次性」(到点跑完就结束),"
            "回答用户「这个任务还会不会再跑」时以此为准。"
            "用户问「我设了哪些定时任务」时调用;要取消或删除某个任务前,"
            "也先用它拿到对应的 job_id —— 不要凭记忆猜 id。"
        ),
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "job_cancel",
        "description": (
            "取消一个定时任务:它不再被调度执行,但记录保留在任务库里(状态变为 cancelled),"
            "之后可以随时用 job_resume 恢复。适合「先别跑了,但我想留着」的场景。"
            "执行中(running)的任务取消不了(已经领走在跑,拦不住),那种情况用 job_delete。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": { "type": "string", "description": "任务id(job_开头),用 job_list 查" }
            },
            "required": ["job_id"]
        }
    },
    {
        "name": "job_resume",
        "description": (
            "恢复一个被取消的定时任务:重新放回调度池,下次到点照常执行。"
            "只对已取消(cancelled)的任务有效 —— 其他状态的任务本来就在调度里或正在跑。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": { "type": "string", "description": "任务id(job_开头),用 job_list 查" }
            },
            "required": ["job_id"]
        }
    },
    {
        "name": "job_delete",
        "description": (
            "删除一个定时任务:任务连同记录一起从任务库移除,不可恢复、没有后悔余地。"
            "适合「这个任务我不要了」,或清理卡在 running 的僵尸任务。"
            "只是暂时不想让它跑、以后还想再开,请用 job_cancel(可恢复);"
            "用户说要「删掉」时如果拿不准是不是想留着,先问一句。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": { "type": "string", "description": "任务id(job_开头),用 job_list 查" }
            },
            "required": ["job_id"]
        }
    },
    {
        "name": "job_take",
        "description": (
            "从定时任务队列取出一个已到执行时间的任务,取走后该任务状态变为 running。"
            "队列为空时返回「暂无待执行任务」。"
            "取出后请立即按 content 描述的内容执行,"
            "并根据执行结果调用 job_update_status 汇报,不要一直占着不汇报。"
        ),
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "job_update_status",
        "description": (
            "汇报定时任务的执行结果并修改其状态。"
            "任务执行完必须调用本工具,否则该任务会一直停留在 running,之后不再被调度。"
            "只接受 running → completed(执行成功)/ running → failed(执行失败)。"
            "注意:这两个状态说的是「这一轮」跑完了,不是「这个任务结束了」——"
            "周期任务下次到点会自动重新派发,一次性任务跑完才会就此结束,"
            "两种情况都不需要你重新创建任务。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": { "type": "string", "description": "任务id(job_开头)" },
                "status": {
                    "type": "string",
                    "enum": ["completed", "failed"],
                    "description": "执行结果:成功 completed / 失败 failed"
                }
            },
            "required": ["job_id", "status"]
        }
    },
    {
        "name":"subagent",
        "description":"启动一个独立子agent执行指定任务。",
        "input_schema":{
            "type":"object",
            "properties":{
                "prompt":{
                    "type":"string",
                    "description":"描述需要子agent完成的具体任务，包括目标和期望结果"
                }
            },
            "required":["prompt"]
        }
    },
    {
        "name":"load_skill",
        "description":"用来加载指定skill",
        "input_schema":{
            "type":"object",
            "properties":{
                "name":{
                    "type":"string",
                    "description":"需要加载的skill名称"
                },
            },
            "required":["name"]
        }
    },
    {
        "name": "write_memory",
        "description": (
            "将重要信息写入长期记忆存储。适用场景："
            "- 用户明确表达的偏好或习惯"
            "- 解决了复杂问题学到了新方法"
            "- 项目有重要的技术决策"
            "- 需要跨会话保留的信息"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": { "type": "string", "description": "记忆标题" },
                "content": { "type": "string", "description": "记忆内容" },
                "tags": { "type": "array", "items": {"type": "string"}, "description": "标签列表" }
            },
            "required": ["title", "content"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "搜索并召回相关的经验记忆。使用场景："
            "- 当用户询问「之前记住什么」「搜索记忆」「召回记忆」时调用"
            "- 当用户提到某个话题，想起了之前保存的相关记忆时调用"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": { "type": "string", "description": "搜索查询关键词" }
            },
            "required": ["query"]
        }
    },
    {
        "name": "send_message",
        "description": (
            "给团队中的另一个agent(或主agent)发消息。"
            "to 填对方的 ID(形如 alice-7f3a,主agent是 main);"
            "名字只在团队里唯一时也能用,重名必须用 ID —— 记不准先用 team_list 查。"
            "对方空闲时会立即被唤醒并处理这条消息;"
            "对方处理完若需要继续对话,会再给你发消息。"
            "content 要自包含:把需要对方知道的信息都写进去,不要依赖对方看不到的上下文。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "接收者agent的ID(如 alice-7f3a;主agent是 main)"},
                "content": {"type": "string", "description": "消息内容,需自包含"}
            },
            "required": ["to", "content"]
        }
    },
    {
        "name": "team_spawn",
        "description": (
            "启动一个常驻团队成员(独立agent + 独立线程),prompt 是它的初始任务。"
            "返回该成员的 ID,之后一律用这个 ID 给它发消息(名字可能重名,ID 才是身份)。"
            "成员启动后一直活着:没事时idle阻塞等待,收到消息才工作 —— "
            "这一点和一次性的 subagent 不同(成员可以反复派活、来回沟通)。"
            "成员干完活不会自己退出,需要它下线时用 team_stop 请它下线。"
            "name 自己起(英文小写,如 'alice'),允许与已有成员重名(但要用ID区分)。"
            "派发是异步的:本工具立即返回,成员完成后会发消息给你。"
            "注意:有明确结果、一次就能干完的独立子任务用 subagent 更省,不要为此建团队。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "成员名字(英文小写,如 alice;仅用于展示)"},
                "prompt": {"type": "string", "description": "初始任务,需自包含(成员看不到你和用户的对话)"}
            },
            "required": ["name", "prompt"]
        }
    },
    {
        "name": "team_list",
        "description": (
            "列出团队成员的 ID、名字、逻辑状态与执行状态。"
            "逻辑状态:alive 在岗 / exiting 已请求下线、等它确认 / offline 已下线 / dead 线程异常终止;"
            "执行状态是成员自己报的(idle 空闲等待 / work 工作中)。"
            "给成员发消息、或想确认下线结果时用它。成员只能查看,不能修改团队成员。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "team_stop",
        "description": (
            "请一个团队成员下线(只是发出请求,不是直接杀死它)。"
            "member 填该成员的 ID(唯一的名字也可以)。"
            "成员收到后要做最终确认:手头还有活、还在等别人回复就拒绝下线并说明理由;"
            "确认可以结束了才同意下线,它的线程随后停止。"
            "所以调用后它的状态先是 exiting,等它确认并真的停下来才变 offline —— "
            "不要据此宣布「它已经退出了」,可以用 team_list 或等它的回信确认。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "member": {"type": "string", "description": "成员ID(如 alice-7f3a;团队内唯一时也可用名字)"}
            },
            "required": ["member"]
        }
    },
    {
        "name": "team_offline_confirm",
        "description": (
            "【仅团队成员可用】对主agent的下线请求做最终确认。"
            "主agent请求你下线时,你必须判断自己是否还有必须完成的工作、还在等别人回复、"
            "还有没处理的消息:还有就 agree=false 拒绝下线,确实可以结束了才 agree=true。"
            "agree=true 后你的线程会在本轮结束后停止,不能再继续工作;"
            "不做确认就无法结束,主agent会一直等你的回复。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agree": {"type": "boolean", "description": "true=同意下线并停止;false=拒绝,继续留在团队"},
                "reason": {"type": "string", "description": "理由(拒绝时必须说明还有什么没做完)"}
            },
            "required": ["agree"]
        }
    },
]

#==================== 工具权限(角色) ====================
#三层分工,别混:
#  角色(这里)        —— 这个身份**拥有**哪些工具,构造时算一次
#  tool_filter        —— 拥有的工具里,哪些真的进 self.tools(模型看得见)
#  HOOKS/PERMISSIONS  —— 已拥有的工具,这次调用的**参数**放不放行(和身份正交)
#
#用**黑名单**:工具多、要禁的少,而且"禁"和原来那批 allow_* 开关是同一个语义方向
#(allow_subagent=False 就是"禁 subagent"),属连续演化,不是语义反转。
ROLE_MAIN = "main"
ROLE_SUBAGENT = "subagent"
ROLE_MEMBER = "team_member"

#定时任务这 7 个:统一由主agent管。扫描线程和锁都不可复制,子agent领了会重复执行同一件事
JOB_TOOLS = frozenset({"job_create", "job_list", "job_cancel", "job_resume",
                       "job_delete", "job_take", "job_update_status"})

#团队这 5 个:要有消息总线、要有一张成员表才有意义。
#子agent是一次性的、没有信箱,整组禁掉
TEAM_TOOLS = frozenset({"send_message", "team_spawn", "team_list",
                        "team_stop", "team_offline_confirm"})

ROLE_DENIED = {
    #主agent拥有全部工具,只有一个本来就不该给它:
    #下线确认是**成员**回答主agent用的。以前它只是没注册 handler,schema 却照样发给模型 ——
    #模型看得见、一调就是 Unknown tool,正是这次要消灭的那种东西
    ROLE_MAIN: frozenset({"team_offline_confirm"}),

    #子agent:禁再起子agent、禁写长期记忆、禁 recall_memory(防递归、防污染共享记忆库),
    #禁定时任务,禁团队(它没有信箱,发出去也没人收)
    ROLE_SUBAGENT: frozenset({"subagent", "recall_memory", "write_memory"}) | JOB_TOOLS | TEAM_TOOLS,

    #成员:和子agent一样禁那几类,团队工具里只禁"写"的两个 ——
    #发消息、看成员表、做下线确认都是它该有的(成员表的写权限只属于主agent,设计.md 十一)
    ROLE_MEMBER: frozenset({"subagent", "recall_memory", "write_memory",
                            "team_spawn", "team_stop"}) | JOB_TOOLS,
}

#工具被拒时给模型的一句话说明。按**身份**说一次,而不是按工具重复 N 遍 ——
#同一个理由(如"不能写长期记忆")在好几个工具上都成立,逐工具写就散了
ROLE_HINT = {
    ROLE_MAIN: "",
    ROLE_SUBAGENT: ("你是子agent:不能起下级agent、不能写长期记忆、不能调 recall_memory、"
                    "不碰定时任务和团队。自己做,或把需求带回父agent。"),
    ROLE_MEMBER: ("你是团队成员:不能起下级agent、不能写长期记忆、不能调 recall_memory、"
                  "不碰定时任务,也不能创建/下线成员。需要人手、或要把发现保存进长期记忆,"
                  "就说给主agent;查看团队用 team_list。"),
}


def tool_filter(schemas, denied):
    """把没权限的工具从工具表里摘掉。

    只收"要摘掉的名字集合"、不收角色 —— 角色在调用方已经换算成集合了,
    以后加角色这个函数不用动。

    传进来的必须是**两个来源合并之后**的完整工具表:原生 schema 和 MCP schema 分开滤,
    只滤其中一个,另一批会整批绕过。
    """
    return [s for s in schemas if s.get("name") not in denied]


def check_roles(known):
    """自检:角色表里写的名字必须真的存在。

    黑名单最危险的失效方式不是"忘了禁某个新工具",而是**名字写错** ——
    拼错的词不会报错,只是静默地什么都不禁,表现是"这工具怎么还能调",
    而且没人会想到是表写错了。所以启动时对一遍。
    只返回问题清单、不抛异常:表写错不该让整个项目起不来。
    """
    known = set(known)
    problems = []
    for role, denied in ROLE_DENIED.items():
        unknown = sorted(denied - known)
        if unknown:
            problems.append(
                f"角色 {role} 的禁用表里有不存在的工具名 {unknown} —— 这些名字不会禁掉任何东西")
    return problems


def run_calculate(expression):
    try:
        return eval(expression)
    except:
        return "Error: Invalid expression"

def run_bash(command, timeout=300, **_ignored):
    output, _exit_code = execute_bash(command, timeout)
    return output

    
def run_read(path, limit=None):
    try:
        file_path = Path(path)
        if not file_path.exists():
            return f"文件不存在: {path}"
        if not file_path.is_file():
            return f"路径不是文件: {path}"
        content = file_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        if limit and limit > 0:
            lines = lines[:limit]
        return chr(10).join(lines)
    except Exception as e:
        return f"读取文件时发生错误: {e}"


def run_write(path, content):
    """把内容整个写进文件。父目录不存在就自动建,已存在直接覆盖。

    校验刻意从"最便宜的拒绝"排到"最贵的落盘":先看是不是受保护的配置文件,
    再看内容类型、再看目标是不是目录,最后才动手写 —— 免得写了一半才发现不该写。
    """
    try:
        file_path = Path(path)

        #.env 里是真实 API key,写坏了只能手工找回。
        #按文件名判断而不是整条路径,所以 D:/x/.env、./.env、.env.local 都会拦下
        if file_path.name == ".env" or file_path.name.startswith(".env."):
            return (f"❌ 拒绝写入 {path}:这是存放密钥的配置文件,工具不允许覆盖它。"
                    f"如果确实要改,请让用户手工改。")

        #模型偶尔会把数字/对象直接塞进 content;提前挡掉,别让它变成一个"写了一半才报错"
        if not isinstance(content, str):
            return (f"❌ content 必须是字符串,收到的是 {type(content).__name__}。"
                    f"请把要写入的完整内容作为字符串再调用一次。")

        #"out/" 这种尾斜杠会被 Path 悄悄吃掉、然后被当成名叫 out 的文件建出来,提前拦
        if str(path).rstrip().endswith(("/", chr(92))):
            return f"❌ 路径看起来是目录,不是文件: {path}。请给出完整文件名。"

        if file_path.is_dir():
            return f"路径是目录,不是文件: {path}"

        #旧大小必须在落盘前量 —— 写完再量就是新文件的大小了
        old_size = file_path.stat().st_size if file_path.exists() else None

        #父目录可能还不存在(out/ 这种)。这一步是加这个工具的主因
        file_path.parent.mkdir(parents=True, exist_ok=True)

        file_path.write_text(content, encoding="utf-8", newline="\n")

        size = file_path.stat().st_size
        lines = 0 if not content else content.count(chr(10)) + (0 if content.endswith(chr(10)) else 1)
        if old_size is None:
            return f"✅ 已写入 {file_path}(新建,{size} 字节,{lines} 行)"
        #覆盖时把旧大小报出来:模型看得见"我冲掉了一个原本有内容的东西",觉得不对可以自己补救
        return f"✅ 已写入 {file_path}(已覆盖原有文件,{old_size} 字节 → {size} 字节,{lines} 行)"
    except Exception as e:
        return f"写入文件时发生错误: {e}"


class ToolRegistry:
    def __init__(self):
        self.handler = {}
    def register(self, name, handler):
        self.handler[name] = handler
    def get(self, name):
        return self.handler.get(name)

def create_default_registry():
    registry = ToolRegistry()
    registry.register("calculator", run_calculate)
    registry.register("bash", run_bash)
    registry.register("read_file", run_read)
    registry.register("write_file", run_write)
    return registry
