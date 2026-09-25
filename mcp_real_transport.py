# owned by cj
"""接真实 MCP server:stdio + JSON-RPC 2.0。

这是 mcp_client.py 里进程内替身(MCPClient)的真实版对照。接口形状刻意保持一致
—— 连上后拿到工具表,调用时走 `call_tool(name, args)` —— 所以 assemble_tool_pool()
一行都不用改。差别只在"怎么说话",不在"说话的内容"。

本文件给了三种写法:
  1. StdioMCPClient —— 手写 JSON-RPC,零依赖,能看到协议全貌(强烈建议先读这个)
  2. SdkMCPClient   —— 官方 mcp SDK 的封装(生产环境推荐)
  3. 注意事项        —— 换成真实 server 后,你的代码哪里必须改(见文件末尾)

直接运行可自测:
    python mcp_real_transport.py

协议版本说明(重要):
  2025-11-25 及更早(legacy):先 `initialize` 握手,再 `notifications/initialized`,
                              之后才能发请求。本文件的写法属于这一代。
  2026-07-28 及更新(modern):握手被整个删除,协议改成无状态 —— 每个请求在自己的
                              `_meta` 里带上 protocolVersion / clientInfo,
                              客户端可以调 `server/discover` 探能力。
                              连 stdio 也走这套。
  两代并存,服务端可能同时支持。实际接入要么锁一个版本,要么按 `server/discover`
  返回的清单挑一个双方都支持的。
"""
import asyncio
import json
import os
import queue
import subprocess
import sys
import threading

# 我们对外声称支持的协议版本。真实实现应当从配置读,并处理服务端的版本回绝。
PROTOCOL_VERSION = "2025-06-18"

# 单次请求的默认超时。真实 server 可能是远程的,不设超时会把 agent 挂死。
DEFAULT_TIMEOUT = 30.0


class MCPError(RuntimeError):
    """MCP 层错误:JSON-RPC error 响应,或传输故障(超时 / 管道断开)。"""


def flatten_content(blocks):
    """把 tools/call 返回的 content 块列表拼成一段文本。

    真实 server 返回的是块列表(可能混有 image / resource),不是纯字符串 ——
    这是和你现在的模拟 server 最直接的一处差别。
    """
    parts = []
    for block in blocks or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "resource":
            uri = (block.get("resource") or {}).get("uri", "?")
            parts.append(f"[resource {uri}]")
        else:
            parts.append(f"[{kind} content omitted]")
    return "\n".join(part for part in parts if part)


