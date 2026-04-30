from datetime import datetime, timezone
import threading
import time
from typing import Any, Dict, List, Optional
import requests
from requests import HTTPError
from dateutil import parser as date_parser
from models import Tweet


class XClient:
    """
    twitterapi.io wrapper with defensive parsing for response variants.
    """

    def __init__(self, api_key: str, base_url: str, max_requests_per_second: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.min_request_interval = (1.0 / max_requests_per_second) + 0.005
        self._rate_limit_lock = threading.Lock()
        self._last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": api_key,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def _wait_for_rate_limit(self) -> None:
        with self._rate_limit_lock:
            now = time.monotonic()
            wait_for = self.min_request_interval - (now - self._last_request_at)
            if wait_for > 0:
                time.sleep(wait_for)
            self._last_request_at = time.monotonic()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        attempts = 4
        for attempt in range(attempts):
            self._wait_for_rate_limit()
            res = self.session.get(f"{self.base_url}{path}", params=params or {}, timeout=30)
            if res.status_code == 429 and attempt < attempts - 1:
                time.sleep(self._retry_delay(res, attempt))
                continue
            try:
                res.raise_for_status()
            except HTTPError as e:
                raise RuntimeError(self._format_http_error(e)) from e
            return res.json()
        raise RuntimeError("request failed after retries")

    def _retry_delay(self, res: requests.Response, attempt: int) -> float:
        retry_after = res.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 1.0)
            except ValueError:
                pass
        return 8.0 * (attempt + 1)

    def _format_http_error(self, error: HTTPError) -> str:
        response = error.response
        if response is None:
            return str(error)
        return f"{response.status_code} {response.reason}"

    def get_user_recent_tweets(self, username: str, limit: int = 20) -> List[Tweet]:
        payload = self._get("/twitter/user/last_tweets", {"userName": username, "limit": limit})
        raw_tweets = payload.get("tweets") or payload.get("data") or payload.get("result") or []
        return self._parse_tweets(raw_tweets)

    def search_user_tweets(
        self,
        username: str,
        since: datetime,
        until: datetime,
        *,
        quote_only: bool = False,
        max_pages: int = 3,
    ) -> List[Tweet]:
        query = self._build_search_query(username, since, until, quote_only=quote_only)
        tweets: List[Tweet] = []
        cursor = ""

        for _ in range(max_pages):
            payload = self._get(
                "/twitter/tweet/advanced_search",
                {"query": query, "queryType": "Latest", "cursor": cursor},
            )
            raw_tweets = payload.get("tweets") or payload.get("data") or payload.get("result") or []
            tweets.extend(self._parse_tweets(raw_tweets))

            if not payload.get("has_next_page"):
                break
            cursor = payload.get("next_cursor") or ""
            if not cursor:
                break

        return tweets

    def _build_search_query(self, username: str, since: datetime, until: datetime, *, quote_only: bool) -> str:
        # twitterapi.io requires epoch since_time/until_time. The string
        # form `since:YYYY-MM-DD_HH:MM:SS_UTC` is documented as unsupported and
        # silently returns far fewer or zero results.
        parts = [
            f"from:{username.lstrip('@')}",
            f"since_time:{self._format_search_time(since)}",
            f"until_time:{self._format_search_time(until)}",
            "-filter:replies",
            "-filter:nativeretweets",
        ]
        if quote_only:
            parts.append("filter:quote")
        else:
            parts.append("-filter:quote")
        return " ".join(parts)

    def _format_search_time(self, dt: datetime) -> str:
        # twitterapi.io expects unix epoch seconds for since_time/until_time.
        return str(int(dt.astimezone(timezone.utc).timestamp()))

    def _parse_tweets(self, raw_tweets: Any) -> List[Tweet]:
        if isinstance(raw_tweets, dict):
            raw_tweets = list(raw_tweets.values())
        if not isinstance(raw_tweets, list):
            raw_tweets = [raw_tweets]

        parsed: List[Tweet] = []
        for item in raw_tweets:
            tweet = self._parse_tweet(item)
            if tweet:
                parsed.append(tweet)
        return parsed

    def get_tweet_by_id(self, tweet_id: str) -> Optional[Tweet]:
        payload = self._get("/twitter/tweet", {"tweetId": tweet_id})
        raw = payload.get("tweet") or payload.get("data") or payload.get("result")
        if not raw:
            return None
        return self._parse_tweet(raw)

    def _parse_tweet(self, t: Any) -> Optional[Tweet]:
        if not isinstance(t, dict):
            return None

        tweet_id = str(t.get("id") or t.get("tweet_id") or "")

        user = t.get("author") or t.get("user") or {}
        if not isinstance(user, dict):
            user = {}

        username = user.get("userName") or user.get("username") or t.get("userName") or "unknown"
        author_id = str(user.get("id")) if user.get("id") else None

        created_raw = t.get("createdAt") or t.get("created_at")
        try:
            created_at = date_parser.parse(created_raw).astimezone(timezone.utc) if created_raw else datetime.now(timezone.utc)
        except Exception:
            created_at = datetime.now(timezone.utc)

        try:
            quote_count = int(
                t.get("quoteCount")
                or t.get("quote_count")
                or (t.get("public_metrics") or {}).get("quote_count")
                or 0
            )
        except (TypeError, ValueError):
            quote_count = 0

        is_quote = bool(t.get("isQuote") or t.get("is_quote") or t.get("quoted_tweet_id") or t.get("quotedStatusId"))
        quoted_id = t.get("quotedStatusId") or t.get("quoted_tweet_id")
        quoted_raw = t.get("quoted_tweet") if isinstance(t.get("quoted_tweet"), dict) else None
        quoted_tweet = self._parse_tweet(quoted_raw) if quoted_raw else None
        if not quoted_id and quoted_tweet:
            quoted_id = quoted_tweet.id
        is_quote = is_quote or bool(quoted_tweet)
        is_reply = bool(t.get("isReply") or t.get("is_reply") or t.get("inReplyToId") or t.get("in_reply_to_status_id"))

        media_urls: List[str] = []
        media = t.get("media") or t.get("extendedEntities") or []
        if isinstance(media, list):
            for m in media:
                if not isinstance(m, dict):
                    continue
                u = m.get("media_url_https") or m.get("media_url") or m.get("url")
                if u:
                    media_urls.append(u)
        elif isinstance(media, dict):
            candidates = media.get("photos") or media.get("items") or []
            if isinstance(candidates, list):
                for m in candidates:
                    if not isinstance(m, dict):
                        continue
                    u = m.get("url") or m.get("media_url")
                    if u:
                        media_urls.append(u)

        return Tweet(
            id=tweet_id,
            author_username=str(username).lstrip("@"),
            author_id=author_id,
            text=t.get("text") or "",
            created_at=created_at,
            quote_count=quote_count,
            url=f"https://x.com/{str(username).lstrip('@')}/status/{tweet_id}" if tweet_id else "",
            is_quote=is_quote,
            is_reply=is_reply,
            quoted_tweet_id=str(quoted_id) if quoted_id else None,
            quoted_tweet=quoted_tweet,
            media_urls=media_urls,
        )
