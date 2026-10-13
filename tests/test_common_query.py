"""查询串解析的版本兼容测试。

Python 3.10 在严格模式下解析空串会报错，新版本返回空列表。
parse_query_pairs 统一成新版本的行为，其余输入和 parse_qsl 完全一致。
"""
import unittest

from services.common.query import parse_query_pairs


STRICT = dict(keep_blank_values=True, strict_parsing=True, max_num_fields=2)


class ParseQueryPairsTests(unittest.TestCase):
    def test_empty_query_means_no_parameters(self):
        self.assertEqual(parse_query_pairs("", **STRICT), [])

    def test_normal_query_is_parsed(self):
        self.assertEqual(parse_query_pairs("after=3&limit=20", **STRICT), [("after", "3"), ("limit", "20")])

    def test_malformed_query_is_still_rejected(self):
        for query in ("&", "a=1&", "flag"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                parse_query_pairs(query, **STRICT)

    def test_too_many_fields_is_still_rejected(self):
        with self.assertRaises(ValueError):
            parse_query_pairs("a=1&b=2&c=3", **STRICT)


if __name__ == "__main__":
    unittest.main()
