#!/usr/bin/env python3
"""Tests for how the bot tells you things, and what happens when telling fails. Nothing is sent: every
network call is replaced.

    python3 -m unittest -v test_bot
"""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

import check

DISCORD = "https://discord.com/api/webhooks/123/abc"


class Notify(unittest.TestCase):
    def env(self, **kw):
        base = {k: "" for k in ("DISCORD_WEBHOOK_URL", "GMAIL_USER", "GMAIL_APP_PASSWORD", "EMAIL_TO")}
        return mock.patch.dict(os.environ, {**base, **kw})

    def test_a_discord_only_setup_sends_to_discord(self):
        with self.env(DISCORD_WEBHOOK_URL=DISCORD), mock.patch.object(check, "send_discord") as d, mock.patch.object(check, "send_email") as e:
            check.notify("hello", "subject")
        d.assert_called_once_with("hello")
        e.assert_not_called()

    def test_one_failing_channel_does_not_block_the_other(self):
        with self.env(DISCORD_WEBHOOK_URL=DISCORD, GMAIL_USER="me@x.com", GMAIL_APP_PASSWORD="pw"), \
                mock.patch.object(check, "send_discord", side_effect=RuntimeError("webhook deleted")), \
                mock.patch.object(check, "send_email") as e:
            with contextlib.redirect_stdout(io.StringIO()):
                check.notify("hello", "subject")  # must not raise: the email went out
        e.assert_called_once()

    def test_when_every_channel_fails_the_step_fails_loudly(self):
        with self.env(DISCORD_WEBHOOK_URL=DISCORD), mock.patch.object(check, "send_discord", side_effect=RuntimeError("down")):
            with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(io.StringIO()):
                check.notify("hello", "subject")
        self.assertEqual(ctx.exception.code, 1)

    def test_with_no_channel_at_all_it_says_what_to_set_up(self):
        with self.env():
            with self.assertRaises(SystemExit) as ctx:
                check.notify("hello", "subject")
        self.assertIn("DISCORD_WEBHOOK_URL", str(ctx.exception.code))

    def test_only_discord_dot_com_webhook_urls_count_as_configured(self):
        with self.env(DISCORD_WEBHOOK_URL="https://example.com/hook"):
            self.assertFalse(check.discord_configured())
        with self.env(DISCORD_WEBHOOK_URL=DISCORD):
            self.assertTrue(check.discord_configured())

    def test_a_long_digest_is_split_so_discord_accepts_it(self):
        text = "\n".join(f"line {i} " + "x" * 60 for i in range(80))  # about 5,000 characters
        with self.env(DISCORD_WEBHOOK_URL=DISCORD), mock.patch.object(check, "post_json") as post:
            check.send_discord(text)
        self.assertGreater(post.call_count, 2)
        self.assertTrue(all(len(c.args[1]["content"]) <= 2000 for c in post.call_args_list))


class FailureAlert(unittest.TestCase):
    """The workflow runs `check.py --alert "..."` when a run fails."""

    def run_main(self, *argv):
        with mock.patch.object(sys, "argv", ["check.py", *argv]), mock.patch.object(check, "notify") as notify:
            check.main()
        return notify

    def test_the_alert_carries_the_message_and_a_warning_sign(self):
        notify = self.run_main("--alert", "The daily manga check failed. Details: https://github.com/x/actions/runs/1")
        text, subject = notify.call_args.args
        self.assertTrue(text.startswith("⚠️"))
        self.assertIn("https://github.com/x/actions/runs/1", text)
        self.assertIn("failed", subject)

    def test_the_test_message_is_a_friendly_one(self):
        text, _ = self.run_main("--test").call_args.args
        self.assertIn("connected", text)


if __name__ == "__main__":
    unittest.main()
