# -*- coding: utf-8 -*-
"""MCP(Model Context Protocol)客户端 —— 用 stdio 连本地的 MCP 服务器。

MCP 干的事一句话:**让外部进程能给模型提供工具。**

    模型  ←→  本文件(MCPClient)  ←→  MCP 服务器(另一个进程)

说的话是 JSON-RPC 2.0:一行一条 JSON,一问一答。带 id 的是请求,对方要回;
不带 id 的叫"通知",没人回。一次握手长这样:

    客户端 → initialize                    「我是谁、我支持哪个协议版本」
    服务器 → {protocolVersion, serverInfo} 「我用这个版本」
    客户端 → notifications/initialized      通知,不等回应(少了它有的服务器会拒绝后续请求)
    客户端 → tools/list                    「你有哪些工具?」
    服务器 → {tools: [{name, description, inputSchema}, ...]}
    客户端 → tools/call                    「帮我调 echo,参数是...」
    服务器 → {content: [{type:"text", text:"..."}], isError: false}

传输方式(transport)决定消息怎么送过去。这里只实现 **stdio**:
把服务器当子进程拉起来,消息从它的 stdin 进去、从它的 stdout 出来。本地开发默认这个。
另一种是 HTTP(连远程服务器),那需要真有一台服务器在跑,本项目没有,没实现。

stdio 有一条铁规矩,踩了当场就崩:
**服务器的 stdout 只跑协议,一个字节别的都不能有。**
它想打日志必须写 stderr —— 往 stdout 多打一行"正在启动...",
客户端就会拿这行去 json.loads。随附的 mcp_demo_server.py 就是这么写的。

用法:

    from mcp import MCPClient
    with MCPClient() as client:
        client.connect([sys.executable, "mcp_demo_server.py"])
        client.list_tools()
        print(client.call_tool("echo", {"text": "你好"}))

接到 agent 上是自动的:ClaudeMini 启动时会找同目录下的 mcp_servers.json,
把里面每个服务器的工具并进来(见本文件末尾的 MCPManager)。那份配置长这样:

    {
      "servers": {
        "demo":  { "args": ["mcp_demo_server.py"] },
        "其它":  {
          "command": "node",                  //不写 = 用当前这个 Python 解释器
          "args": ["server.js"],
          "cwd": "...",                       //不写 = 配置文件所在目录
          "env": {"TOKEN": "..."},            //额外环境变量,叠加在现有环境之上
          "prefix": "x__",                    //工具名前缀,不写 = "服务器名__"
          "timeout": 30,                      //单次请求等的秒数
          "enabled": false                    //设 false 可临时关掉
        }
      }
    }

配置不存在、或某个服务器连不上,都只是少几个工具,不会挡住 agent 启动。

想单独用客户端(不经过配置文件和 ClaudeMini):

    with MCPClient() as client:
        client.connect([sys.executable, "某服务器.py"])
        client.list_tools()
        print(client.call_tool("echo", {"text": "你好"}))

直接跑 `python mcp.py` 会连上随附的演示服务器,把上面整条流程走一遍。
"""
import atexit
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
from pathlib import Path

#客户端声明的协议版本(按日期命名)。这只是"我支持这个";
#服务器在 initialize 的回应里给出它实际要用的版本,以它为准 —— 老服务器回旧版本是正常的。
PROTOCOL_VERSION = "2024-11-05"

DEFAULT_TIMEOUT = 30                #单次请求的等待上限(秒)
DEFAULT_CONFIG_NAME = "mcp_servers.json"    #服务器清单,和 mcp.py 放一起


class MCPError(Exception):
    """MCP 出问题:连不上、超时、服务器回 error。统一抛这一个,调用方只需 catch 一种。"""


