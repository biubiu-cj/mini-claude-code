# owned by cj

import subprocess
import threading


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """判断这次工具调用是否要放到后台执行。"""
    return tool_name == "bash" and tool_input.get("run_in_background") is True


def _format_bash_result(command, stdout, stderr, returncode):
    """把后台 bash 的结果格式化成一段文本。"""
    return f"$ {command}\nExit code: {returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"


def _run_bash_process(command):
    """同步跑完命令,返回 (stdout, stderr, returncode)。"""
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return result.stdout, result.stderr, result.returncode


class BackgroundManager:
    """管理后台 bash 任务:启动、记录状态、收集完成通知。"""

    def __init__(self):
        """初始化任务表、结果表和锁。"""
        self.tasks = {}
        self.results = {}
        self._ready = []
        self._lock = threading.Lock()
        self._counter = 0

    def start(self, command: str) -> str:
        """启动一个后台任务,立刻返回任务 id。"""
        with self._lock:
            self._counter += 1
            task_id = f"bg_{self._counter:04d}"
            self.tasks[task_id] = {"id": task_id, "command": command, "status": "running"}

        thread = threading.Thread(target=self._run, args=(task_id, command), daemon=True)
        thread.start()

        return task_id

    def _run(self, task_id: str, command: str):
        """在子线程里执行命令,结束后登记状态和结果。"""
        # 后台任务故意不设 timeout:跑得久正是它存在的理由
        try:
            stdout, stderr, returncode = _run_bash_process(command)
            status = "completed" if returncode == 0 else "failed"
            result = _format_bash_result(command, stdout, stderr, returncode)
        except Exception as error:
            status = "failed"
            result = f"Background task failed:\n{error}"

        with self._lock:
            self.tasks[task_id]["status"] = status
            self.results[task_id] = result
            self._ready.append(task_id)

    def collect(self) -> list[str]:
        """取走已完成任务的通知文本,每个任务只通知一次。"""
        with self._lock:
            ready_ids = list(self._ready)
            self._ready.clear()

        notifications = []

        for task_id in ready_ids:
            with self._lock:
                task = self.tasks.get(task_id)
                result = self.results.get(task_id)

            if task is None or result is None:
                continue

            notifications.append(
                "<task_notification>\n"
                f"Task ID: {task_id}\n"
                f"Status: {task['status']}\n"
                f"Command: {task['command']}\n\n"
                f"{result}\n"
                "</task_notification>"
            )
        return notifications

    def running(self) -> list[str]:
        """返回还在运行的任务描述,每行 `id: command`。"""
        with self._lock:
            return [
                f"{task['id']}: {task['command']}"
                for task in self.tasks.values()
                if task["status"] == "running"
            ]


BACKGROUND = BackgroundManager()
