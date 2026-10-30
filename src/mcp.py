# -*- coding: utf-8 -*-
"""MCP(Model Context Protocol)客户端 —— 两种传输:stdio 子进程、Streamable HTTP 远程。

MCP 干的事一句话:**让外部进程能给模型提供工具。**

    模型  ←→  本文件(MCPClient)  ←→  MCP 服务器(另一个进程或另一台机器)

说的话是 JSON-RPC 2.0。带 id 的是请求,对方要回;不带 id 的叫"通知",没人回。
一次握手长这样:

    客户端 → initialize                    「我是谁、我支持哪个协议版本」
    服务器 → {protocolVersion, serverInfo} 「我用这个版本」
    客户端 → notifications/initialized      通知,不等回应(少了它有的服务器会拒绝后续请求)
    客户端 → tools/list                    「你有哪些工具?」(可能分页,这里会翻到没有下一页为止)
    服务器 → {tools: [{name, description, inputSchema}, ...]}
    客户端 → tools/call                    「帮我调 echo,参数是...」
    服务器 → {content: [{type:"text", text:"..."}], isError: false}

传输方式(transport)决定消息怎么送过去,这里实现三种:

    · stdio          把服务器当子进程拉起来,消息从 stdin 进、stdout 出。本地默认。
    · Streamable HTTP 往一个 URL POST JSON-RPC(MCP 规范 2025-03-26 起的远程方案)。
      回应可以是单条 JSON,也可以是一帧一帧的 SSE 流(data: 行攒到空行算一条,
      流上顺带捎的服务器主动消息照收)。会话靠响应头 Mcp-Session-Id:
      initialize 拿到就带着走,后面所有请求/通知都署上它。
    · HTTP+SSE       旧版双端点方案(GET /sse + POST /messages)。请求与回应分在两条
      HTTP 连接:先开一条常驻 GET /sse,首帧 event:endpoint 报来可 POST 的地址(带
      ?sessionId=),之后每条 JSON-RPC POST 到那儿只回 202,真正的 result 从那条 GET
      流上异步飘回。会话在 endpoint 的 query 里,不靠 Mcp-Session-Id。配type:"sse" 启用。

stdio 有一条铁规矩,踩了当场就崩:
**服务器的 stdout 只跑协议,一个字节别的都不能有。**
它想打日志必须写 stderr —— 往 stdout 多打一行"正在启动...",
客户端就会拿这行去 json.loads。随附的 mcp_demo_server.py 就是这么写的。

HTTP 这条路依赖 httpx —— 不新增包,它是 anthropic SDK 的传递依赖,装了 SDK 就在。

用法:

    from mcp import MCPClient
    with MCPClient() as client:
        client.connect([sys.executable, "mcp_demo_server.py"])       # stdio
        client.connect({"url": "https://example.com/mcp/",
                        "headers": {"Authorization": "Bearer ${TOKEN}"}})  # 远程 HTTP
        client.connect({"url": "http://127.0.0.1:9123/sse",
                        "transport": "sse"})                              # 旧版 SSE
        client.list_tools()
        print(client.call_tool("echo", {"text": "你好"}))

接到 agent 上是自动的:ClaudeMini 启动时会找同目录下的 mcp_servers.json,
把里面每个服务器的工具并进来(见本文件末尾的 MCPManager)。那份配置长这样:

    {
      "servers": {
        "demo":  { "args": ["mcp_demo_server.py"] },
        "github":{
          "url": "https://api.githubcopilot.com/mcp/",   //出现 url = 走远程;与 command 互斥
          //远程默认 Streamable HTTP;服务器只出旧版 SSE 端点时补 "type": "sse" 切过去
          "headers": {"Authorization": "Bearer ${GITHUB_MCP_PAT}"},
                                                       //值经 os.path.expandvars 插值,
                                                       //${VAR} 从环境取 —— 凭证不进配置文件;
                                                       //插值后为空 = 配错,跳过并留档,不阻断
          "prefix": "x__",                    //工具名前缀,不写 = "服务器名__"
          "timeout": 60,                      //单次请求等的秒数
          "enabled": false                    //设 false 可临时关掉
        },
        "其它":  {
          "command": "node",                  //不写 = 用当前这个 Python 解释器(仅 stdio)
          "args": ["server.js"],
          "cwd": "...",                       //不写 = 配置文件所在目录
          "env": {"TOKEN": "..."},            //额外环境变量,叠加在现有环境之上
        }
      }
    }

配置不存在、或某个服务器连不上,都只是少几个工具,不会挡住 agent 启动。

直接跑 `python mcp.py` 会连上随附的演示服务器,把 stdio 的完整流程走一遍;
`python mcp.py --http` 会拉起随附的 HTTP 演示服务器(mcp_http_demo_server.py)、
`python mcp.py --sse` 拉起 SSE 演示服务器(mcp_sse_demo_server.py)各走同一条流程
—— 同一套语义,三种传输对照。
"""
import atexit
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urljoin
import ui

