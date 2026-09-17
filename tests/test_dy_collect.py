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

    def test_comment_pagination_waits_for_finished_bodies_and_explicit_end(self):
        fetch_source = inspect.getsource(dy_collect.fetch_comments)
        self.assertIn("Network.loadingFinished", fetch_source)
        self.assertIn("primary_no_more", fetch_source)
        self.assertIn("bottom_stable_rounds", fetch_source)
        self.assertIn("not pending", fetch_source)
        self.assertNotIn("window.scrollTo(0, document.body.scrollHeight)", fetch_source)

    def test_comment_payload_reads_nested_has_more(self):
        rows, more = dy_collect._comment_payload({
            "data": {"comments": [{"cid": "c-1"}], "has_more": 1}
        })
        self.assertEqual(rows[0]["cid"], "c-1")
        self.assertTrue(more)
        rows, more = dy_collect._comment_payload({"comments": [], "has_more": 0})
        self.assertEqual(rows, [])
        self.assertFalse(more)


if __name__ == "__main__":
    unittest.main()
