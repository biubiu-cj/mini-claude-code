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

class LazyClient:
    """惰性 OpenAI 客户端:首次真正调用时才读 .env 建连接。

    原来这里在 import 阶段就 `OpenAI(...)`,于是只测纯函数的单测也被迫依赖
    .env 存在(没配 key 时构造就抛错)。改成惰性之后,单测不碰客户端就完全不
    需要密钥,将来上 CI 也不用先注入假 key。
    """

    def __init__(self):
        """只记状态,不建连接。"""
        self._client = None

    def _connect(self):
        """首次访问时创建真正的客户端。"""
        if self._client is None:
            self._client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        return self._client

    def __getattr__(self, name):
        """把属性访问转发给真正的客户端,调用方写法完全不变。"""
        return getattr(self._connect(), name)


client = LazyClient()

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

# 主循环刹车:非交互回合(定时 / 队友 / 后台唤醒)的兜底上限。
# 主循环是全部循环里唯一没有上限的那个,而它偏偏最可能长时间跑。
# 刻意**不**约束用户手动提问的回合 —— 那种场合的正解是"能被干净取消",
# 不是"到点自尽",跑到第 60 轮被砍掉是另一种难受。
MAX_TURNS = 60
MAX_SECONDS = 1800
