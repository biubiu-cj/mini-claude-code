# owned by cj
"""s14 MCP:连接外部 server、发现工具,并把它们并进 agent 的工具池。"""
import re

from toolschema import TOOLS


# -- 常量 --
# 模型工具名的合法字符集:规范化和改名都按它来
_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
# 模型侧工具名长度上限
MAX_TOOL_NAME_LEN = 64

# 授权只来自宿主配置,不看 server 自报的 readOnlyHint / destructiveHint。
# 未列出的外部工具一律 confirm。
MCP_HOST_POLICY = {
    ("docs", "search"): "allow",
    ("docs", "get_version"): "allow",
    ("deploy", "status"): "allow",
    ("deploy", "trigger"): "confirm",
}


# -- 全局状态 --
# server 名 -> 已连接的 MCPClient
mcp_clients: dict[str, "MCPClient"] = {}
# 前缀化工具名 -> "allow" / "confirm";由 assemble_tool_pool 就地更新
mcp_tool_policies: dict[str, str] = {}


class MCPClient:
    """一个 server 的进程内替身:保存它的工具定义和调用入口。"""
    def __init__(self, name):
        """记下 server 名,工具表和 handler 表都还空着。"""
        self.name = name
        self.tools = []
        self._handlers = {}

    def register(self, tool_defs, handlers):
        """对应 tools/list:登记工具定义,并校验每个工具都有 handler。"""
        names = [tool.get("name") for tool in tool_defs]
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Every MCP tool needs a non-empty name")
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate MCP tool name on server {self.name!r}")
        missing = [name for name in names if name not in handlers]
        if missing:
            raise ValueError(f"Missing MCP handlers: {', '.join(missing)}")
        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    def call_tool(self, tool_name, args):
        """对应 tools/call:调用一个工具,错误留在工具边界内返回。"""
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return str(handler(**args))
        except Exception as error:
            return f"MCP error: {type(error).__name__}: {error}"


def normalize_mcp_name(name):
    """把不适合模型工具名的字符换成下划线。"""
    normalized = _DISALLOWED_CHARS.sub("_", name or "")
    if not normalized:
        raise ValueError("MCP names cannot normalize to an empty string")
    return normalized



def _mock_server_docs():
    """造 docs server:一个只读搜索、一个只读取版本。"""
    server = MCPClient("docs")
    server.register(
        tool_defs=[
            {
                "name": "search",
                "description": "Search the documentation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "get_version",
                "description": "Get the documentation API version.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        },
    )
    return server


def _mock_server_deploy():
    """造 deploy server:一个会改状态的 trigger、一个只读的 status。"""
    server = MCPClient("deploy")
    server.register(
        tool_defs=[
            {
                "name": "trigger",
                "description": "Trigger a deployment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
                "annotations": {"destructiveHint": True},
            },
            {
                "name": "status",
                "description": "Check deployment status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        },
    )
    return server


MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}




def connect_mcp(name):
    """连接一个 server 并取回它的工具列表;已连过就复用。"""
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if not factory:
        return f"Unknown server '{name}'. Available: {', '.join(MOCK_SERVERS)}"
    server = factory()
    mcp_clients[name] = server
    names = ", ".join(tool["name"] for tool in server.tools)
    print(f"  [mcp] connected: {name} -> {names}")
    return (
        f"Connected to MCP server '{name}'. "
        f"Discovered {len(server.tools)} tools: {names}"
    )


def run_connect_mcp(name):
    """Agent 工具 connect_mcp 的 handler。"""
    return connect_mcp(name)



def assemble_tool_pool(base_handlers):
    """把内置工具和所有已连接 server 的工具并成一份 (tools, handlers)。"""
    tools = list(TOOLS)
    handlers = dict(base_handlers)
    policies = {}
    # 名字 -> 来源,用来在规范化撞名时报出到底是谁和谁撞了
    origins = {
        tool["function"]["name"]: f"built-in tool {tool['function']['name']!r}"
        for tool in tools
    }

    for server_name, server in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in server.tools:
            raw_name = tool_def["name"]
            safe_tool = normalize_mcp_name(raw_name)
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            if len(prefixed) > MAX_TOOL_NAME_LEN:
                raise ValueError(
                    f"MCP tool name is longer than {MAX_TOOL_NAME_LEN} "
                    f"characters: {prefixed}"
                )
            origin = f"MCP tool {server_name!r}/{raw_name!r}"
            if prefixed in origins:
                raise ValueError(
                    "MCP tool name collision after normalization: "
                    f"{prefixed!r} maps both {origins[prefixed]} and {origin}"
                )
            schema = tool_def.get("inputSchema", {})
            if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                raise ValueError(f"Invalid input schema for {origin}")
            origins[prefixed] = origin
            tools.append({
                "type": "function",
                "function": {
                    "name": prefixed,
                    "description": tool_def.get("description", ""),
                    "parameters": schema,
                },
            })
            # 默认参数在创建时绑定当前的 client 和原始工具名,
            # 否则循环里的 lambda 会全部指向最后一个工具
            handlers[prefixed] = (
                lambda *, client=server, tool=raw_name, **kwargs:
                client.call_tool(tool, kwargs)
            )
            policies[prefixed] = MCP_HOST_POLICY.get(
                (server_name, raw_name), "confirm"
            )

    # 就地更新,不重新绑定:permissions.py 持有的是这个 dict 对象
    mcp_tool_policies.clear()
    mcp_tool_policies.update(policies)
    return tools, handlers


def mcp_policy_for(tool_name):
    """查某个 MCP 工具在宿主侧的授权策略;未配置一律 confirm。"""
    return mcp_tool_policies.get(tool_name, "confirm")