class MCPClient:
    """一个 MCP 客户端。

    同一时刻只发一个请求 —— 够用,代码也简单(不必给每条请求配一把锁和一个等待位)。
    真要并发,把 _request/_read 换成"每条请求挂一个 Event,读线程按 id 唤醒",其余不用动。
    """

    def __init__(self, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.proc = None
        self.tools = []                 #最近一次 list_tools 的原始结果
        self.server_info = {}
        self.protocol_version = None
        self._id = 0
        self._inbox = queue.Queue()     #读线程往这里放解析好的消息
        self._reader = None
        self._lock = threading.Lock()   #一次只放一条请求出去,见 _request

    #===== 连接与握手 =====

    def connect(self, server):
        """启动服务器并完成握手,成功返回 self(方便连着往下写)。

        server 三选一:
          ["python", "server.py"]                      列表(推荐,Windows 上最省事)
          "python server.py"                           字符串,按 shell 规则拆
          {"command":..., "args":[...], "cwd":..., "env":{...}}
        """
        if isinstance(server, str) and server.startswith(("http://", "https://")):
            raise MCPError("这个实现只支持 stdio(把服务器当子进程跑)。"
                           "HTTP 传输要有真实的远程服务器,本项目没有,未实现。")
        if self.proc is not None:
            self.close()

        cmd, cwd, env = self._normalize(server)

        #bufsize=1 + text:按行收发。stderr 不接管 —— 服务器的日志直接漏到我们的终端,
        #它启动就崩的话能立刻看见原因(所以它写日志要写 stderr,写 stdout 会污染协议)。
        #encoding 锁死 utf-8:MCP 的 stdio 传输按规范就是 UTF-8,跟着系统默认编码走会乱码。
        self.proc = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )

        #读线程:专职读 stdout。为什么不能就地读 —— Windows 的管道不支持 select,
        #没有"带超时地读一行"这种调用,那样一旦服务器不回话,我们就永远卡住。
        #丢进队列之后,超时由调用方那个 get(timeout=...) 负责。
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "claude-mini", "version": "0.1"},
        })
        self.protocol_version = result.get("protocolVersion")
        self.server_info = result.get("serverInfo", {})
        self._notify("notifications/initialized")
        return self

    def close(self):
        """断开:关掉子进程。可以重复调用。"""
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        try:
            proc.stdin.close()          #服务器那边读到 EOF,自己就会退出
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()                 #不体面的收场(它没在 5 秒内退出)
        #读线程是 daemon,而且进程已经没了、stdout 会读到 EOF,它会自己结束

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return False                    #异常照常往外抛,不吞

    #===== 三个主要动作 =====

    def list_tools(self):
        """问服务器有哪些工具。原始结果存进 self.tools,并原样返回。

        返回的是 MCP 的格式(name / description / inputSchema),
        要接到本项目的 agent 上得再转一道,见 as_tools()。
        """
        result = self._request("tools/list", {})
        self.tools = result.get("tools", [])
        return self.tools

    def call_tool(self, name, arguments):
        """调一个工具,返回拼好的纯文本。

        服务器回的是结构化内容(content 数组,元素可以是文本/图片/资源),
        但本项目的工具结果一律当字符串用,所以这里就在这一层拍平成文本。
        """
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        return self._render(result)

    def find_tool(self, name):
        """按名字取工具定义(没有就返回 None)。想把工具接到 agent 上时才需要。"""
        return next((t for t in self.tools if t.get("name") == name), None)

    #===== 接到本项目的 agent 上 =====

    def as_tools(self, prefix=""):
        """转成 TOOLS.py 里那种工具定义 —— 只差一个字段名:inputSchema → input_schema。

        prefix 是给工具名加命名空间用的。同时连好几个服务器时(比如 github__ / fs__),
        不加前缀就可能撞名,而 registry 是按名字索引的,后注册的会把先注册的顶掉。
        """
        return [{
            "name": prefix + t["name"],
            "description": t.get("description") or "",
            "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
        } for t in self.tools]

    def make_handlers(self, prefix=""):
        """生成 {工具名: 处理函数},可以直接丢给 ToolRegistry.register。

        处理函数收 **kwargs,和 ClaudeMini._call_tool 的调用姿势一致。

        参数名对不上就地挡下来,不发给服务器。原因:handler(**kwargs) 收得下任何关键字,
        写错名字不会抛 TypeError,会一路原样发给服务器、那边 args["a"] 抛个 KeyError,
        最后模型看到的是"KeyError: 'a'" —— 它得自己去猜参数该叫什么。
        在这里挡住,就能像本项目其它工具一样把正确参数名报给它,还省一个来回。
        """
        def make(tool, shown_name):
            real_name = tool["name"]            #发给服务器用真名,不带前缀
            schema = tool.get("inputSchema") or {}
            props = list(schema.get("properties") or {})
            required = list(schema.get("required") or [])
            hint = ", ".join(f"{p}(必填)" if p in required else p for p in props) or "(无参数)"

            #真名/参数表都用闭包钉住。要是直接在 for 里引用循环变量,
            #循环结束后所有函数都会指向最后一个工具
            def handler(**kwargs):
                unknown = [k for k in kwargs if k not in props]
                if unknown:
                    return (f"❌ 工具 {shown_name} 调用参数有误:不认识的参数 {', '.join(unknown)}。\n"
                            f"本工具接受的参数:{hint}。请按这些参数名重新调用一次。")
                missing = [k for k in required if k not in kwargs]
                if missing:
                    return (f"❌ 工具 {shown_name} 缺少必填参数:{', '.join(missing)}。\n"
                            f"本工具接受的参数:{hint}。")
                return self.call_tool(real_name, kwargs)
            return handler

        return {prefix + t["name"]: make(t, prefix + t["name"]) for t in self.tools}

    #===== 内部:收发 =====

    def _normalize(self, server):
        """把三种写法统一成 Popen 要的 (命令列表, cwd, env)。"""
        if isinstance(server, dict):
            command = server.get("command")
            if not command:
                raise MCPError("server 字典里缺 command 字段。")
            cmd = [str(command)] + [str(a) for a in server.get("args", [])]
            cwd = server.get("cwd")
            extra = server.get("env") or {}
            #叠加而不是替换:替换会把 PATH 之类的整个抹掉,子进程连解释器都找不到
            env = {**os.environ, **extra} if extra else None
            return cmd, cwd, env
        if isinstance(server, (list, tuple)):
            if not server:
                raise MCPError("server 命令是空的。")
            return [str(x) for x in server], None, None
        if isinstance(server, str):
            #shlex 是按 POSIX 规则拆的,Windows 路径里的反斜杠会被它当转义符吃掉。
            #所以路径带反斜杠或空格时,请改用列表写法。
            cmd = shlex.split(server)
            if not cmd:
                raise MCPError("server 命令是空的。")
            return cmd, None, None
        raise MCPError(f"server 参数类型不认识:{type(server).__name__}。"
                       f"可以是列表、字符串或字典。")

    def _request(self, method, params):
        """发一条请求并等它的回应。

        全程持锁,一次只放一条请求出去。锁不是可有可无的:团队成员各在自己的线程里跑,
        而它们**共享同一个 client** —— 不加锁的话两条请求会同时在管道上跑,
        各自的 _read 会把对方的回应吃掉(协议上只认"下一条不是 method 的消息"),
        谁拿到谁的纯凭运气。MCP 本来就是一问一答的顺序协议,串起来才是对的。
        """
        with self._lock:
            if self.proc is None:
                raise MCPError(f"还没连服务器(调用 {method} 之前要先 connect)。")

            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})

            while True:
                msg = self._read()
                if msg is None:
                    raise MCPError(f"服务器在回应 {method} 之前就退出了"
                                   f"(退出码 {self.proc.poll() if self.proc else '?'});"
                                   f"往上翻它的 stderr,原因通常就在那里。")

                #带 method 的是服务器主动发来的(通知或反向请求),不是对我们这条的回应。
                #判据用 method 而不是 id:id 是两边各自编号的,可能撞车;method 不会。
                if "method" in msg:
                    self._answer_server(msg)
                    continue

                if "error" in msg:
                    err = msg["error"] or {}
                    raise MCPError(f"{method} 被服务器拒绝:"
                                   f"[{err.get('code')}] {err.get('message')}")
                return msg.get("result") or {}

    def _send(self, payload):
        """往服务器 stdin 写一条。"""
        if self.proc is None or self.proc.stdin is None:
            raise MCPError("连接已经关闭了。")
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()     #不 flush 就躺在缓冲区里,服务器永远收不到,然后我们等到超时
        except (BrokenPipeError, OSError) as e:
            raise MCPError(f"写不进服务器(它的 stdin 已经断了):{e}")

    def _notify(self, method, params=None):
        """发一条通知:没有 id,服务器不回。"""
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _read(self):
        """从队列取一条消息。返回 None 表示服务器已经没了(EOF)。"""
        try:
            return self._inbox.get(timeout=self.timeout)
        except queue.Empty:
            raise MCPError(f"等服务器回应超时({self.timeout} 秒)。"
                           f"要么它在忙,要么它压根没打算回 —— 可以调大 timeout 再试。")

    def _pump(self):
        """读线程:把服务器 stdout 的每一行解析成 dict 丢进队列,读到 EOF 就放个 None。

        顺手兜住两种脏数据,免得整个客户端被一行意外输出带崩:
        空白行直接跳过;不是 JSON 的行打个招呼丢掉(比如服务器忘了规矩,往 stdout 打了日志)。
        """
        #先把流抓在手里再迭代:close() 会把 self.proc 置成 None,
        #要是这里每次迭代都去 self.proc.stdout 取,就会在收尾时炸出一个 AttributeError
        stream = self.proc.stdout
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                self._inbox.put(json.loads(line))
            except json.JSONDecodeError:
                print(f"[mcp] 服务器往 stdout 打了非协议内容,已忽略:{line[:120]}")
        self._inbox.put(None)

    def _answer_server(self, msg):
        """处理服务器主动发来的消息。

        没有 id = 通知,本来就不需要回,忽略即可。
        有 id = 服务器反过来请求我们(比如 sampling:让客户端帮忙跑一次模型)。
        我们不支持,但**必须回一条 error** —— 不回它就一直挂在那儿等,
        后面我们自己的请求也可能跟着被堵住。
        """
        if "id" not in msg:
            return
        self._send({"jsonrpc": "2.0", "id": msg["id"],
                    "error": {"code": -32601,
                              "message": f"本客户端不支持 {msg.get('method')}"}})

    @staticmethod
    def _render(result):
        """把工具结果里的 content 数组拍平成一段文本。"""
        parts = []
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                parts.append(str(item))
            elif item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                #图片、内嵌资源这些本项目的工具结果用不上,报个类型占位,别装作没看见
                parts.append(f"[{item.get('type')} 类型的内容,当前不支持展示]")
        text = "\n".join(p for p in parts if p) or "(服务器没有返回内容)"
        return f"❌ {text}" if result.get("isError") else text


