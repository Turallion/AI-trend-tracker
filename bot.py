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
        window_start = run_started_at - timedelta(minutes=self.settings.search_window_minutes)
        state = self.store.load()
        state.setdefault("tweets", {})
        state.setdefault("source_tweets", {})
        state.setdefault("alerts_sent", {})
        state.setdefault("alerts_history", [])
        state.setdefault("daily_digest_sent", {})

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
            if self._should_send_report(account_reports, alert_results):
                self._send_report(report_text)

        return report_text

    def send_daily_digest_if_due(
        self,
        *,
        now: Optional[datetime] = None,
        send_telegram: bool = True,
        save_state: bool = True,
    ) -> Optional[str]:
        now = now or datetime.now(timezone.utc)
        local_now = self._to_local_time(now)
        digest_date = local_now.strftime("%Y-%m-%d")

        if local_now.hour != self.settings.daily_digest_hour:
            return None

        state = self.store.load()
        state.setdefault("alerts_history", [])
        state.setdefault("daily_digest_sent", {})
        if state["daily_digest_sent"].get(digest_date):
            return None

        window_start = now - timedelta(hours=24)
        alerts = self._alerts_in_window(state["alerts_history"], window_start, now)
        self._refresh_alert_quote_counts(alerts)
        text = self._format_daily_digest(alerts, window_start, now)
        if send_telegram:
            self._send_report(text)
        state["daily_digest_sent"][digest_date] = datetime.now(timezone.utc).isoformat()
        if save_state:
            self.store.save(state)
        return text

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

        if (now - root_tweet.created_at) > timedelta(hours=self.settings.original_max_age_hours):
            ignored = True
            reason = f"original tweet older than {self.settings.original_max_age_hours}h"

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
        state.setdefault("alerts_history", []).append(self._alert_history_item(result))

    def _alert_history_item(self, result: EvaluationResult) -> dict:
        return {
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "root_tweet_id": result.root_tweet.id,
            "author": result.root_tweet.author_username,
            "quote_count": result.root_tweet.quote_count,
            "original_url": result.root_tweet.url,
            "source_account": result.source_account,
            "source_mode": result.mode,
            "source_url": result.tweet.url,
            "tracked_accounts": result.tracked_accounts_on_trend,
            "text": result.root_tweet.text[:160],
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
        return self._to_local_time(dt).strftime("%H.%M")

    def _to_local_time(self, dt: datetime) -> datetime:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(self.settings.timezone_name)
        except Exception:
            tz = timezone.utc
        return dt.astimezone(tz)

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

    def _should_send_report(
        self,
        reports: List[AccountScanReport],
        alert_results: List[EvaluationResult],
    ) -> bool:
        if alert_results:
            return True
        return any(report.new_items_count > 0 for report in reports)

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

    def _alerts_in_window(self, alerts: List[dict], start: datetime, end: datetime) -> List[dict]:
        selected = []
        for alert in alerts:
            try:
                sent_at = datetime.fromisoformat(alert["sent_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            if start <= sent_at <= end:
                selected.append(alert)
        return sorted(selected, key=lambda item: item.get("quote_count", 0), reverse=True)

    def _refresh_alert_quote_counts(self, alerts: List[dict]) -> None:
        for alert in alerts:
            tweet_id = alert.get("root_tweet_id")
            if not tweet_id:
                continue
            try:
                tweet = self.x.get_tweet_by_id(str(tweet_id))
            except Exception:
                continue
            if tweet:
                alert["quote_count"] = tweet.quote_count

    def _format_daily_digest(self, alerts: List[dict], window_start: datetime, now: datetime) -> str:
        period = f"{self._format_digest_time(window_start)}-{self._format_digest_time(now)}"
        lines = [
            "AI trend bot",
            f"Daily alert digest: {period}",
            f"Alerts: {len(alerts)}",
            "",
        ]

        if not alerts:
            lines.append("No alerts in the last 24 hours.")
            return "\n".join(lines)

        for idx, alert in enumerate(alerts, start=1):
            lines.extend([
                f"{idx}. @{alert.get('author', 'unknown')} - {alert.get('quote_count', 0)} quotes",
                f"   original: {alert.get('original_url', 'n/a')}",
            ])
            lines.append("")

        return "\n".join(lines).rstrip()

    def _format_digest_time(self, dt: datetime) -> str:
        return self._to_local_time(dt).strftime("%m.%d")

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