#客户端声明的协议版本(按日期命名)。这只是"我支持这个";
#服务器在 initialize 的回应里给出它实际要用的版本,以它为准 —— 老服务器回旧版本是正常的。
PROTOCOL_VERSION = "2024-11-05"          #stdio 路:保守声明,老服务器都认
HTTP_PROTOCOL_VERSION = "2025-03-26"     #HTTP 路:Streamable HTTP 自这一版起才有,声明低了有的远程服务器不给建会话

DEFAULT_TIMEOUT = 30                #单次请求的等待上限(秒)
DEFAULT_CONFIG_NAME = "mcp_servers.json"    #服务器清单,和 mcp.py 放一起
MAX_TOOL_PAGES = 32                 #tools/list 翻页封顶:防服务器给出永远不变的 nextCursor 把这里拖死

#headers 插值用的环境变量引用扫描:${VAR} 或 $VAR(裸名到非单词符为止)
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


class MCPError(Exception):
    """MCP 出问题:连不上、超时、服务器回 error。统一抛这一个,调用方只需 catch 一种。"""


#==============================================================================
# 传输层:只管把一条 JSON-RPC 消息送出去、把对端消息一条条收回来,不懂协议语义
#==============================================================================

def _iter_sse_frames(lines):
    """把 SSE 的行流逐帧 yield 成 (event, data_blob);data_blob 是该事件 data: 行的原文(多行以 \n 连)。

    生成器不是列表:旧版 SSE 的 GET 流是长连接的、永不结束,只有边到边 yield,后台读才能
    一收到 result 就派发。规矩:一条事件到空行收束即 yield;`:` 开头注释跳过;`event:` 缺省当
    "message";流尾没赶上空行的那条在收尾时补 yield。两条 HTTP 路都用它:
      - Streamable HTTP 只取 data_blob 去 json.loads(_HttpTransport._parse_sse 在其上过滤);
      - 旧版 SSE 要靠 event 认出首帧 `endpoint`(data 是 POST 目标、不是 JSON-RPC)。
    """
    event, data = "message", []
    for raw in lines:
        line = (raw or "").rstrip("\r")
        if line.startswith(":"):
            continue                     #注释行(心跳/保活),看一眼就走
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
            continue
        if line.startswith("event:"):
            event = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:                             #流断了没赶上空行 —— 攒着的这条还是要
        yield event, "\n".join(data)


class _StdioTransport:
    """把服务器当子进程:消息走它的 stdin/stdout。

    读线程 + 队列不是摆设:Windows 的管道不支持 select,没有"带超时地读一行"
    这种调用,一旦服务器不回话就地读会永远卡住。丢进队列后,超时由拿的一方
    next_message(block=False, timeout=...) 负责。
    """

    def __init__(self, cmd, cwd=None, env=None):
        #bufsize=1 + text:按行收发。stderr 不接管 —— 服务器的日志直接漏到我们的终端,
        #它启动就崩的话能立刻看见原因(所以它写日志要写 stderr,写 stdout 会污染协议)。
        #encoding 锁死 utf-8:MCP 的 stdio 传输按规范就是 UTF-8,跟着系统默认编码走会乱码。
        self.proc = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self._inbox = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def send(self, payload):
        """往服务器 stdin 写一条。"""
        if self.proc is None or self.proc.stdin is None:
            raise MCPError("连接已经关闭了。")
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()     #不 flush 就躺在缓冲区里,服务器永远收不到,然后我们等到超时
        except (BrokenPipeError, OSError) as e:
            raise MCPError(f"写不进服务器(它的 stdin 已经断了):{e}")

    def next_message(self, timeout=None):
        """取对端发来的一条。None = EOF(服务器没了)。超了抛 MCPError。"""
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            raise MCPError(f"等服务器回应超时({timeout} 秒)。"
                           f"要么它在忙,要么它压根没打算回 —— 可以调大 timeout 再试。")

    def poll_exit(self):
        """服务器进程的退出码;还在跑/已退都拿得到,None = 还没退。"""
        return self.proc.poll() if self.proc is not None else None

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
                ui.warn(f"[mcp] 服务器往 stdout 打了非协议内容,已忽略:{line[:120]}")
        self._inbox.put(None)

    def close(self):
        """关掉子进程。可以重复调用。"""
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