# ——————————————————————————————————————————————————————————————
# 写法一:手写 stdio JSON-RPC(零依赖)
# ——————————————————————————————————————————————————————————————
class StdioMCPClient:
    """把 server 当子进程拉起来,用换行分隔的 JSON-RPC 2.0 说话。

    stdio transport 的线格式就是"一行一个 JSON 对象",没有 Content-Length 头
    (那是 LSP 的做法,别搞混)。
    """

    def __init__(self, name, command, args=(), env=None, timeout=DEFAULT_TIMEOUT):
        """拉起 server 子进程,起后台读写线程,然后完成握手和工具发现。"""
        self.name = name
        self.timeout = timeout
        self.tools = []          # tools/list 的结果,原样保留 inputSchema
        self._handlers = {}
        self._next_id = 0
        self._pending = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stderr_tail = []

        self._process = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, **(env or {})},
            text=True, encoding="utf-8", errors="replace",
            bufsize=1,     # 行缓冲:一条消息一行
        )
        # 必须持续读 stderr:管道缓冲区写满会让 server 卡死在 write 上
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._handshake()

    # -- 传输层 --
    def _drain_stderr(self):
        """持续排空 stderr,只留最后几十行用于报错。"""
        for line in self._process.stderr:
            self._stderr_tail.append(line.rstrip())
            del self._stderr_tail[:-40]

    def _read_loop(self):
        """后台线程:按行读 stdout,把响应按 id 投递给等待方。"""
        for line in self._process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue          # server 的非协议输出,忽略
            request_id = message.get("id")
            if request_id is None:
                continue          # 通知,或 server 反向发起的请求(本章不处理)
            with self._lock:
                mailbox = self._pending.pop(request_id, None)
            if mailbox is not None:
                mailbox.put(message)

    def _send(self, payload):
        """把一条消息写成一行 JSON 送进 server 的 stdin。"""
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._write_lock:
            if self._process.poll() is not None:
                raise MCPError(
                    f"{self.name}: server exited "
                    f"(rc={self._process.returncode}) {self.stderr_tail()}"
                )
            try:
                self._process.stdin.write(line)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise MCPError(f"{self.name}: server pipe closed: {error}") from error

    def _request(self, method, params, timeout=None):
        """发一条请求并等它的响应;超时或 server 报错都抛 MCPError。"""
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            mailbox = queue.Queue(maxsize=1)
            self._pending[request_id] = mailbox
        self._send({"jsonrpc": "2.0", "id": request_id,
                    "method": method, "params": params})
        try:
            response = mailbox.get(timeout=timeout or self.timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(request_id, None)
            raise MCPError(f"{self.name}: {method} timed out "
                           f"after {timeout or self.timeout}s") from None
        if "error" in response:
            error = response["error"]
            raise MCPError(f"{self.name}: {method} -> "
                           f"{error.get('code')} {error.get('message')}")
        return response.get("result") or {}

    def stderr_tail(self):
        """取 server 最后几行 stderr,拼成一句便于报错。"""
        tail = [line for line in self._stderr_tail if line][-5:]
        return " | ".join(tail) if tail else "(no stderr)"

    def alive(self):
        """server 进程是否还活着。"""
        return self._process.poll() is None

    # -- 协议层 --
    def _handshake(self):
        """initialize → notifications/initialized。顺序不能反。"""
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "learn-claude-code", "version": "0.1"},
        })
        server = result.get("serverInfo") or {}
        print(f"  [mcp] {self.name} ready: {server.get('name')} "
              f"{server.get('version')} (protocol {result.get('protocolVersion')})")
        # initialized 是通知:没有 id,不需要也收不到响应
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.discover()

    def discover(self):
        """对应 tools/list:拉回工具定义并建好 handler 表。"""
        self.tools = self._request("tools/list", {}).get("tools", [])
        self._handlers = {
            tool["name"]: self._make_handler(tool["name"]) for tool in self.tools
        }
        return self.tools

    def _make_handler(self, tool_name):
        """造一个绑定了工具名的 handler,签名与 MCPClient.call_tool 一致。"""
        def handler(**kwargs):
            """调用这个 MCP 工具,把 content 块拼成文本返回。"""
            return self.call_tool(tool_name, kwargs)

        return handler

    def call_tool(self, tool_name, args):
        """对应 tools/call:错误留在工具边界内,不抛给 agent loop。"""
        known = {tool["name"] for tool in self.tools}
        if tool_name not in known:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            result = self._request("tools/call",
                                   {"name": tool_name, "arguments": args})
        except MCPError as error:
            return f"MCP error: {error}"
        text = flatten_content(result.get("content"))
        if result.get("isError"):
            return f"MCP error: {text or 'tool reported an error'}"
        return text or "(no output)"

    def close(self):
        """关掉 server:先关 stdin 让它自己退,不退就杀。"""
        if self._process.poll() is not None:
            return
        try:
            self._process.stdin.close()
            self._process.wait(timeout=5)
        except Exception:
            self._process.kill()


