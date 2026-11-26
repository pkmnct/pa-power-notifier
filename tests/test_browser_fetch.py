import subprocess
import sys
import time
import unittest
from unittest import mock

import browser_fetch as bf


def virtual():
    return ":99"


def no_virtual():
    return None


def has_socket(_path):
    return True


def no_socket(_path):
    return False


HEADLESS = "DUQUESNE_PUPPETEER_HEADLESS"


class BrowserLaunchTest(unittest.TestCase):
    def test_default_is_headless_even_with_a_display_or_xvfb(self):
        # 0.1.1 defaulted to a window and was blocked by Incapsula on every run.
        for environ, exists, start in (({}, no_socket, virtual), ({"DISPLAY": ":0"}, has_socket, virtual)):
            launch = bf._browser_launch(environ, exists, start)
            self.assertEqual(launch.env, {HEADLESS: "1"})
            self.assertEqual(launch.label, "headless")

    def test_auto_value_is_the_default(self):
        for var in ("DUQUESNE_BROWSER_MODE", HEADLESS):
            self.assertEqual(
                bf._browser_launch({var: "auto"}, no_socket, virtual),
                bf._browser_launch({}, no_socket, virtual),
            )

    def test_window_mode_uses_a_real_display_directly(self):
        launch = bf._browser_launch({"DUQUESNE_BROWSER_MODE": "window", "DISPLAY": ":0"}, has_socket, no_virtual)
        self.assertEqual(launch.env, {HEADLESS: "0"})

    def test_window_mode_does_not_trust_a_stale_display(self):
        launch = bf._browser_launch({"DUQUESNE_BROWSER_MODE": "window", "DISPLAY": ":0"}, no_socket, virtual)
        self.assertEqual(launch.env, {HEADLESS: "0", "DISPLAY": ":99"})
        self.assertEqual(launch.label, "virtual-display")

    def test_window_mode_trusts_a_remote_display(self):
        launch = bf._browser_launch({"DUQUESNE_BROWSER_MODE": "window", "DISPLAY": "host:0"}, no_socket, no_virtual)
        self.assertEqual(launch.env, {HEADLESS: "0"})

    def test_window_mode_uses_a_virtual_display_when_there_is_none(self):
        launch = bf._browser_launch({"DUQUESNE_BROWSER_MODE": "window"}, no_socket, virtual)
        self.assertEqual(launch.env, {HEADLESS: "0", "DISPLAY": ":99"})

    def test_headless_can_be_forced(self):
        for value in ("1", "true", "headless"):
            env = bf._browser_launch({"DUQUESNE_BROWSER_MODE": value}, has_socket, virtual).env
            self.assertEqual(env, {HEADLESS: "1"}, value)

    def test_shell_is_passed_through(self):
        env = bf._browser_launch({"DUQUESNE_BROWSER_MODE": "shell"}, has_socket, virtual).env
        self.assertEqual(env, {HEADLESS: "shell"})

    def test_legacy_variable_is_still_read(self):
        self.assertEqual(bf._browser_launch({HEADLESS: "true"}, has_socket, virtual).env, {HEADLESS: "1"})
        self.assertEqual(bf._browser_launch({HEADLESS: "0"}, no_socket, virtual).env[HEADLESS], "0")

    def test_window_without_a_display_falls_back_to_headless_with_a_warning(self):
        with self.assertLogs(bf.logger, "WARNING"):
            launch = bf._browser_launch({"DUQUESNE_BROWSER_MODE": "window"}, no_socket, no_virtual)
        self.assertEqual(launch.env, {HEADLESS: "1"})


class RunBrowserTest(unittest.TestCase):
    def test_passes_env_and_returns_output(self):
        proc = bf._run_browser(
            [sys.executable, "-c", "import os; print(os.environ['PROBE'])"], {"PROBE": "ok"}
        )
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "ok"))

    def test_timeout_kills_the_whole_process_tree(self):
        # The child spawns a grandchild that holds the pipes open; both must die.
        code = (
            "import subprocess, sys, time;"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            "time.sleep(60)"
        )
        start = time.monotonic()
        with mock.patch.object(bf, "_puppeteer_timeout", return_value=1):
            with self.assertRaises(subprocess.TimeoutExpired):
                bf._run_browser([sys.executable, "-c", code], {})
        self.assertLess(time.monotonic() - start, 10)


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