class _HttpTransport:
    """Streamable HTTP:往同一个 URL POST JSON-RPC,回应认 JSON 和 SSE 两种形态。

    和 _StdioTransport 同一张脸(send / next_message / poll_exit / close),
    上面的 MCPClient 因此完全不关心底下是哪条路。

    会话:initialize 的响应头里若有 Mcp-Session-Id 就存下,之后每个请求/通知都带上;
    带 DELETE 收尾。服务器没给 = 无会话服务器,合法,跳。
    """

    def __init__(self, url, headers=None, timeout=DEFAULT_TIMEOUT, client=None):
        import httpx        #传递依赖(anthropic SDK 带的),延迟 import 保住 stdio 路零三方依赖
        self._httpx = httpx
        self.url = url
        self.timeout = timeout
        #headers 由调用方插值成终值;本类不碰环境变量,也就不把凭证带进任何报错文案
        self.headers = dict(headers or {})
        self.session_id = None
        self._closed = False
        self._inbox = queue.Queue()    #对端消息都先到这里(send 的响应/SSE 帧)
        #client 是注入口:单测塞 httpx.MockTransport 建的 Client 进来,不出网也能走全路径
        self._client = client or httpx.Client(headers=self.headers, timeout=timeout)

    def send(self, payload):
        """POST 一条消息,把回应收进队列。返回 None 只代表"这条不产消息"(202)。"""
        if self._closed:
            raise MCPError("连接已经关闭了。")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            resp = self._client.post(self.url, json=payload,
                                     headers=headers, timeout=self.timeout)
        except self._httpx.HTTPError as e:
            #连接/读写层面的毛病归一到 MCPError,调用方仍只需 catch 一种
            raise MCPError(f"HTTP 传输出错({self.url}):{type(e).__name__}: {e}")

        #会话头:任何响应带了都认(规范说必在 initialize,有的服务器每条都回,照单全收)
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid

        if resp.status_code in (202, 204) or not resp.content:
            return              #通知已受理/空回应 —— 不产消息
        if not resp.is_success:
            #状态码和响应体都带着走:404 多半是会话过期,401/403 是鉴权头不对,
            #光说"失败了"让人没法分辨。headers 不进这里的任何文案。
            raise MCPError(f"HTTP {resp.status_code}:{resp.text[:200]}")

        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            for msg in self._parse_sse(resp.iter_lines()):
                self._inbox_put(msg)
        else:
            try:
                self._inbox_put(resp.json())
            except ValueError as e:
                raise MCPError(f"服务器回的不是合法 JSON({ctype}):{str(e)[:120]}")
        return None

    def next_message(self, timeout=None):
        """取对端发来的一条。HTTP 路的 EOF 概念就是"连接已关" → None。"""
        if self._closed:
            return None
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            raise MCPError(f"等服务器回应超时({timeout} 秒)。"
                           f"要么它在忙,要么它压根没打算回 —— 可以调大 timeout 再试。")

    def poll_exit(self):
        return None if self._closed else 0      #没有进程可探;0 = "还活着"

    @staticmethod
    def _parse_sse(lines):
        """把 SSE 行流攒成 JSON-RPC 消息列表(只取每条事件的 data、去 json.loads)。
    
        拆帧交给 _iter_sse_frames;这里只负责"data 不是合法 JSON 就当脏数据 warn 丢弃"。
        """
        msgs = []
        for _event, blob in _iter_sse_frames(lines):
            try:
                msgs.append(json.loads(blob))
            except json.JSONDecodeError:
                ui.warn(f"[mcp] SSE 帧不是合法 JSON,已忽略:{blob[:120]}")
        return msgs

    def close(self):
        """尽力 DELETE 结束会话,再关连接。可以重复调用。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self.session_id:
                self._client.delete(self.url,
                                    headers={"Mcp-Session-Id": self.session_id},
                                    timeout=self.timeout)
        except Exception as e:
            #收尾不体面只打招呼:会话没了服务器那边也会自己回收
            ui.warn(f"[mcp] 结束会话失败(忽略):{type(e).__name__}: {e}")
        try:
            self._client.close()
        except Exception:
            pass

    #send 里往队列放消息的小口子,单测可以直接喂
    def _inbox_put(self, msg):
        self._inbox.put(msg)


class _SseTransport:
    """旧版 HTTP+SSE 传输:一条常驻 GET /sse 收货,JSON-RPC 请求 POST 到 GET 报来的 endpoint。

    与另两条传输同一张脸(send / next_message / poll_exit / close),语义层因此不感知底下是哪条。
    和 Streamable HTTP 的关键差别:请求与回应分在两条连接上 ——
      - send 的 POST 只回 202,真正的 result 从后台 GET 流上异步飘下来、进 _inbox;
      - 会话不在 Mcp-Session-Id 头里,而在 endpoint 的 query(?sessionId=)里,由服务器定,我们只把它照回 POST。
    语义层的 _request 用锁串行、把"下一条不带 method 的消息"当回应 —— 对这种异步收货天然成立,不用改。
    """

    def __init__(self, url, headers=None, timeout=DEFAULT_TIMEOUT, client=None):
        import httpx        #传递依赖(anthropic SDK 带的),延迟 import 保住 stdio 路零三方依赖
        self._httpx = httpx
        self.url = url
        self.timeout = timeout
        #headers 由调用方插值成终值;本类不碰环境变量,也就不把凭证带进任何报错文案
        self.headers = dict(headers or {})
        #client 是注入口:单测可塞自建 Client;真实路径自己建(headers 挂客户端级,GET/POST 都带)
        self._client = client or httpx.Client(headers=self.headers, timeout=timeout)
        self._inbox = queue.Queue()
        self._post_url = None          #GET 首帧报来的 POST 目标(相对路径会经 urljoin 补成绝对)
        self._ready = threading.Event()  #endpoint 到手才置位;send 会先等它
        self._closed = False
        self._error = None             #后台流挂了(连不上/非2xx)的因由,留给 send 报因
        self._reader = threading.Thread(target=self._listen, daemon=True)
        self._reader.start()

    def _listen(self):
        """后台收货线程:读 GET /sse 流,首帧认 endpoint、其余帧当 JSON-RPC 消息进队列。"""
        try:
            #收货流不设读超时(长连接可能几秒才飘一帧);靠 close() 关客户端打断阻塞读
            with self._client.stream("GET", self.url,
                                     headers={"Accept": "text/event-stream"},
                                     timeout=None) as resp:
                if not resp.is_success:
                    #连上了但状态不对(多半是没这个端点/鉴权失败):记下,等 send 报因
                    self._error = f"GET {self.url} 回了 {resp.status_code}"
                    return
                for event, blob in _iter_sse_frames(resp.iter_lines()):
                    if event == "endpoint" and self._post_url is None:
                        #首帧:data 是后续 POST 的目标(多半带 ?sessionId= 的相对路径)
                        self._post_url = urljoin(self.url, blob)
                        self._ready.set()
                        continue
                    try:
                        self._inbox.put(json.loads(blob))
                    except json.JSONDecodeError:
                        ui.warn(f"[mcp] SSE 帧不是合法 JSON,已忽略:{blob[:120]}")
        except Exception as e:
            #close() 关客户端会让迭代抛错 —— 那是正常收场;其余才是真故障
            if not self._closed:
                self._error = f"{type(e).__name__}: {e}"

    def send(self, payload):
        """POST 一条请求到 endpoint。POST 只回执、不产消息,结果在后台 GET 流上。"""
        if self._closed:
            raise MCPError("连接已经关闭了。")
        if not self._ready.wait(self.timeout):
            #endpoint 一直没来 = 那条 GET 流没报上可 POST 的地址(没连上/端点不对/服务器没实现)
            raise MCPError(f"SSE 没等到 endpoint 事件(服务器没在 {self.url} 上报可 POST 的地址)"
                           + (f";底层:{self._error}" if self._error else ""))
        headers = {"Content-Type": "application/json"}
        try:
            resp = self._client.post(self._post_url, json=payload,
                                     headers=headers, timeout=self.timeout)
        except self._httpx.HTTPError as e:
            raise MCPError(f"SSE 传输出错(POST {self._post_url}):{type(e).__name__}: {e}")
        if not resp.is_success:
            #状态码+响应体前 200 字带因;headers 不进这里的任何文案(与 HTTP 路一致)
            raise MCPError(f"HTTP {resp.status_code}:{resp.text[:200]}")
        return None      #202 受理 —— 不产消息,调用方回 _request 循环里从 _inbox 等结果

    def next_message(self, timeout=None):
        """取后台流送来的一条。EOF 概念在这条路上就是"连接已关" → None。"""
        if self._closed:
            return None
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            raise MCPError(f"等服务器回应超时({timeout} 秒)。"
                           f"要么它在忙,要么它压根没打算回 —— 可以调大 timeout 再试。")

    def poll_exit(self):
        return None if self._closed else 0      #没有进程可探;0 = "还活着"

    def close(self):
        """关客户端打断后台流。可以重复调用。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._client.close()
        except Exception:
            pass