class MCPManager:
    """管一组 MCP 服务器:读配置 → 连上 → 把工具汇总给 agent → 退出时收掉子进程。

    必须**整个 agent 树共享同一个实例**(和 Scheduler / TaskStore 同理):
    每个 client 背后是一个子进程加一条读线程,这两样都复制不了 ——
    给每个子agent、每个团队成员各建一份,就是给同一个服务器反复拉进程。
    ClaudeMini 的构造参数 mcp= 就是干这个的。

    配置文件没有、或某个服务器连不上,都只是"少几个工具",**绝不阻断 agent 启动** ——
    和 SKILLS 目录不存在时是一样处理的。所以 load() 只告警、不抛异常。
    """

    def __init__(self, config_path=None, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.config_path = (Path(config_path) if config_path
                            else Path(__file__).resolve().parent / DEFAULT_CONFIG_NAME)
        self.servers = []       # [{"name","prefix","client"}],只放连上的
        self.failed = []        # [(名字, 原因)],连不上的留个档,方便排查
        #显式 close 之外再挂一道兜底:Python 进程退出不会顺手杀掉子进程,
        #agent 要是崩了,这些服务器会挂在后台一直等 stdin。close() 可重复调用,不冲突
        atexit.register(self.close)
        self.load()

    def load(self):
        """按配置连服务器。任何一步出问题都只跳过、不往外抛。"""
        if not self.config_path.is_file():
            return          #没有配置文件 = 这个项目不用 MCP,正常状态,不是错误

        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            servers = raw.get("servers") or {}
            if not isinstance(servers, dict):
                raise ValueError("servers 得是一个对象:{服务器名: 配置}")
        except (OSError, ValueError, json.JSONDecodeError) as e:
            self.failed.append((self.config_path.name, f"配置读不出来:{e}"))
            print(f"[mcp] {self.config_path.name} 解析失败,本次不加载 MCP:{e}")
            return

        for name, cfg in servers.items():
            cfg = cfg or {}
            if not cfg.get("enabled", True):
                continue
            #前缀默认是"服务器名__"。除了防撞名,还有一层安全考虑:
            #没有前缀的话,服务器只要提供一个叫 bash 的工具,就绕开了 PERMISSIONS 里对 bash 的检查
            #(那套检查是按工具名精确匹配的,见 PERMISSIONS.check_permission)
            prefix = cfg.get("prefix", f"{name}__")

            client = MCPClient(timeout=cfg.get("timeout", self.timeout))
            try:
                client.connect(self._server_arg(cfg))
                client.list_tools()
            except Exception as e:
                #这里故意抓得很宽,而且不重新抛出。这是一个"不许失败"的边界:
                #MCP 连不上只该是少几个工具,绝不能把 agent 带得启动不了。
                #要抓的远不止 MCPError —— Popen 在 command 不存在时抛的是
                #FileNotFoundError/OSError(non-MCPError),漏掉它就等于一个配错的
                #服务器名字把整个程序拦在启动阶段(这正是实测踩到的)
                client.close()
                self.failed.append((name, f"{type(e).__name__}: {e}"))
                print(f"[mcp] 服务器 {name} 连接失败,已跳过:{type(e).__name__}: {e}")
                continue

            self.servers.append({"name": name, "prefix": prefix, "client": client})
            print(f"[mcp] 已连接 {name}:{len(client.tools)} 个工具"
                  f"({', '.join(t['name'] for t in client.tools) or '无'})")

    def _server_arg(self, cfg):
        """把配置里的一条服务器定义转成 MCPClient.connect 要的形式。

        command 不写就默认用当前这个解释器 —— 多半配的就是 Python 写的服务器,
        而写死 "python" 可能撞上 PATH 里另一个版本。
        cwd 默认是配置文件所在目录,这样 args 里写相对路径(如 mcp_demo_server.py)
        就不用管 agent 是从哪个目录启动的。
        """
        arg = {
            "command": cfg.get("command") or sys.executable,
            "args": list(cfg.get("args") or []),
            "cwd": cfg.get("cwd") or str(self.config_path.parent),
        }
        if cfg.get("env"):
            arg["env"] = cfg["env"]
        return arg

    def collect(self, reserved=()):
        """汇总所有服务器的工具,返回 (schema 列表, {名字: 处理函数})。

        两边必须一起产出、且**只能包含同一个集合** ——
        注册了处理函数却没放进 schema,模型不知道有这个工具;
        放进了 schema 却没注册处理函数,模型一调就是"❌ Unknown tool"。

        reserved 是已经被原生工具占掉的名字,撞名的整个丢掉。宁可少一个工具,
        也不能让 MCP 悄悄顶替原生工具:registry 是字典,后注册的覆盖先注册的,
        模型看到的名字没变、行为却变了,这种问题极难排查。两个 MCP 服务器之间撞名同理。
        """
        schemas, handlers, taken = [], {}, set(reserved)
        for s in self.servers:
            client, prefix = s["client"], s["prefix"]
            handlers_of = client.make_handlers(prefix=prefix)
            for schema in client.as_tools(prefix=prefix):
                name = schema["name"]
                if name in taken:
                    print(f"[mcp] 工具名 {name} 已被占用,跳过它(来自服务器 {s['name']})")
                    continue
                taken.add(name)
                schemas.append(schema)
                handlers[name] = handlers_of[name]
        return schemas, handlers

    def close(self):
        """收掉所有子进程。可以重复调用(启动时挂的 atexit 和显式收尾会各调一次)。"""
        for s in self.servers:
            s["client"].close()


#直接跑本文件 = 连上随附的演示服务器,把完整流程走一遍。
#既是自测,也是"这东西到底怎么用"的活文档。
if __name__ == "__main__":
    server = [sys.executable, str(Path(__file__).with_name("mcp_demo_server.py"))]
    #用 sys.executable 而不是写死 "python":PATH 里那个可能是别的版本,甚至没有

    print(f"连接服务器:{' '.join(server)}\n")
    with MCPClient(timeout=10) as client:
        client.connect(server)
        print(f"握手完成:服务器 {client.server_info.get('name')} "
              f"{client.server_info.get('version')},协议 {client.protocol_version}\n")

        tools = client.list_tools()
        print(f"服务器提供了 {len(tools)} 个工具:")
        for t in tools:
            required = t.get("inputSchema", {}).get("required", [])
            params = ", ".join(required) or "无参数"
            print(f"  - {t['name']}({params}):{t.get('description', '')}")

        print("\n逐个调用:")
        for name, args in [("echo", {"text": "你好,MCP"}),
                           ("add", {"a": 3, "b": 4}),
                           ("now", {}),
                           ("no_such_tool", {})]:
            try:
                print(f"  {name}{args or ''} → {client.call_tool(name, args)}")
            except MCPError as e:
                print(f"  {name}{args or ''} → 调用失败:{e}")

        print("\n转成本项目的工具格式(可直接注册给 agent):")
        for tool in client.as_tools(prefix="demo__"):
            print(f"  {tool['name']}  参数:{list(tool['input_schema'].get('properties', {}))}")
        print(f"  处理函数:{list(client.make_handlers(prefix='demo__'))}")

    print("\n连接已关闭。")
