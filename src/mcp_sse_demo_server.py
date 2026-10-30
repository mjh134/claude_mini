# -*- coding: utf-8 -*-
"""旧版 HTTP+SSE 版 MCP 演示服务器 —— 与 mcp_http_demo_server.py 同一套工具,老一套双端点传输。

只依赖标准库(http.server),给测试和 `python mcp.py --sse` 当靶子用。演示的正是 SSE 传输
和 Streamable HTTP 的关键差别 —— 请求与回应分在两条连接上:

    · GET  /sse               客户端开一条常驻收货流。连上后先回一帧
                              event: endpoint / data: /messages?sessionId=<sid>,
                              告诉对方"要发就往这儿 POST";此后这条流只用来陆续飘回应。
    · POST /messages?sid=...   收一条 JSON-RPC,处理结果**不回在这个 POST 上**
                              (只回 202 空 body),而是投进该会话那条 GET 流的队列,
                              由 GET 那边一帧一帧 data: 送回去。

会话 id 活在 endpoint 的 query 里,不是 Mcp-Session-Id 响应头 —— 这是两代方案的又一处不同。

日志走 stderr,和另两个演示服务器同一条习惯。就绪后第一行 stdout 打 GET /sse 的地址(端口随机)。
"""
import json
import queue
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {"name": "echo", "description": "把传进来的文字原样返回。用来确认链路是通的。",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "add", "description": "计算两个数相加,返回算式与结果。",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"},
                                                      "b": {"type": "number"}},
                     "required": ["a", "b"]}},
    {"name": "now", "description": "返回服务器这台机器上的当前时间。",
     "inputSchema": {"type": "object", "properties": {}}},
]

#每个会话一条队列:GET 那边 get、POST 那边 put。sid -> queue.Queue()
SESSIONS = {}


def run_tool(name, args):
    if name == "echo":
        return f"服务器收到:{args.get('text', '')}"
    if name == "add":
        a, b = args.get("a", 0), args.get("b", 0)
        return f"{a} + {b} = {a + b}"
    if name == "now":
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    raise KeyError(name)


def handle_message(msg):
    """把一条 JSON-RPC 请求变成 (是否产消息, 回应对象)。通知不产消息。"""
    method = msg.get("method")
    msg_id = msg.get("id")                 #没有 id = 通知
    params = msg.get("params") or {}

    if method == "initialize":
        return msg_id is not None, {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "claude-mini-sse-demo", "version": "0.1"},
                "capabilities": {"tools": {}},
            },
        }

    if msg_id is None:                     #通知:受理即可,不产消息
        return False, None

    if method == "tools/list":
        return True, {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        try:
            text = run_tool(name, params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except KeyError:
            result = {"content": [{"type": "text", "text": f"没有这个工具:{name}"}],
                      "isError": True}
        return True, {"jsonrpc": "2.0", "id": msg_id, "result": result}

    return True, {"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32601, "message": f"不支持的方法 {method}"}}


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):      #访问日志走 stderr,不打扰 stdout 的报到
        print(f"[sse-demo] {fmt % args}", file=sys.stderr, flush=True)

    def _write_frame(self, text):
        """往收货流上写一帧并立刻 flush:SSE 靠 flush 让对方即时可见。"""
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    def do_GET(self):
        #只服务 /sse;其余路径当没这个端点
        if not self.path.split("?")[0].rstrip("/").endswith("/sse") \
                and self.path != "/sse":
            return self._send_plain(404, "not found")

        sid = uuid.uuid4().hex[:12]
        q = queue.Queue()
        SESSIONS[sid] = q
        #HTTP/1.0 + 不设 Content-Length = "直到连接关闭为止",正是流式收货要的语义
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        #首帧:告诉客户端 POST 的目标(相对路径,客户端会 urljoin 成绝对)
        self._write_frame(f"event: endpoint\ndata: /messages?sessionId={sid}\n\n")
        try:
            while True:
                item = q.get()                    #阻塞等这条会话的下一条回应
                if item is None:                  #哨兵 = 会话被回收
                    break
                self._write_frame("data: " + json.dumps(item, ensure_ascii=False) + "\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass                                  #客户端断线,正常收场
        finally:
            SESSIONS.pop(sid, None)

    def do_POST(self):
        head, _, query = self.path.partition("?")
        if not head.rstrip("/").endswith("/messages"):
            return self._send_plain(404, "not found")
        sid = _query_value(query, "sessionId")
        q = SESSIONS.get(sid)
        if q is None:
            return self._send_plain(404, "unknown or expired session")

        length = int(self.headers.get("Content-Length") or 0)
        try:
            msg = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return self._send_plain(400, "bad json")

        produces, reply = handle_message(msg)
        if produces:
            q.put(reply)                          #回应投到 GET 那条流上异步送回
        #POST 本身只回执,绝不把 result 塞进这个响应体 —— 这是 SSE 传输的命门
        self.send_response(202)
        self.end_headers()

    def _send_plain(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _query_value(query, key):
    for part in query.split("&"):
        k, _, v = part.partition("=")
        if k == key:
            return v
    return None


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)   #端口 0 = 系统随机分配
    #就绪报到(客户端读这行):GET 收货流的地址
    print(f"http://127.0.0.1:{server.server_port}/sse", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
