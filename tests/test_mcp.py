#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP 双传输(decisions/0008)的回归测试:HTTP 语义单测 + stdio 老路回归锚。

    python -m pytest test_mcp.py -q         # pytest 模式
    python test_mcp.py                      # 直接运行模式

HTTP 单测全程走 httpx.MockTransport,**不出网**;另起一个真的 stdio 演示进程
验证传输重构没动老路;以及一个 http.server 版的 Streamable HTTP 演示服务器走端到端。
全程不烧 LLM、不碰凭证。
"""

#直跑自举:把 src/ 加进 import 路径(pytest 模式走 tests/conftest.py,不靠这两行)
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "src"))

import json
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

import mcp
from mcp import MCPClient, MCPError, MCPManager, _HttpTransport

DEMO_SERVER = str(Path(__file__).resolve().parents[1] / "src" / "mcp_demo_server.py")


def _rpc_ok(payload, headers=None):
    """从 jsonrpc 请求体里认出 method,回一条对应形状的成功响应(单测桩)。"""
    if hasattr(payload, "content"):            #MockTransport 直接挂它时收的是 Request,拆成体
        payload = payload.content
    req = json.loads(payload)
    if "id" not in req:                       #通知:202 受理,不产消息
        return httpx.Response(202, headers=headers or {})
    if req["method"] == "initialize":
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", **(headers or {})},
            json={"jsonrpc": "2.0", "id": req["id"],
                  "result": {"protocolVersion": "2025-03-26",
                             "serverInfo": {"name": "mock", "version": "1"}}},
        )
    return httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": req["id"], "result": {}},
    )


def _client_via_mock(handler, **connect_kwargs):
    """建一个走 MockTransport 的 MCPClient 并握手完;handler 就是"服务器的反应"。"""
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    c = MCPClient(timeout=5)
    c.connect({"url": "http://mock.local/mcp", **connect_kwargs,
               "_client": http_client})
    return c


#===== HTTP 语义:会话头 / SSE / 202 / 报错文案 =====

def test_http_handshake_captures_session_id():
    """initialize 响应头的 Mcp-Session-Id 被存下,并出现在后续每一个请求上。"""
    seen_headers = []

    def handler(request):
        if request.method == "POST":
            seen_headers.append(request.headers.get("Mcp-Session-Id"))
            #只在 initialize 的响应头上给会话 id(规范形状):后续请求必须自己带着走
            hello = b"initialize" in request.content
            return _rpc_ok(request.content,
                           headers={"Mcp-Session-Id": "sess-42"} if hello else None)
        return httpx.Response(200)

    c = _client_via_mock(handler)
    assert c.transport.session_id == "sess-42"
    c.list_tools()
    #initialized 通知 + tools/list 请求,都该署上会话
    assert "sess-42" in seen_headers


def test_http_tools_list_via_sse_stream():
    """回应是 SSE 流:多帧 data: 行、注释行、event: 头,都要正确攒出消息。"""
    def handler(request):
        req = json.loads(request.content)
        if req["method"] == "tools/list":
            body = (": keep-alive\n"
                    "event: message\n"
                    f"data: {json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': {'tools': [{'name': 'x'}]}})}\n"
                    "\n")
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  content=body.encode("utf-8"))
        return _rpc_ok(request.content)

    c = _client_via_mock(handler)
    assert c.list_tools() == [{"name": "x"}]


def test_http_notification_returns_202():
    """通知(无 id)回 202 空 body:不产消息、不报错、不卡住。"""
    c = _client_via_mock(_rpc_ok)
    c._notify("notifications/test")            #不抛 = 通过;队列里不该多出东西
    assert c.transport._inbox.empty()


def test_http_error_status_message_readable():
    """非 2xx:MCPError 带状态码和响应体前段,401 的"鉴权头不对"能一眼分辨。"""
    def handler(request):
        req = json.loads(request.content)
        return httpx.Response(401, headers={"Content-Type": "application/json"},
                              json={"error": "unauthorized: bad token"})

    c = MCPClient(timeout=5)
    with pytest.raises(MCPError) as e:
        c.connect({"url": "http://mock.local/mcp",
                   "_client": httpx.Client(transport=httpx.MockTransport(handler))})
    assert "401" in str(e.value) and "bad token" in str(e.value)


def test_sse_parser_frames_and_comments():
    """SSE 拆帧纯函数:多行 data 拼一条、空行分界、注释跳、残帧收。"""
    lines = [":ping", "data: {\"a\":1}", "",
             "data: {\"b\":", "data: 2}", "",
             "data: {\"c\":3}"]                #流断没赶上空行 —— 残帧也要
    msgs = _HttpTransport._parse_sse(lines)
    assert msgs == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_iter_sse_frames_recognizes_endpoint():
    """共享帧迭代器:认出 event:endpoint、无 event 行默认 message、跳注释、收残帧。

    旧版 SSE 传输靠这个从 GET 流上区分"首帧 endpoint(data 是 POST 地址、不是 JSON)"
    和后续 JSON-RPC 回应。_parse_sse 把 event 丢了,这个保留。
    """
    lines = [":keep-alive",
             "event: endpoint", "data: /messages?sessionId=abc", "",
             "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}", "",
             "data: {\"tail\":1}"]            #无空行收尾的残帧
    frames = list(mcp._iter_sse_frames(lines))
    assert frames == [
        ("endpoint", "/messages?sessionId=abc"),
        ("message", '{"jsonrpc":"2.0","id":1,"result":{}}'),
        ("message", '{"tail":1}'),
    ]


#===== tools/list 分页 =====

def _paged_handler(pages):
    """造一个按 cursor 链翻页的服务器桩:pages=[工具..., 工具..., ...],每页一个。"""
    cursors = [str(i + 1) for i in range(len(pages))]

    def handler(request):
        req = json.loads(request.content)
        if req["method"] == "tools/list":
            cursor = (req.get("params") or {}).get("cursor")
            idx = 0 if not cursor else int(cursor)
            body = {"tools": [{"name": f"t{idx}_{i}"} for i in range(len(pages[idx]))]}
            if idx + 1 < len(pages):
                body["nextCursor"] = cursors[idx]
            return httpx.Response(200, headers={"Content-Type": "application/json"},
                                  json={"jsonrpc": "2.0", "id": req["id"], "result": body})
        return _rpc_ok(request.content)
    return handler


def test_list_tools_merges_pages():
    """三页各 2 个:合并成 6 个,顺序按页。"""
    c = _client_via_mock(_paged_handler([[1, 2], [1, 2], [1, 2]]))
    tools = c.list_tools()
    assert len(tools) == 6
    assert [t["name"] for t in tools] == ["t0_0", "t0_1", "t1_0", "t1_1", "t2_0", "t2_1"]


def test_list_tools_page_cap(monkeypatch):
    """坏服务器每页都回同样的 nextCursor:封顶 MAX_TOOL_PAGES 停,不死循环。"""
    monkeypatch.setattr(mcp, "MAX_TOOL_PAGES", 3)
    calls = []

    def handler(request):
        req = json.loads(request.content)
        if req["method"] == "tools/list":
            calls.append(1)
            return httpx.Response(200, headers={"Content-Type": "application/json"},
                                  json={"jsonrpc": "2.0", "id": req["id"],
                                        "result": {"tools": [{"name": "x"}],
                                                   "nextCursor": "always"}})
        return _rpc_ok(request.content)

    c = _client_via_mock(handler)
    c.list_tools()
    assert len(calls) == 3                    #恰好封顶数,没多翻


#===== MCPManager:url 配置与插值 =====

def test_manager_url_and_command_are_exclusive(tmp_path):
    """url 与 command 同时出现 = 配置矛盾:该服务器进 failed 档,不阻断。"""
    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text(json.dumps({"servers": {
        "bad": {"url": "http://x/mcp", "command": "python", "args": ["y"]}}}),
        encoding="utf-8")
    m = MCPManager(config_path=cfg)
    assert m.servers == []
    assert any("互斥" in reason for _, reason in m.failed)


def test_manager_header_env_interpolation(tmp_path, monkeypatch):
    """${VAR} 从环境插值;缺失则跳过并点名缺哪个变量。"""
    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text(json.dumps({"servers": {
        "gh": {"url": "http://127.0.0.1:9/mcp",
               "headers": {"Authorization": "Bearer ${TEST_MCP_TOKEN}"}}}}),
        encoding="utf-8")

    monkeypatch.delenv("TEST_MCP_TOKEN", raising=False)
    m = MCPManager(config_path=cfg)
    assert m.servers == []
    assert any("TEST_MCP_TOKEN" in reason for _, reason in m.failed)   #报出缺的变量名

    #设上了就不再因配置被拦(连不上是另一回事:端口 9 没人听 → failed 记连接错)
    monkeypatch.setenv("TEST_MCP_TOKEN", "tk-123")
    m2 = MCPManager(config_path=cfg)
    assert all("环境变量未设置" not in reason for _, reason in m2.failed)


#===== 端到端:真起一个 Streamable HTTP 服务器,走真连接 =====

HTTP_DEMO = str(Path(__file__).resolve().parents[1] / "src" / "mcp_http_demo_server.py")


@pytest.fixture()
def http_demo_url():
    """拉起演示服务器,等它报到 URL,用完收掉。端口随机,不碰防火墙。"""
    proc = subprocess.Popen([sys.executable, HTTP_DEMO],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8")
    try:
        url = proc.stdout.readline().strip()
        assert url.startswith("http://"), f"演示服务器没报到:{url!r}"
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_http_e2e_full_flow(http_demo_url):
    """对真 HTTP 服务器走完整链路:握手建会话→翻页合并→SSE 形态的调用结果→收尾。"""
    with MCPClient(timeout=10) as c:
        c.connect({"url": http_demo_url})
        assert c.protocol_version == "2025-03-26"
        assert c.transport.session_id                 #会话真建立了,不是本地臆想
        #服务器分两页给(2+1):不翻页就只能看到 2 个 —— 这里必须拿到 3 个
        names = [t["name"] for t in c.list_tools()]
        assert names == ["echo", "add", "now"]
        #tools/call 的回应特意走 SSE 流:拍平后仍是纯文本
        assert c.call_tool("echo", {"text": "端到端"}) == "服务器收到:端到端"
        assert "42" in c.call_tool("add", {"a": 20, "b": 22})
        #工具层报错不破协议:isError 拍平成 ❌ 前缀
        assert c.call_tool("no_such", {}).startswith("❌")


def test_http_e2e_session_required(http_demo_url):
    """服务器对不带会话的带 id 请求回 404 —— 报错文案里要能看见 404。"""
    c = MCPClient(timeout=10)
    c.connect({"url": http_demo_url})
    #把会话抹掉,模拟过期:下一个请求就该被服务器拒掉
    c.transport.session_id = "forged"
    with pytest.raises(MCPError) as e:
        c.list_tools()
    assert "404" in str(e.value)


#===== 端到端:真起一个旧版 HTTP+SSE 服务器,走双端点传输 =====

SSE_DEMO = str(Path(__file__).resolve().parents[1] / "src" / "mcp_sse_demo_server.py")


@pytest.fixture()
def sse_demo_url():
    """拉起 SSE 演示服务器,等它报到 GET /sse 地址,用完收掉。端口随机。"""
    proc = subprocess.Popen([sys.executable, SSE_DEMO],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8")
    try:
        url = proc.stdout.readline().strip()
        assert url.startswith("http://"), f"SSE 演示服务器没报到:{url!r}"
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_sse_e2e_full_flow(sse_demo_url):
    """对真 SSE 服务器走完整链路:常驻 GET 报 endpoint → POST 只回执 → result 从 GET 流飘回。"""
    with MCPClient(timeout=10) as c:
        c.connect({"url": sse_demo_url, "transport": "sse"})
        assert c.protocol_version == "2024-11-05"          #SSE 路声明保守版(配套该传输)
        #会话不在 Mcp-Session-Id 头里,而在 endpoint 的 query 里 —— 验证真的拿到了 POST 目标
        assert c.transport._post_url and "sessionId=" in c.transport._post_url
        names = [t["name"] for t in c.list_tools()]
        assert names == ["echo", "add", "now"]
        assert c.call_tool("echo", {"text": "端到端"}) == "服务器收到:端到端"
        assert "42" in c.call_tool("add", {"a": 20, "b": 22})
        assert c.call_tool("no_such", {}).startswith("❌")


def test_manager_routes_sse_by_type(tmp_path, sse_demo_url):
    """配置里 "type":"sse" 就该走 _SseTransport(而不默认的 Streamable HTTP),工具照样并进来了。"""
    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text(json.dumps({"servers": {"sse": {"url": sse_demo_url, "type": "sse"}}}),
                   encoding="utf-8")
    m = MCPManager(config_path=cfg)
    assert not m.failed, m.failed
    assert isinstance(m.servers[0]["client"].transport, mcp._SseTransport)
    schemas, handlers = m.collect()
    names = [s["name"] for s in schemas]
    assert "sse__echo" in names and "sse__add" in names
    assert handlers["sse__echo"](text="你好") == "服务器收到:你好"
    m.close()


#===== stdio 回归锚:传输重构没动老路 =====

def test_stdio_transport_regressions():
    """真起演示子进程:握手→列工具(单页)→调用→工具层报错,全链路与旧行为一致。"""
    with MCPClient(timeout=10) as c:
        c.connect([sys.executable, DEMO_SERVER])
        assert c.protocol_version == "2024-11-05"          #老路声明没升(保守锚)
        names = [t["name"] for t in c.list_tools()]
        assert "echo" in names and "add" in names
        assert "收到" in c.call_tool("echo", {"text": "回归"})
        #未知工具 = result 里的 isError(协议层正常),拍平成 ❌ 前缀回给模型自己改
        assert c.call_tool("no_such_tool", {}).startswith("❌")


if __name__ == "__main__":
    #直跑模式:逐个调用,全过打 PASS(项目惯例,见 test_sandbox.py 同款)
    import tempfile
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        nargs = fn.__code__.co_varnames[:fn.__code__.co_argcount]
        if "tmp_path" in nargs:
            with tempfile.TemporaryDirectory() as td:
                kw = {p: Path(td) for p in ("tmp_path",)}
                if "monkeypatch" in nargs:
                    import pytest as _p
                    kw["monkeypatch"] = _p.MonkeyPatch()
                fn(**kw)
        elif "monkeypatch" in nargs:
            import pytest as _p
            fn(monkeypatch=_p.MonkeyPatch())
        else:
            fn()
        print(f"PASS {name}")
    print(f"\n{len(fns)} 项全过")