#==============================================================================
# 协议语义层:握手/请求-应答/工具 —— 底下换成哪条传输都不动
#==============================================================================

class MCPClient:
    """一个 MCP 客户端。

    同一时刻只发一个请求 —— 够用,代码也简单(不必给每条请求配一个等待位)。
    真要并发,把 _request 换成"每条请求挂一个 Event,按 id 唤醒",其余不用动。
    """

    def __init__(self, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.transport = None               #stdio 或 http 两条路之一,见 connect
        self.tools = []                 #最近一次 list_tools 的原始结果
        self.server_info = {}
        self.protocol_version = None
        self._id = 0
        self._lock = threading.Lock()   #一次只放一条请求出去,见 _request

    #===== 连接与握手 =====

    def connect(self, server):
        """启动服务器并完成握手,成功返回 self(方便连着往下写)。

        server 三选一,加一条远程路:
          ["python", "server.py"]                      列表(推荐,Windows 上最省事)
          "python server.py"                           字符串,按 shell 规则拆
          {"command":..., "args":[...], "cwd":..., "env":{...}}    stdio
          {"url":..., "headers":{...}}                 Streamable HTTP
          {"url":..., "headers":{...}, "transport":"sse"}    旧版 HTTP+SSE(双端点)
        """
        if self.transport is not None:
            self.close()

        if isinstance(server, dict) and server.get("url"):
            if server.get("transport") == "sse":
                self.transport = _SseTransport(
                    server["url"],
                    headers=server.get("headers") or {},
                    timeout=server.get("timeout", self.timeout),
                    client=server.get("_client"),
                )
                declared_version = PROTOCOL_VERSION    #旧版 SSE 本就配套 2024-11-05
            else:
                self.transport = _HttpTransport(
                    server["url"],
                    headers=server.get("headers") or {},
                    timeout=server.get("timeout", self.timeout),
                    client=server.get("_client"),      # 单测注入口,配置里不该出现
                )
                declared_version = HTTP_PROTOCOL_VERSION
        else:
            cmd, cwd, env = self._normalize(server)
            self.transport = _StdioTransport(cmd, cwd=cwd, env=env)
            declared_version = PROTOCOL_VERSION

        result = self._request("initialize", {
            "protocolVersion": declared_version,
            "capabilities": {},
            "clientInfo": {"name": "claude-mini", "version": "0.1"},
        })
        self.protocol_version = result.get("protocolVersion")
        self.server_info = result.get("serverInfo", {})
        self._notify("notifications/initialized")
        return self

    def close(self):
        """断开传输。可以重复调用。"""
        if self.transport is None:
            return
        t, self.transport = self.transport, None
        t.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return False                    #异常照常往外抛,不吞

    #===== 三个主要动作 =====

    def list_tools(self):
        """问服务器有哪些工具,分页就翻到底。原始结果存进 self.tools,并原样返回。

        返回的是 MCP 的格式(name / description / inputSchema),
        要接到本项目的 agent 上得再转一道,见 as_tools()。

        远程服务器(GitHub remote 有几十个工具)普遍一页给不完:回应的
        nextCursor 非空就带着它再要下一页,直到没有 —— 封顶 MAX_TOOL_PAGES,
        防服务器回的 cursor 永远在变、页页收不到尾。
        """
        tools, cursor, pages = [], None, 0
        while True:
            result = self._request("tools/list",
                                   {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            pages += 1
            if not cursor:
                break
            if pages >= MAX_TOOL_PAGES:
                ui.warn(f"[mcp] tools/list 翻了 {MAX_TOOL_PAGES} 页仍有 nextCursor,停止翻页"
                        f"(多半是服务器 cursor 异常,后边的工具这次拿不到)")
                break
        self.tools = tools
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

    @staticmethod
    def _normalize(server):
        """把 stdio 的三种写法统一成 (命令列表, cwd, env)。"""
        if isinstance(server, dict):
            command = server.get("command")
            if not command:
                raise MCPError("server 字典里既没有 url 也没有 command。")
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
            if self.transport is None:
                raise MCPError(f"还没连服务器(调用 {method} 之前要先 connect)。")

            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})

            while True:
                msg = self._read()
                if msg is None:
                    code = self.transport.poll_exit() if self.transport else "?"
                    raise MCPError(f"服务器在回应 {method} 之前就没了"
                                   f"(退出码 {code});"
                                   f"stdio 路往上翻它的 stderr,原因通常就在那里;"
                                   f"http 路多半是连接断了或服务器重启了。")

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
        """交传输层送一条。"""
        if self.transport is None:
            raise MCPError("连接已经关闭了。")
        self.transport.send(payload)

    def _notify(self, method, params=None):
        """发一条通知:没有 id,服务器不回。"""
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _read(self):
        """从传输层取一条消息。返回 None 表示对端已经没了(EOF)。"""
        return self.transport.next_message(timeout=self.timeout)

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
    每个 client 背后是一条连接(子进程或 HTTP 会话)加一条读线程,这两样都复制不了 ——
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
            ui.warn(f"[mcp] {self.config_path.name} 解析失败,本次不加载 MCP:{e}")
            return

        for name, cfg in servers.items():
            cfg = cfg or {}
            if not cfg.get("enabled", True):
                continue
            #前缀默认是"服务器名__"。除了防撞名,还有一层安全考虑:
            #没有前缀的话,服务器只要提供一个叫 bash 的工具,就绕开了 PERMISSIONS 里对 bash 的检查
            #(那套检查是按工具名精确匹配的,见 PERMISSIONS.check_permission)
            prefix = cfg.get("prefix", f"{name}__")

            try:
                arg = self._server_arg(cfg)
            except MCPError as e:
                self.failed.append((name, str(e)))
                ui.warn(f"[mcp] 服务器 {name} 配置有问题,已跳过:{e}")
                continue

            client = MCPClient(timeout=cfg.get("timeout", self.timeout))
            try:
                client.connect(arg)
                client.list_tools()
            except Exception as e:
                #这里故意抓得很宽,而且不重新抛出。这是一个"不许失败"的边界:
                #MCP 连不上只该是少几个工具,绝不能把 agent 带得启动不了。
                #要抓的远不止 MCPError —— Popen 在 command 不存在时抛的是
                #FileNotFoundError/OSError(non-MCPError),漏掉它就等于一个配错的
                #服务器名字把整个程序拦在启动阶段(这正是实测踩到的)
                client.close()
                self.failed.append((name, f"{type(e).__name__}: {e}"))
                ui.warn(f"[mcp] 服务器 {name} 连接失败,已跳过:{type(e).__name__}: {e}")
                continue

            self.servers.append({"name": name, "prefix": prefix, "client": client})
            ui.debug(f"[mcp] 已连接 {name}:{len(client.tools)} 个工具"
                  f"({', '.join(t['name'] for t in client.tools) or '无'})")

    def _server_arg(self, cfg):
        """把配置里的一条服务器定义转成 MCPClient.connect 要的形式。

        有 url = 远程路(Streamable HTTP 或旧版 SSE,靠 type 区分)。headers 的值在这里做环境变量
        插值 —— 凭证不写进配置文件(配置会进 git、会被 agent 的 read_file 读到,token 只能活在
        .env/环境里)。url 与 command 互斥,同时出现是配置矛盾,当场拦:两条传输的语义差别太大,
        猜哪条都是错。

        stdio 路:command 不写就默认用当前这个解释器 —— 多半配的就是 Python 写的
        服务器,而写死 "python" 可能撞上 PATH 里另一个版本。cwd 默认是配置文件所在
        目录,这样 args 里写相对路径(如 mcp_demo_server.py)就不用管 agent 是从哪个
        目录启动的。
        """
        url = cfg.get("url")
        if url:
            if cfg.get("command") or cfg.get("args"):
                raise MCPError("url 和 command/args 同时出现:互斥配置,无法判定走哪条传输。")
            headers, missing = {}, []
            for key, value in (cfg.get("headers") or {}).items():
                #插值前扫描模板里的 ${VAR}/$VAR 并逐个查 os.environ:
                #缺一个就拦 —— 这种头发出去只换来一个 401,不如不问。
                #不这么干而去看插值后的串,会把 "币种: $USD" 这种合法字面值误杀
                refs = [m.group(1) or m.group(2) for m in
                        _ENV_REF.finditer(str(value))]
                lost = [v for v in refs if v not in os.environ]
                if lost:
                    missing.extend(lost)
                    continue
                headers[key] = os.path.expandvars(str(value))
            if missing:
                raise MCPError(f"headers 里的环境变量未设置:{', '.join(sorted(set(missing)))};跳过。")
            arg = {"url": url, "headers": headers}
            if cfg.get("timeout"):
                arg["timeout"] = cfg["timeout"]
            #type:"sse" = 走旧版 HTTP+SSE 双端点(GET /sse 收货 + POST endpoint);不写默认 Streamable HTTP
            if cfg.get("type") == "sse":
                arg["transport"] = "sse"
            return arg

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
                    ui.warn(f"[mcp] 工具名 {name} 已被占用,跳过它(来自服务器 {s['name']})")
                    continue
                taken.add(name)
                schemas.append(schema)
                handlers[name] = handlers_of[name]
        return schemas, handlers

    def close(self):
        """收掉所有连接。可以重复调用(启动时挂的 atexit 和显式收尾会各调一次)。"""
        for s in self.servers:
            s["client"].close()


def _selfcheck_http():
    """python mcp.py --http:拉起随附的 HTTP 演示服务器,把同一条流程在 HTTP 传输上走一遍。"""
    server_proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("mcp_http_demo_server.py"))],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8",
    )
    try:
        url_line = server_proc.stdout.readline().strip()      #服务器就绪后先吐一行 URL
        if not url_line.startswith("http://"):
            raise MCPError(f"HTTP 演示服务器没报到(第一行:{url_line!r})")
        url = url_line
        print(f"连接远程服务器:{url}\n")
        with MCPClient(timeout=10) as client:
            client.connect({"url": url})
            print(f"握手完成:服务器 {client.server_info.get('name')} "
                  f"{client.server_info.get('version')},协议 {client.protocol_version}"
                  f"(会话 {client.transport.session_id or '无'})\n")
            tools = client.list_tools()
            print(f"服务器提供了 {len(tools)} 个工具(分页已合并):")
            for t in tools:
                print(f"  - {t['name']}:{(t.get('description') or '')[:40]}")
            print("\n调用:add(20, 22) →", client.call_tool("add", {"a": 20, "b": 22}))
        print("\n连接已关闭。")
    finally:
        server_proc.terminate()
        server_proc.wait(timeout=5)