# ——————————————————————————————————————————————————————————————
# 写法二:官方 SDK(推荐)
# ——————————————————————————————————————————————————————————————
class SdkMCPClient:
    """官方 mcp SDK 的同步封装。

    SDK 是 async 的,而这个 agent 是同步的,所以这里把 asyncio 事件循环放到一个
    专用线程里,再用 run_coroutine_threadsafe 把协程丢过去、同步等结果。
    协议版本协商、通知、取消、超时都交给 SDK,别自己重造。

    需要先安装:  pip install mcp
    它的接口和 StdioMCPClient 完全一致,可以直接替换。
    """

    def __init__(self, name, command, args=(), env=None, timeout=DEFAULT_TIMEOUT):
        """起专用事件循环线程,在里面建连接并发现工具。"""
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self.name = name
        self.timeout = timeout
        self.tools = []
        self._handlers = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=command, args=list(args),
            env={**os.environ, **(env or {})},
        )

        async def connect():
            """在事件循环里建立 stdio 连接并完成握手 + 工具发现。"""
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            await self._session.initialize()
            response = await self._session.list_tools()
            self.tools = [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.inputSchema,
                    # 注意:annotations 是 server 自己声明的,只能当提示,
                    # 不能当授权依据 —— 见文件末尾第 2 条。
                    "annotations": getattr(tool, "annotations", None),
                }
                for tool in response.tools
            ]
            self._handlers = {
                tool["name"]: self._make_handler(tool["name"]) for tool in self.tools
            }

        self._submit(connect())

    def _submit(self, coro):
        """把一个协程交给专用事件循环执行,并在当前线程同步等结果。"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=self.timeout)

    def _make_handler(self, tool_name):
        """造一个绑定了工具名的 handler。"""
        def handler(**kwargs):
            """同步调用这个 MCP 工具,把返回内容拼成文本。"""
            return self.call_tool(tool_name, kwargs)

        return handler

    def call_tool(self, tool_name, args):
        """调用工具;SDK 的类型化结果在这里降级成文本。"""
        try:
            result = self._submit(
                self._session.call_tool(tool_name, arguments=args)
            )
        except Exception as error:
            return f"MCP error: {type(error).__name__}: {error}"
        blocks = []
        for item in result.content or []:
            blocks.append({"type": "text", "text": getattr(item, "text", "")}
                          if getattr(item, "type", None) == "text"
                          else {"type": getattr(item, "type", "unknown")})
        text = flatten_content(blocks)
        if getattr(result, "isError", False):
            return f"MCP error: {text or 'tool reported an error'}"
        return text or "(no output)"

    def close(self):
        """关掉 session 与子进程,再停掉事件循环线程。"""
        try:
            self._submit(self._stack.aclose())
        except Exception:
            pass
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)


# ——————————————————————————————————————————————————————————————
# 自测:一个最小的真实 MCP server(stdio),用来验证上面的客户端
# ——————————————————————————————————————————————————————————————
DEMO_SERVER = r'''
import json
import sys

TOOLS = [
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "echo",
        "description": "Echo the message back.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
]


def send(payload):
    """把一条 JSON-RPC 消息写成一行 JSON 送出去。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle_call(params):
    """执行一次 tools/call,返回 content 块列表。"""
    name = params.get("name")
    args = params.get("arguments") or {}
    try:
        if name == "add":
            text = str(args["a"] + args["b"])
        elif name == "echo":
            text = args["message"]
            print("echo tool was called", file=sys.stderr)   # 故意写 stderr 试探排空
        else:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}],
                    "isError": True}
    except Exception as error:
        return {"content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}],
                "isError": True}
    return {"content": [{"type": "text", "text": text}], "isError": False}


