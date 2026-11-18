"""The legacy send_email wrapper: a sent email must not be logged as a failure."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pa_power_notifier as notifier  # noqa: E402


class FakeSMTP:
    """Stands in for smtplib.SMTP: records the message, sends nothing."""

    sent: list = []

    def __init__(self, host, port):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class SendEmailTests(unittest.TestCase):
    def test_legacy_send_email_logs_success_not_failure(self):
        checker = notifier.RateChecker.__new__(notifier.RateChecker)  # no env reads
        checker.smtp_host, checker.smtp_port = "smtp.invalid", 587
        checker.smtp_username, checker.smtp_password = "user", "secret"
        checker.notify_from, checker.notify_to = "from@example.invalid", "to@example.invalid"
        FakeSMTP.sent = []
        with mock.patch.object(notifier.smtplib, "SMTP", FakeSMTP), \
                mock.patch.object(notifier.RateChecker, "format_notification", return_value="body"), \
                mock.patch.object(notifier, "_record_email_state") as record, \
                self.assertLogs(notifier.logger, level="INFO") as logs:
            checker.send_email([{"supplier": "X"}])
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertFalse([r for r in logs.records if r.levelname == "ERROR"], logs.output)
        self.assertTrue(any("Email sent" in r.getMessage() for r in logs.records))
        record.assert_not_called()  # no report to fingerprint: that's for the report email


if __name__ == "__main__":
    unittest.main()
