import unittest
import inspect

from src import dy_collect


class DouyinExpandControlTests(unittest.TestCase):
    def test_trigger_prioritizes_real_nested_reply_expand_button(self):
        source = dy_collect.TRIGGER_JS
        self.assertIn("comment-reply-expand-btn", source)
        self.assertIn("scrollIntoView", source)
        self.assertIn("new MouseEvent('mousedown'", source)
        self.assertIn("expandRe", source)
        self.assertIn("clicked:clicked.length", source)
        fetch_source = inspect.getsource(dy_collect.fetch_comments)
        self.assertIn("expand_stalled_rounds", fetch_source)
        self.assertIn("expand_click_total >= 24", fetch_source)


if __name__ == "__main__":
    unittest.main()