for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    message = json.loads(raw)
    method = message.get("method")
    if "id" not in message:
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "demo-server", "version": "0.1.0"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = handle_call(message.get("params") or {})
    else:
        send({"jsonrpc": "2.0", "id": message["id"],
              "error": {"code": -32601, "message": f"method not found: {method}"}})
        continue
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})
'''


def demo_handwritten():
    """用自带的 demo server 跑一遍手写客户端。"""
    import tempfile
    from pathlib import Path

    print("=== 写法一:手写 stdio JSON-RPC ===")
    with tempfile.TemporaryDirectory() as tmp:
        server_path = Path(tmp) / "demo_server.py"
        server_path.write_text(DEMO_SERVER, encoding="utf-8")
        # 用 sys.executable 而不是裸 "python":后者在部分机器上是商店占位符
        client = StdioMCPClient("demo", sys.executable, [str(server_path)])
        try:
            print("  工具表      :", [tool["name"] for tool in client.tools])
            print("  inputSchema :", json.dumps(
                client.tools[0]["inputSchema"], ensure_ascii=False))
            print("  add(2, 3)   :", client.call_tool("add", {"a": 2, "b": 3}))
            print("  echo('hi')  :", client.call_tool("echo", {"message": "hi"}))
            print("  漏参数      :", client.call_tool("add", {"a": 1}))
            print("  不存在的工具:", client.call_tool("nope", {}))
            print("  server 存活 :", client.alive())
        finally:
            client.close()
        print("  close 后存活:", client.alive())


def demo_pool_shape():
    """证明真实工具定义能直接喂给现有的 assemble_tool_pool(无需改一行)。"""
    print("=== 与现有代码的接口对齐 ===")
    from toolschema import TOOLS

    sample = StdioMCPClient.__new__(StdioMCPClient)   # 只为拿结构,不真连
    sample.tools = [{
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {"type": "object", "properties": {}},
    }]
    wrapped = {
        "type": "function",
        "function": {
            "name": "mcp__demo__add",
            "description": sample.tools[0]["description"],
            "parameters": sample.tools[0]["inputSchema"],
        },
    }
    builtin = {tool["function"]["name"] for tool in TOOLS}
    print("  inputSchema -> function.parameters 直接映射,无需转换")
    print("  撞名检查用的 builtin 名字数:", len(builtin))
    print("  包装后的形状:", json.dumps(wrapped, ensure_ascii=False))


# ——————————————————————————————————————————————————————————————
# 换成真实 server 后,和现在的模拟实现有哪些硬差别
# ——————————————————————————————————————————————————————————————
REAL_WORLD_NOTES = [
    "工具名不能预先写死。MCP_HOST_POLICY 用 (server, tool) 做键,但真实 server 的"
    "工具要连上才知道 —— 静态白名单写不出来。改成按 server 给默认策略"
    "(如 {'docs': 'allow', 'deploy': 'confirm'}),或者运行时让用户逐条批准并记住。",

    "永远不要拿 server 自报的 annotations 当授权依据。readOnlyHint / destructiveHint"
    " 是 server 自己写的,不可信;只能当「提示用户」的素材,不能当放行理由。",

    "server 会死。子进程可能崩、可能被 OOM 杀掉。一旦 alive() 为假,必须把"
    " mcp_clients[name] 摘掉,否则模型还会看到一堆必然失败的工具,白烧轮次。",

    "tools/call 返回的是 content 块列表,不是字符串(可能混有 image / resource)。"
    " 必须拍平成文本(见 flatten_content),否则 tool result 会塞进一个 dict 的 repr。",

    "connect_mcp 从「瞬时且不会失败」变成「要几秒、可能失败」。它现在是 spawn 子进程 +"
    " 网络往返,必须在工具层兜住失败(报错给模型)并设超时,不能让它挂死 agent。",

    "server 配置要从文件读,不要硬编码 dict。真实形态类似 Claude Code 的 .mcp.json:"
    " {'mcpServers': {'docs': {'command': 'npx', 'args': [...]}}}。",

    "初始化是有顺序的:initialize 必须最先,拿到结果后才能发"
    " notifications/initialized,之后 tools/list 才有效。顺序错了 server 会直接回绝。",

    "协议版本要真的协商。2026-07-28 起握手被删除、协议改为无状态"
    "(每个请求在 _meta 里带版本,并提供 server/discover);服务端拒绝版本时错误码"
    " 是 -32022 并附上它支持的清单。写死一个版本号迟早会踩。",

    "要想清楚 stdio 子进程的归属:它跑在 agent 进程里,agent 退出时子进程可能变孤儿。"
    " close() 里要显式关 stdin 并 wait,必要时 kill。",
]


if __name__ == "__main__":
    demo_handwritten()
    demo_pool_shape()
    print()
    print("=== 换成真实 server 后,你的代码必须改的地方 ===")
    for index, note in enumerate(REAL_WORLD_NOTES, start=1):
        print(f"{index}. {note}")

