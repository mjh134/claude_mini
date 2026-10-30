# -*- coding: utf-8 -*-
"""Streamable HTTP 版 MCP 演示服务器 —— 与 mcp_demo_server.py 同一套工具,另一条传输。

只依赖标准库(http.server),给测试和 `python mcp.py --http` 当靶子用。
演示的正是要测的四件事:

    · initialize 在**响应头**里给 Mcp-Session-Id,之后的请求不带 = 404
    · tools/list 分两页,第一页带 nextCursor —— 客户端不翻页就只能看到一半工具
    · 普通请求回 application/json;tools/call 特意回 **SSE 流**,演示多帧形态
    · 通知回 202 空 body

日志一律走 stderr —— 和 stdio 版同一条铁规矩,虽然 HTTP 传输没有"污染管道"之虞,
习惯保持一致。

用法:python mcp_http_demo_server.py    (就绪后第一行 stdout 打 URL,端口随机)
"""
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS_PAGE_1 = [
    {"name": "echo", "description": "把传进来的文字原样返回。用来确认链路是通的。",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "add", "description": "计算两个数相加,返回算式与结果。",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"},
                                                      "b": {"type": "number"}},
                     "required": ["a", "b"]}},
]
TOOLS_PAGE_2 = [
    {"name": "now", "description": "返回服务器这台机器上的当前时间。",
     "inputSchema": {"type": "object", "properties": {}}},
]

SESSIONS = set()          #已建立的会话 id;initialize 之外没这个 = 404


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


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):      #访问日志走 stderr,不打扰 stdout 的 URL 报到
        import sys
        print(f"[http-demo] {fmt % args}", file=sys.stderr, flush=True)

    #===== 出站小工具 =====

    def _json_response(self, status, body_obj, headers=None):
        body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _sse_response(self, msg_obj):
        """回一帧 SSE:注释 + data: + 空行收束 —— 客户端的 _parse_sse 就吃这个形状。"""
        payload = ("data: " + json.dumps(msg_obj, ensure_ascii=False) + "\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _reply(self, msg_id, result=None, error=None, sse=False, headers=None):
        msg = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        if sse:
            #注意 SSE 分支不携带自定义响应头(会话头只在 initialize 上演示)
            return self._sse_response(msg)
        return self._json_response(200, msg, headers)

    #===== 入站处理 =====

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return self._send_plain(400, "bad json")

        method = msg.get("method")
        msg_id = msg.get("id")                    #没有 id = 通知
        params = msg.get("params") or {}

        #会话检查:initialize 之外的带 id 请求必须署会话(通知回 202 就好,不强求)
        if method != "initialize" and msg_id is not None:
            if self.headers.get("Mcp-Session-Id") not in SESSIONS:
                return self._send_plain(404, "unknown or expired session")

        if method == "initialize":
            sid = uuid.uuid4().hex[:12]
            SESSIONS.add(sid)
            return self._reply(msg_id, {
                "protocolVersion": "2025-03-26",
                "serverInfo": {"name": "claude-mini-http-demo", "version": "0.1"},
                "capabilities": {"tools": {}},
            }, headers={"Mcp-Session-Id": sid})

        if msg_id is None:                        #通知:受理即可,不产消息
            self.send_response(202)
            self.end_headers()
            return

        if method == "tools/list":
            cursor = params.get("cursor")
            if not cursor:                        #第一页:两个工具,并告诉对方还有下一页
                return self._reply(msg_id, {"tools": TOOLS_PAGE_1, "nextCursor": "page2"})
            return self._reply(msg_id, {"tools": TOOLS_PAGE_2})   #第二页:到底,不给 cursor

        if method == "tools/call":
            name = params.get("name")
            try:
                text = run_tool(name, params.get("arguments") or {})
                #演示 SSE 形态:调用结果走事件流回,而不是单条 JSON
                return self._reply(msg_id, {"content": [{"type": "text", "text": text}],
                                            "isError": False}, sse=True)
            except KeyError:
                return self._reply(msg_id, {"content": [{"type": "text",
                                                         "text": f"没有这个工具:{name}"}],
                                            "isError": True})

        if msg_id is not None:
            return self._reply(msg_id, error={"code": -32601,
                                              "message": f"不支持的方法 {method}"})
        self.send_response(202)                   #不认识的通知:按协议忽略
        self.end_headers()

    def do_DELETE(self):
        sid = self.headers.get("Mcp-Session-Id")
        SESSIONS.discard(sid)
        self.send_response(200)
        self.end_headers()

    def _send_plain(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    import sys
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)   #端口 0 = 系统随机分配
    print(f"http://127.0.0.1:{server.server_port}/mcp", flush=True)   #就绪报到(客户端读这行)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
