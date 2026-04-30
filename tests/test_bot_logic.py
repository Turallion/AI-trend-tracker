import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot import CTTrendHunterBot
from config import Settings
from models import AccountScanReport, EvaluationResult, Tweet
from x_client import XClient


def make_settings(db_path: str) -> Settings:
    return Settings(
        x_api_key="test",
        x_api_base_url="https://example.com",
        x_api_requests_per_second=20,
        telegram_bot_token="test",
        telegram_chat_id="test",
        check_interval_minutes=30,
        quote_threshold=100,
        db_path=db_path,
        account_config_path="project_accounts.json",
        makers=[],
        catchers=[],
    )


def make_tweet(
    tweet_id: str,
    *,
    author: str = "maker",
    quote_count: int = 0,
    created_at: datetime | None = None,
    is_quote: bool = False,
    is_reply: bool = False,
    quoted_tweet_id: str | None = None,
    quoted_tweet: Tweet | None = None,
) -> Tweet:
    created_at = created_at or datetime.now(timezone.utc)
    return Tweet(
        id=tweet_id,
        author_username=author,
        author_id=None,
        text="tweet",
        created_at=created_at,
        quote_count=quote_count,
        url=f"https://x.com/{author}/status/{tweet_id}",
        is_quote=is_quote,
        is_reply=is_reply,
        quoted_tweet_id=quoted_tweet_id,
        quoted_tweet=quoted_tweet,
    )


class BotLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)
        self.bot = CTTrendHunterBot(make_settings(self.tmp.name))
        self.state = {"tweets": {}, "source_tweets": {}, "alerts_sent": {}}

    def tearDown(self) -> None:
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_maker_original_under_threshold_is_ignored(self) -> None:
        tweet = make_tweet("1", quote_count=99)

        result = self.bot._evaluate("maker", tweet, tweet, "maker", self.state)

        self.assertTrue(result.ignored)
        self.assertEqual(result.reason, "not enough quotes (<100)")

    def test_maker_original_over_threshold_is_alert_eligible(self) -> None:
        tweet = make_tweet("1", quote_count=100)

        result = self.bot._evaluate("maker", tweet, tweet, "maker", self.state)

        self.assertFalse(result.ignored)
        self.assertEqual(result.reason, "eligible for alert (>=100 quotes)")

    def test_maker_quote_report_is_not_included(self) -> None:
        report = AccountScanReport(account="maker", mode="maker", skipped_quote_count=1, ignored_count=1)

        self.assertFalse(self.bot._should_include_report(report))

    def test_catcher_non_quote_is_skipped_reason(self) -> None:
        report = AccountScanReport(account="catcher", mode="catcher", skipped_non_quote_count=1, ignored_count=1)

        text = self.bot._format_account_report(report)

        self.assertIn("new quote tweets: 0", text)
        self.assertIn("non-quote tweets skipped for catchers", text)

    def test_catcher_old_root_is_ignored(self) -> None:
        quote = make_tweet("q1", author="catcher", is_quote=True, quoted_tweet_id="r1")
        root = make_tweet("r1", author="root", quote_count=700, created_at=datetime.now(timezone.utc) - timedelta(hours=7))

        result = self.bot._evaluate("catcher", quote, root, "catcher", self.state)

        self.assertTrue(result.ignored)
        self.assertEqual(result.reason, "original tweet older than 6h")

    def test_report_only_shows_active_accounts(self) -> None:
        now = datetime(2026, 4, 28, 10, 30, tzinfo=timezone.utc)
        active_tweet = make_tweet("1", author="maker", quote_count=120, created_at=now)
        active_result = EvaluationResult(
            source_account="maker",
            mode="maker",
            tweet=active_tweet,
            root_tweet=active_tweet,
            seen_state="new",
            source_seen_state="new",
            ignored=True,
            reason="not enough quotes (<100)",
        )
        reports = [
            AccountScanReport(account="maker", mode="maker", new_items_count=1, ignored_count=1, results=[active_result]),
            AccountScanReport(account="quiet", mode="maker"),
        ]

        text = self.bot._format_report(reports, now - timedelta(minutes=30), now, 0)

        self.assertIn("@maker", text)
        self.assertIn("tweet: https://x.com/maker/status/1", text)
        self.assertNotIn("@quiet", text)

    def test_catcher_report_uses_quote_tweet_link(self) -> None:
        quote = make_tweet("q1", author="catcher", is_quote=True, quoted_tweet_id="r1")
        root = make_tweet("r1", author="root", quote_count=90)
        result = EvaluationResult(
            source_account="catcher",
            mode="catcher",
            tweet=quote,
            root_tweet=root,
            seen_state="new",
            source_seen_state="new",
            ignored=True,
            reason="not enough quotes (<100)",
        )
        report = AccountScanReport(account="catcher", mode="catcher", new_items_count=1, ignored_count=1, results=[result])

        text = self.bot._format_account_report(report)

        self.assertIn("tweet: https://x.com/catcher/status/q1", text)
        self.assertNotIn("tweet: https://x.com/root/status/r1", text)

    def test_embedded_quoted_tweet_is_parsed_and_used_as_root(self) -> None:
        client = XClient("test", "https://example.com")
        parsed = client._parse_tweet({
            "id": "q1",
            "text": "quote",
            "createdAt": "2026-04-28T10:00:00Z",
            "author": {"userName": "catcher"},
            "quoted_tweet": {
                "id": "r1",
                "text": "root",
                "quoteCount": 600,
                "createdAt": "2026-04-28T10:00:00Z",
                "author": {"userName": "root"},
            },
        })

        self.assertIsNotNone(parsed)
        self.assertTrue(parsed.is_quote)
        self.assertEqual(parsed.quoted_tweet_id, "r1")
        self.assertEqual(self.bot._resolve_root(parsed).id, "r1")


if __name__ == "__main__":
    unittest.main()
