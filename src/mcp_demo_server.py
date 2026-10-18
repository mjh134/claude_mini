# -*- coding: utf-8 -*-
"""一个最小的 MCP 服务器 —— 只为让 mcp.py 有东西可连,不是给生产用的。

它被 mcp.py 当子进程拉起来,说的是 JSON-RPC 2.0:一行一条 JSON,从 stdin 读、往 stdout 写。
协议那头(握手、tools/list、tools/call)看 mcp.py 的模块注释,这里是同一份协议的服务器视角。

**铁律:stdout 只跑协议。** 想打日志一律 file=sys.stderr ——
往 stdout 多打一个字,客户端就会拿它去 json.loads 然后崩掉。
(本文件里所有 print 都写了 file=sys.stderr,只有 _reply 往 stdout 写。)

单独跑它没有意义:它会安静地等 stdin,你敲什么都不回显。它得由客户端拉起来。
"""
import json
import sys
from datetime import datetime

PROTOCOL_VERSION = "2024-11-05"

#stdin/stdout 显式锁成 UTF-8。MCP 的 stdio 传输按规范就是 UTF-8,
#而 Windows 上被重定向的 stdout 默认走系统 ANSI 代码页(中文机器是 GBK),
#不锁的话中文会以 GBK 发出去、客户端按 UTF-8 解,直接乱码。
#注意这跟"改 agent 控制台的输出编码"是两回事:这里是一条协议管道,不是给人看的终端。
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

#暴露给客户端的工具。字段名是 MCP 定的:inputSchema(驼峰),
#本项目的 TOOLS.py 用 input_schema(下划线),转换在客户端 mcp.py 的 as_tools() 里做。
TOOLS = [
    {
        "name": "echo",
        "description": "把传进来的文字原样返回。用来确认链路是通的。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文字"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "计算两个数相加,返回算式和结果。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "第一个加数"},
                "b": {"type": "number", "description": "第二个加数"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "now",
        "description": ("返回服务器这台机器上的当前时间。"
                        "这个时刻只有服务器知道 —— 用它来验证工具真的跑在另一个进程里。"),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def run_tool(name, args):
    """工具的实在内容。出错就抛,由调用方转成 isError 的回应。"""
    if name == "echo":
        return f"服务器收到:{args.get('text', '')}"
    if name == "add":
        #宽容一点:模型有时把数字写成字符串("3"),float() 能接住
        a, b = float(args["a"]), float(args["b"])
        return f"{a:g} + {b:g} = {a + b:g}"
    if name == "now":
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    raise KeyError(name)        #不认识的名字,由下面转成"没有这个工具"


def reply(msg_id, result=None, error=None):
    """往 stdout 写一条回应。这是本文件唯一碰 stdout 的地方。"""
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()          #不 flush 就卡在缓冲区里,客户端会一直等到超时


def handle(msg):
    """处理一条请求或通知。"""
    method = msg.get("method")
    msg_id = msg.get("id")
    print(f"  [server] ← {method}", file=sys.stderr)

    if method == "initialize":
        #回应里给出我们实际使用的协议版本、能力清单和自我介绍。
        #capabilities 里声明 tools,客户端才知道可以问我们要工具列表
        reply(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "claude-mini-demo", "version": "0.1"},
        })

    elif method == "notifications/initialized":
        pass                    #通知:没有 id,不该回。回了客户端会当成一条莫名其妙的回应

    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})

    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            text = run_tool(name, args)
            reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except KeyError:
            #注意:工具执行失败走的**不是** JSON-RPC 的 error,而是 result 里带 isError。
            #两者的区别是"协议层出错"和"工具本身出错",后者模型看得见、可以自己改参数重试
            reply(msg_id, {"content": [{"type": "text", "text": f"没有这个工具:{name}"}],
                           "isError": True})
        except Exception as e:
            reply(msg_id, {"content": [{"type": "text",
                                        "text": f"工具 {name} 执行出错:{type(e).__name__}: {e}"}],
                           "isError": True})

    elif msg_id is not None:
        #带 id 却不认识 = 请求,得回个"不支持",否则客户端一直等
        reply(msg_id, error={"code": -32601, "message": f"不支持的方法 {method}"})

    #没有 id 又不认识的 = 通知,按协议就该忽略


def main():
    print("[server] mcp_demo_server 启动,等待 stdin 上的消息", file=sys.stderr)
    while True:
        #用 readline 而不是 `for line in sys.stdin`:前者拿到的就是一行,语义明确;
        #后者会做预读,交互式场景下可能把消息扣在缓冲区里不放手
        line = sys.stdin.readline()
        if not line:            #EOF:客户端把 stdin 关了,收工
            print("[server] stdin 关闭,退出", file=sys.stderr)
            return
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"[server] 收到不是 JSON 的一行,忽略:{line[:80]}", file=sys.stderr)
            continue
        handle(msg)


if __name__ == "__main__":
    main()