def _selfcheck_sse():
    """python mcp.py --sse:拉起随附的旧版 SSE 演示服务器,验证双端点传输能走同一条流程。"""
    server_proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("mcp_sse_demo_server.py"))],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8",
    )
    try:
        url_line = server_proc.stdout.readline().strip()      #服务器就绪后先吐一行 GET /sse 地址
        if not url_line.startswith("http://"):
            raise MCPError(f"SSE 演示服务器没报到(第一行:{url_line!r})")
        url = url_line
        print(f"连接 SSE 服务器:{url}\n")
        with MCPClient(timeout=10) as client:
            client.connect({"url": url, "transport": "sse"})
            print(f"握手完成:服务器 {client.server_info.get('name')} "
                  f"{client.server_info.get('version')},协议 {client.protocol_version}"
                  f"(POST 目标 {client.transport._post_url})\n")
            tools = client.list_tools()
            print(f"服务器提供了 {len(tools)} 个工具:")
            for t in tools:
                print(f"  - {t['name']}:{(t.get('description') or '')[:40]}")
            print("\n调用:add(20, 22) →", client.call_tool("add", {"a": 20, "b": 22}))
        print("\n连接已关闭。")
    finally:
        server_proc.terminate()
        server_proc.wait(timeout=5)


#直接跑本文件 = 连上随附的演示服务器,把完整流程走一遍。
#既是自测,也是"这东西到底怎么用"的活文档。
if __name__ == "__main__":
    if "--http" in sys.argv:
        _selfcheck_http()
        sys.exit(0)
    if "--sse" in sys.argv:
        _selfcheck_sse()
        sys.exit(0)

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
