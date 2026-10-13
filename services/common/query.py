"""解析 URL 查询串的小工具。

为什么需要它：
    Python 3.11 起，parse_qsl("", strict_parsing=True) 返回空列表；
    Python 3.10 却会抛出 ValueError。本项目的接口都用严格模式解析查询串，
    所以没带查询参数的请求在 3.10 上会被误判成"参数格式错误"。
    这里把空串单独处理，让各个 Python 版本的行为一致。
"""

from urllib.parse import parse_qsl


def parse_query_pairs(query, **options):
    """按 parse_qsl 的规则解析查询串，空串直接返回空列表。

    options 原样传给 parse_qsl，例如 strict_parsing、keep_blank_values、max_num_fields。
    """
    if query == "":
        return []
    return parse_qsl(query, **options)
