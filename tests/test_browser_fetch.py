import unittest

import browser_fetch as bf


def xvfb(_name):
    return "/usr/bin/xvfb-run"


def no_xvfb(_name):
    return None


class BrowserLaunchTest(unittest.TestCase):
    def test_default_uses_a_virtual_display_when_there_is_none(self):
        env, prefix = bf._browser_launch({}, xvfb)
        self.assertEqual(env, {"DUQUESNE_PUPPETEER_HEADLESS": "0"})
        self.assertEqual(prefix[0], "xvfb-run")

    def test_auto_value_is_the_default(self):
        self.assertEqual(
            bf._browser_launch({"DUQUESNE_PUPPETEER_HEADLESS": "auto"}, xvfb),
            bf._browser_launch({}, xvfb),
        )

    def test_a_real_display_is_used_directly(self):
        env, prefix = bf._browser_launch({"DISPLAY": ":0"}, xvfb)
        self.assertEqual(env, {"DUQUESNE_PUPPETEER_HEADLESS": "0"})
        self.assertEqual(prefix, [])

    def test_headless_can_be_forced(self):
        for value in ("1", "true", "headless", "shell"):
            self.assertEqual(
                bf._browser_launch({"DUQUESNE_PUPPETEER_HEADLESS": value}, xvfb), ({}, []), value
            )

    def test_without_xvfb_it_falls_back_to_headless(self):
        env, prefix = bf._browser_launch({}, no_xvfb)
        self.assertEqual(env, {"DUQUESNE_PUPPETEER_HEADLESS": "1"})
        self.assertEqual(prefix, [])
        with self.assertLogs(bf.logger, "WARNING"):
            bf._browser_launch({"DUQUESNE_PUPPETEER_HEADLESS": "window"}, no_xvfb)

    def test_window_mode_asks_for_a_window(self):
        env, _ = bf._browser_launch({"DUQUESNE_PUPPETEER_HEADLESS": "window"}, xvfb)
        self.assertEqual(env["DUQUESNE_PUPPETEER_HEADLESS"], "0")


class DescribeChallengeTest(unittest.TestCase):
    def test_empty_page(self):
        self.assertEqual(bf.describe_challenge(""), "empty page")

    def test_incapsula_page_shows_title_and_text(self):
        html = (
            "<html><head><title>Request unsuccessful</title><script>var x=1;</script></head>"
            '<body>Incapsula incident ID: 123 <iframe src="/_Incapsula_Resource"></iframe></body></html>'
        )
        out = bf.describe_challenge(html)
        self.assertIn("Request unsuccessful", out)
        self.assertIn("Incapsula", out)
        self.assertIn("Incapsula incident ID: 123", out)
        self.assertNotIn("var x", out)


if __name__ == "__main__":
    unittest.main()
