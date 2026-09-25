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
                "Create or update the current agent's short-term plan. "
                "This is an in-memory checklist only: it does not create "
                "persistent task-board tasks, assign work, or start a teammate. "
                "Use create_task explicitly when work must be delegated, "
                "parallelized, dependency-managed, or survive the session."
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
SUBAGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "subagent",
        "description": (
            "Synchronously delegate one self-contained piece of work to a one-shot "
            "subagent with a fresh, empty context and wait for its final text. Use "
            "it when you only need the conclusion or implementation result. It is "
            "not a persistent background teammate; it cannot see this conversation, "
            "but it shares the persistent task board with you."
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

PARALLEL_SUBAGENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "parallel_subagents",
        "description": (
            "Run 2-8 independent one-shot subagents concurrently and return "
            "their final texts in input order. Use this for independent research "
            "or analysis that can run in parallel. The Lead waits for this batch "
            "to finish; this is not a persistent background teammate. Do not use "
            "it when subagents need to edit the same files or depend on each "
            "other's intermediate results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompts": {
                    "type": "array",
                    "description": "Independent, self-contained subagent briefs.",
                    "minItems": 2,
                    "maxItems": 8,
                    "items": {"type": "string", "minLength": 1},
                },
                "max_workers": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "description": "Maximum number of subagents running at once.",
                },
            },
            "required": ["prompts"],
        },
    },
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
                "Start a persistent teammate in a background thread. It runs its "
                "own agent loop without blocking the Lead, keeps context across "
                "tasks, can be messaged, claims ready tasks, and reports events "
                "back asynchronously. Use it for parallelizable or continuing "
                "work, not for an ordinary step the Lead can finish directly. "
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

# MCP 连接入口:必须是内置工具,否则模型永远迈不出第一步(鸡生蛋)。
# 只并入 TOOLS(lead),不进 SUB_TOOLS / TEAMMATE_TOOLS —— MCP 只给 lead。
CONNECT_TOOL = {
    "type": "function",
    "function": {
        "name": "connect_mcp",
        "description": "Connect to an MCP server and discover its tools.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": ["docs", "deploy"]},
            },
            "required": ["name"],
        },
    },
}


# 定时任务:运行时早就写好了,这里只是把入口交给模型
CRON_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "schedule_cron",
            "description": (
                "Schedule a prompt to run later. The cron expression has five "
                "fields: minute hour day month weekday (e.g. '30 14 * * *'). "
                "Set durable=true to survive a restart."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cron": {"type": "string"},
                    "prompt": {"type": "string"},
                    "recurring": {"type": "boolean"},
                    "durable": {"type": "boolean"},
                },
                "required": ["cron", "prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_crons",
            "description": "List all scheduled jobs.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_cron",
            "description": "Cancel a scheduled job by id.",
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        },
    },
]

COMPACT_TOOL = {
    "type": "function",
    "function": {
        "name": "compact",
        "description": (
            "Summarize earlier conversation and continue with compacted "
            "context. Use when the context is getting long."
        ),
        "parameters": {
            "type": "object",
            "properties": {"focus": {"type": "string"}},
        },
    },
}


WORKFLOW_TOOL = {
    "type": "function",
    "function": {
        "name": "Workflow",
        "description": (
            "Run a saved workflow by name. Pass the workflow's input in args. "
            "Workflows orchestrate several subagents; use resume_from_run_id to "
            "continue a previous run without redoing finished steps."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "args": {"type": "object"},
                "resume_from_run_id": {"type": "string"},
            },
            "required": ["name"],
        },
    },
}

TOOLS = [*BASE_TOOLS, *TASK_SYSTEM_TOOLS, SUBAGENT_TOOL,
         PARALLEL_SUBAGENTS_TOOL, *TEAM_TOOLS,
         CONNECT_TOOL, *CRON_TOOLS, COMPACT_TOOL, WORKFLOW_TOOL]
