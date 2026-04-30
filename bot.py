from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

from config import Settings
from models import AccountScanReport, EvaluationResult, Tweet
from store import StateStore
from telegram_client import TelegramClient
from x_client import XClient


class CTTrendHunterBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.x = XClient(
            settings.x_api_key,
            settings.x_api_base_url,
            settings.x_api_requests_per_second,
        )
        self.tg = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
        self.store = StateStore(settings.db_path)

    def run_once(self, *, send_telegram: bool = True, save_state: bool = True) -> str:
        run_started_at = datetime.now(timezone.utc)
        window_start = run_started_at - timedelta(minutes=self.settings.check_interval_minutes)
        state = self.store.load()
        state.setdefault("tweets", {})
        state.setdefault("source_tweets", {})
        state.setdefault("alerts_sent", {})

        tracked_hit_accounts: Dict[str, Set[str]] = {}
        all_results: List[EvaluationResult] = []
        account_reports: List[AccountScanReport] = []

        for maker in self.settings.makers:
            report = AccountScanReport(account=maker, mode="maker")
            account_reports.append(report)
            try:
                tweets = self.x.search_user_tweets(maker, window_start, run_started_at)
                report.checked_count = len(tweets)
                for tweet in tweets:
                    if tweet.is_reply:
                        report.notes.append("reply ignored")
                        report.ignored_count += 1
                        continue
                    if tweet.is_quote:
                        report.skipped_quote_count += 1
                        report.ignored_count += 1
                        continue
                    result = self._evaluate(source_account=maker, tweet=tweet, root_tweet=tweet, mode="maker", state=state)
                    report.results.append(result)
                    report.new_items_count += 1 if result.source_seen_state == "new" else 0
                    report.ignored_count += 1 if result.ignored else 0
                    all_results.append(result)
                    if not result.ignored and result.root_tweet.quote_count >= self.settings.quote_threshold:
                        tracked_hit_accounts.setdefault(result.root_tweet.id, set()).add(maker)
            except Exception as e:
                report.error = str(e)

        for catcher in self.settings.catchers:
            report = AccountScanReport(account=catcher, mode="catcher")
            account_reports.append(report)
            try:
                tweets = self.x.search_user_tweets(catcher, window_start, run_started_at, quote_only=True)
                report.checked_count = len(tweets)
                for tweet in tweets:
                    if tweet.is_reply:
                        report.notes.append("reply ignored")
                        report.ignored_count += 1
                        continue
                    if not tweet.is_quote or not tweet.quoted_tweet_id:
                        report.skipped_non_quote_count += 1
                        report.ignored_count += 1
                        continue
                    root = self._resolve_root(tweet)
                    if not root:
                        report.ignored_count += 1
                        report.notes.append("quote tweet ignored: original tweet could not be resolved")
                        continue
                    result = self._evaluate(source_account=catcher, tweet=tweet, root_tweet=root, mode="catcher", state=state)
                    report.results.append(result)
                    report.new_items_count += 1 if result.source_seen_state == "new" else 0
                    report.ignored_count += 1 if result.ignored else 0
                    all_results.append(result)
                    if not result.ignored and result.root_tweet.quote_count >= self.settings.quote_threshold:
                        tracked_hit_accounts.setdefault(result.root_tweet.id, set()).add(catcher)
            except Exception as e:
                report.error = str(e)

        trend_ids: Set[str] = set()
        alert_results: List[EvaluationResult] = []
        for result in all_results:
            if not result.ignored and result.root_tweet.quote_count >= self.settings.quote_threshold:
                trend_ids.add(result.root_tweet.id)
                result.tracked_accounts_on_trend = sorted(list(tracked_hit_accounts.get(result.root_tweet.id, set())))
                alert_results.append(result)

        report_text = self._format_report(account_reports, window_start, run_started_at, len(trend_ids))

        if save_state:
            self.store.save(state)

        if send_telegram:
            for result in alert_results:
                self._send_alert_if_needed(result, state)
            if save_state:
                self.store.save(state)
            self._send_report(report_text)

        return report_text

    def _resolve_root(self, quote_tweet: Tweet) -> Optional[Tweet]:
        current = quote_tweet.quoted_tweet
        if not current and quote_tweet.quoted_tweet_id:
            current = self.x.get_tweet_by_id(quote_tweet.quoted_tweet_id)
        if not current:
            return None
        hop = 0
        while current.is_quote and current.quoted_tweet_id and hop < 5:
            next_t = current.quoted_tweet or self.x.get_tweet_by_id(current.quoted_tweet_id)
            if not next_t:
                break
            current = next_t
            hop += 1
        return current

    def _evaluate(self, source_account: str, tweet: Tweet, root_tweet: Tweet, mode: str, state: dict) -> EvaluationResult:
        now = datetime.now(timezone.utc)
        seen = state["tweets"].get(root_tweet.id)
        seen_state = "new" if not seen else "already_seen"

        ignored = False
        reason = ""

        if (now - root_tweet.created_at) > timedelta(hours=6):
            ignored = True
            reason = "original tweet older than 6h"

        if mode == "catcher":
            if tweet.author_username.lower() == root_tweet.author_username.lower():
                ignored = True
                reason = "self-quote ignored"
            elif tweet.author_username.lower() == source_account.lower() and root_tweet.author_username.lower() == source_account.lower():
                ignored = True
                reason = "quote-on-quote self-chain ignored"

        if root_tweet.id:
            prev_count = int(state["tweets"].get(root_tweet.id, {}).get("quote_count", -1))
            state["tweets"][root_tweet.id] = {
                "quote_count": root_tweet.quote_count,
                "updated_at": now.isoformat(),
            }
            if not ignored and root_tweet.quote_count < self.settings.quote_threshold:
                ignored = True
                reason = f"not enough quotes (<{self.settings.quote_threshold})"
            elif not ignored and seen and root_tweet.quote_count > prev_count:
                reason = "already seen, quote count increased -> re-evaluated"
            elif not ignored and root_tweet.quote_count >= self.settings.quote_threshold:
                reason = f"eligible for alert (>={self.settings.quote_threshold} quotes)"

        source_seen = state["source_tweets"].get(tweet.id)
        source_seen_state = "new" if not source_seen else "already_seen"
        if tweet.id:
            state["source_tweets"][tweet.id] = {
                "source_account": source_account,
                "mode": mode,
                "updated_at": now.isoformat(),
            }

        return EvaluationResult(
            source_account=source_account,
            mode=mode,
            tweet=tweet,
            root_tweet=root_tweet,
            seen_state=seen_state,
            source_seen_state=source_seen_state,
            ignored=ignored,
            reason=reason,
        )

    def _send_alert_if_needed(self, result: EvaluationResult, state: dict) -> None:
        root_id = result.root_tweet.id
        if state["alerts_sent"].get(root_id):
            return

        tracked = ", ".join([f"@{a}" for a in result.tracked_accounts_on_trend]) if result.tracked_accounts_on_trend else "n/a"
        text = (
            "ALERT\n"
            f"Original tweet: {result.root_tweet.text[:160]}\n"
            f"Author: @{result.root_tweet.author_username}\n"
            f"Quote count: {result.root_tweet.quote_count}\n"
            f"Original link: {result.root_tweet.url}\n"
            f"Tracked accounts on trend: {tracked}"
        )

        media = result.root_tweet.media_urls[0] if result.root_tweet.media_urls else None
        if media:
            try:
                self.tg.send_photo_with_caption(media, text)
            except Exception:
                self.tg.send_message(text)
        else:
            self.tg.send_message(text)

        state["alerts_sent"][root_id] = {
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "quote_count": result.root_tweet.quote_count,
        }

    def _send_report(self, text: str) -> None:
        for chunk in self._split_message(text):
            self.tg.send_message(chunk)

    def _format_report(
        self,
        reports: List[AccountScanReport],
        interval_start: datetime,
        run_started_at: datetime,
        trend_count: int,
    ) -> str:
        period = f"{self._format_time(interval_start)}-{self._format_time(run_started_at)}"
        trends_line = f"Trends detected: {'yes, ' + str(trend_count) if trend_count else 'no'}"

        maker_reports = [r for r in reports if r.mode == "maker" and self._should_include_report(r)]
        catcher_reports = [r for r in reports if r.mode == "catcher" and self._should_include_report(r)]

        lines = [
            "AI trend bot",
            f"Report window: {period}",
            trends_line,
            "",
            "Makers",
        ]
        lines.extend(self._format_account_report(report) for report in maker_reports)
        if not maker_reports:
            lines.append("No maker tweets in this window.")
        lines.extend(["", "Catchers"])
        lines.extend(self._format_account_report(report) for report in catcher_reports)
        if not catcher_reports:
            lines.append("No catcher quote tweets in this window.")
        return "\n".join(lines)

    def _format_time(self, dt: datetime) -> str:
        return dt.astimezone().strftime("%H.%M")

    def _format_account_report(self, report: AccountScanReport) -> str:
        if report.error:
            return (
                f"@{report.account} - new items: 0; ignored: yes\n"
                f"  reason: scan error: {report.error}"
            )

        ignored_label = "yes" if report.ignored_count else "no"
        reasons = self._format_reasons(report)

        if report.mode == "maker":
            quotes_count = sum(result.root_tweet.quote_count for result in report.results)
            header = (
                f"@{report.account} - new tweets: {report.new_items_count}; "
                f"quotes: {quotes_count}; ignored: {ignored_label}"
            )
        else:
            header = (
                f"@{report.account} - new quote tweets: {report.new_items_count}; "
                f"ignored: {ignored_label}"
            )

        details = self._format_tweet_links(report)
        if reasons:
            details.append(f"  reason: {reasons}")

        return "\n".join([header, *details])

    def _should_include_report(self, report: AccountScanReport) -> bool:
        if report.error:
            return True
        if report.mode == "maker":
            return bool(report.results)
        return bool(report.results or report.skipped_non_quote_count or report.notes)

    def _format_reasons(self, report: AccountScanReport) -> str:
        reasons = Counter()
        for result in report.results:
            if result.ignored and result.reason:
                reasons[result.reason] += 1
        if report.skipped_quote_count:
            reasons["quote tweets skipped for makers"] += report.skipped_quote_count
        if report.skipped_non_quote_count:
            reasons["non-quote tweets skipped for catchers"] += report.skipped_non_quote_count
        for note in report.notes:
            reasons[note] += 1

        return "; ".join(
            f"{reason} ({count})" if count > 1 else reason
            for reason, count in reasons.items()
        )

    def _format_tweet_links(self, report: AccountScanReport, limit: int = 3) -> List[str]:
        urls: List[str] = []
        for result in report.results:
            url = result.root_tweet.url if report.mode == "maker" else result.tweet.url
            if url and url not in urls:
                urls.append(url)

        lines = [f"  tweet: {url}" for url in urls[:limit]]
        if len(urls) > limit:
            lines.append(f"  more tweet links: {len(urls) - limit}")
        return lines

    def _split_message(self, text: str, limit: int = 3900) -> List[str]:
        chunks: List[str] = []
        current = ""
        for line in text.splitlines():
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > limit:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks
