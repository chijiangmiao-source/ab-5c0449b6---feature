"""DSL 解析与录入限制测试。"""

import unittest

from app.drill import parse_all
from app.sample import sample_drill


class ParseTest(unittest.TestCase):
    def test_sample_parses_cleanly(self):
        spec, errors = parse_all(**sample_drill())
        self.assertEqual(errors, [])
        self.assertEqual(spec["routers"], ["A", "B", "C", "D"])
        self.assertEqual(len(spec["links"]), 8)
        self.assertEqual(len(spec["events"]), 14)

    def test_defaults_applied(self):
        spec, errors = parse_all("A B", "A B", "announce A A prefix=A")
        self.assertEqual(errors, [])
        self.assertEqual(spec["links"][0]["localpref"], 100)
        self.assertEqual(spec["links"][0]["epoch"], 1)
        self.assertEqual(spec["events"][0]["path"], ["A"])
        self.assertEqual(spec["events"][0]["epoch"], 1)

    def test_arrow_syntax(self):
        spec, errors = parse_all("A B", "A -> B pref=50 epoch=3", "")
        self.assertEqual(errors, [])
        self.assertEqual(spec["links"][0]["localpref"], 50)
        self.assertEqual(spec["links"][0]["epoch"], 3)

    def test_router_limit(self):
        spec, errors = parse_all("A B C D E F G H I", "", "")
        self.assertTrue(any("至多" in e for e in errors))

    def test_event_limit(self):
        events = "\n".join("announce A A prefix=P%d" % i for i in range(65))
        spec, errors = parse_all("A", "", events)
        self.assertTrue(any("至多" in e for e in errors))

    def test_unknown_router_rejected(self):
        spec, errors = parse_all("A B", "A B", "announce A Z prefix=P")
        self.assertTrue(any("未定义" in e for e in errors))

    def test_unconfigured_adjacency_rejected(self):
        spec, errors = parse_all("A B", "", "disconnect A B")
        self.assertTrue(any("未配置" in e for e in errors))

    def test_bad_path_tail_rejected(self):
        spec, errors = parse_all("A B C", "A B", "announce A B prefix=P path=C,B")
        self.assertTrue(any("末端" in e for e in errors))

    def test_origination_path_must_be_self(self):
        spec, errors = parse_all("A B", "", "announce A A prefix=P path=B")
        self.assertTrue(any("始发" in e for e in errors))

    def test_deliver_message_rest_of_line(self):
        spec, errors = parse_all("A B", "", "deliver A B msg=hello world")
        self.assertEqual(errors, [])
        self.assertEqual(spec["events"][0]["msg"], "hello world")

    def test_comments_and_blank_lines(self):
        spec, errors = parse_all("A B", "# comment\n\nA B pref=10\n", "# nothing")
        self.assertEqual(errors, [])
        self.assertEqual(len(spec["links"]), 1)


if __name__ == "__main__":
    unittest.main()
