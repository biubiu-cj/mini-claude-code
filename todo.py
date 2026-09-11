import json


#任务列表
class TodoManager:
    """当前请求的步骤清单:只活在这次会话里,不落盘、不跨会话。"""

    VALID_STATUS = {"pending", "in_progress", "completed"}

    #初始化
    def __init__(self):
        """建一个空的计划清单。"""
        self.items = []  #这个就是任务列表

    #校验todos的格式,并更新任务列表
    def update(self, todos: list | str) -> str:
        """校验 todos 格式并覆盖任务列表,返回渲染后的文本。"""
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError:
                raise ValueError("Error: Invalid JSON format")
        if not isinstance(todos, list):
            raise ValueError("Error: Todos must be a list")
        validated_todos = []
        for todo in todos:
            if not isinstance(todo, dict):
                raise ValueError("Error: Each todo must be a dictionary")
            content = todo.get("content")
            status = todo.get("status")
            if not content:
                raise ValueError("Error: Each todo must have 'content'")
            if status not in self.VALID_STATUS:
                raise ValueError(f"Error: Invalid status '{status}'")
            validated_todos.append({"content": content, "status": status})
        self.items = validated_todos
        return self.render()

    #将状态渲染为文本
    def render(self):
        """把任务列表渲染成带状态标记的多行文本。"""
        lines = []
        for todo in self.items:
            status = todo["status"]
            content = todo["content"]
            if status == "pending":
                lines.append(f"[ ] {content}")
            elif status == "in_progress":
                lines.append(f"[-] {content}")
            elif status == "completed":
                lines.append(f"[x] {content}")
        return "\n".join(lines)
