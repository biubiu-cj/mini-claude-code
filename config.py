import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


def _make_console_safe():
    """控制台编码兜底。

    Windows 控制台常是 GBK,而模型的回答和日志里随时可能出现 GBK 编不出的字符
    (emoji 之类)。默认 print 会抛 UnicodeEncodeError,把整个 agent loop 掀翻。
    这里只把不可编码的字符降级,不影响本来就能正常输出的内容。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


_make_console_safe()

load_dotenv()
API_KEY = os.getenv("DEEPSEEK_API_KEY")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL")
MODEL = os.getenv("MODEL_ID")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

# 尚无计划时,连续多少次"非-todo 工具调用"后提醒模型先建计划
REMINDER_EVERY = 3
# 已有计划时,连续多少次"非-todo 工具调用"后提醒模型对照计划自检是否偏离
PLAN_CHECK_EVERY = 20

WORKDIR = Path.cwd()

# 基础设施路径:固定在脚本所在目录,不随启动目录变化。
# WORKDIR 仍然是"工作区",文件工具和 safe_path 都跟着它走。
BASE_DIR = Path(__file__).resolve().parent


# 任务归属身份:由运行时注入,不由模型提供。
# 模型自己填的 owner 不构成任何约束,父/子 agent 都填默认值会让归属检查形同虚设
MAIN_OWNER = "main"

# 出错重试:429(限流)和 529(过载)都按指数退避重试
MAX_RETRIES = 4

# 连续这么多次 529 之后,切到备用模型
MAX_CONSECUTIVE_529 = 2

# 备用模型;留空表示不切
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID", "")

# 每次调模型的输出上限,以及"顶到上限"后升级到多少
DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000

# 升级之后仍然顶到上限,最多再要求模型"继续写"几次;
# 没有这个上限就会无限 continue(模型每次都在写长输出时死循环)
MAX_RECOVERY_RETRIES = 3
