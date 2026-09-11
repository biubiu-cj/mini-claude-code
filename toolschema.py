BASE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a shell command. Commands are killed after 30 seconds; "
                "use run_in_background for longer work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute."
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "Run a long-running command in the background: "
                            "returns immediately with a task id, and the "
                            "output arrives later as a task notification. "
                            "Do not wait for it."
                        )
                    }
                },
                "required": ["command"]
            }
        }
    },
        # 2. read_file
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path of the file to read."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read."
                    }
                },
                "required": ["path"]
            }
        }
    },
    # 3. write_file
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates the file if it does not exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path of the file to write."
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write into the file."
                    }
                },
                "required": ["path", "content"]
            }
        }
    },

    # 4. edit_file
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace text in a file once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The path of the file to edit."
                    },
                    "old_text": {
                        "type": "string",
                        "description": "The exact text to replace."
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The new text that replaces the old text."
                    }
                },
                "required": ["path", "old_text", "new_text"]
            }
        }
    },

    # 5. glob
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files by a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The glob pattern used to find files."
                    }
                },
                "required": ["pattern"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": (
                "Create or update the task plan. "
                "Use this for complex tasks and update "
                "the status as work progresses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "The complete current todo list.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "Task description."
                                },
                                "status": {
                                    "type": "string",
                                    "enum": [
                                        "pending",
                                        "in_progress",
                                        "completed"
                                    ]
                                }
                            },
                            "required": [
                                "content",
                                "status"
                            ]
                        }
                    }
                },
                "required": ["todos"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "Load the full instructions of an available skill "
                "when the current task requires it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "The name of the skill to load."
                        )
                    }
                },
                "required": ["name"]
            }
        }
    }
]



#父系统调用子系统的工具
TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "task",
        "description": (
            "Delegate one self-contained piece of work to a subagent that runs with "
            "a fresh, empty context and returns only its final text. Use it when the "
            "intermediate output would flood your context and you only need the "
            "conclusion. The subagent cannot see this conversation, but it shares "
            "the persistent task board with you."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "A self-contained brief. The subagent sees nothing else."
                    )
                }
            },
            "required": ["prompt"]
        }
    }
}

TASK_SYSTEM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a new persistent task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Short task title.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed task description.",
                    },
                },
                "required": ["subject"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": "Add dependency task IDs to a pending task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID of the target task.",
                    },
                    "add_blocked_by": {
                        "type": "array",
                        "items": {
                            "type": "string",
                        },
                        "description": "Task IDs that must be completed first.",
                    },
                },
                "required": [
                    "task_id",
                    "add_blocked_by",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "List all persistent tasks.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task",
            "description": "Get complete details of one task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID of the task.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claim_task",
            "description": "Claim a pending task when all dependencies are completed. The task becomes owned by the calling agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID of the task to claim.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Complete an in-progress task and report newly unblocked tasks. Only the agent that claimed the task can complete it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID of the task to complete.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
]

# Lead 专属团队工具
TEAM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "spawn_teammate",
            "description": (
                "Spawn a persistent teammate that runs its own agent loop in a "
                "separate thread. It keeps its context across tasks, can be "
                "messaged, and reports results back to you automatically. "
                "Propose the team to the user and wait for confirmation first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Teammate name."},
                    "role": {"type": "string", "description": "Short role label."},
                    "prompt": {"type": "string", "description": "Self-contained brief."},
                    "task_id": {
                        "type": "string",
                        "description": "Task to claim before starting.",
                    },
                    "require_plan": {
                        "type": "boolean",
                        "description": "Require a plan approval before workspace changes.",
                    },
                },
                "required": ["name", "role", "prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_teammates",
            "description": "List active teammates and their status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send a message to an active teammate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Teammate name."},
                    "content": {"type": "string", "description": "Message body."},
                },
                "required": ["to", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_shutdown",
            "description": "Ask a teammate to finish its current step and shut down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "teammate": {"type": "string"},
                },
                "required": ["teammate"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_plan",
            "description": "Require a teammate to submit a plan before workspace changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "teammate": {"type": "string"},
                    "task": {"type": "string", "description": "What the plan is for."},
                },
                "required": ["teammate", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "review_plan",
            "description": "Approve or reject a teammate's submitted plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "string"},
                    "approve": {"type": "boolean"},
                    "feedback": {"type": "string"},
                },
                "required": ["request_id", "approve"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_worktree",
            "description": "Create and bind a dedicated worktree for a pending task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "task_id": {"type": "string"},
                },
                "required": ["name", "task_id"],
            },
        },
    },
]

# 队友专属工具:注意没有 create_task / update_task / get_task ——
# 团队运行期间只有 Lead 能改任务图结构
TEAMMATE_TOOLS = [
    # 复用 LEAD 基础工具里那 5 个文件/Shell 工具
    *[tool for tool in BASE_TOOLS if tool["function"]["name"] in {
        "bash", "read_file", "write_file", "edit_file", "glob",
    }],
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send an intermediate message to 'lead' or an active teammate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["to", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_plan",
            "description": "Submit a work plan for Lead approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {"type": "string"},
                },
                "required": ["plan"],
            },
        },
    },
    # 只挑任务工具里的这三个
    *[
        tool for tool in TASK_SYSTEM_TOOLS
        if tool["function"]["name"] in {"list_tasks", "claim_task", "complete_task"}
    ],
]


# 子 agent 也拿得到任务图:owner 就是用来记录"哪个 agent 负责"的,
# 多 agent 协作建立在共享的任务图之上
SUB_TOOLS = [*BASE_TOOLS, *TASK_SYSTEM_TOOLS]

TOOLS = [*BASE_TOOLS, *TASK_SYSTEM_TOOLS, TASK_TOOL, *TEAM_TOOLS]
