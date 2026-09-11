import json
import re


# 解析模型返回的 JSON,兼容被 ```json ... ``` 围栏包裹的情况
def parse_json(text):
    """解析模型返回的 JSON,兼容被 ```json 围栏包裹的情况。"""
    text = str(text).strip()
    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return json.loads(text)


#消息入库前清洗:去掉无法编码成 UTF-8 的孤立代理字符,
#否则 OpenAI SDK 序列化 messages 时抛 UnicodeEncodeError
def clean_text(s):
    """清洗文本,去掉无法编码成 UTF-8 的孤立代理字符。"""
    return str(s).encode("utf-8", "replace").decode("utf-8")
