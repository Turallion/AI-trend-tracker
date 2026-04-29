from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class Tweet:
    id: str
    author_username: str
    author_id: Optional[str]
    text: str
    created_at: datetime
    quote_count: int
    url: str
    is_quote: bool
    is_reply: bool
    quoted_tweet_id: Optional[str]
    quoted_tweet: Optional["Tweet"] = None
    media_urls: List[str] = field(default_factory=list)


@dataclass
class EvaluationResult:
    source_account: str
    mode: str
    tweet: Tweet
    root_tweet: Tweet
    seen_state: str  # new | already_seen
    source_seen_state: str  # new | already_seen
    ignored: bool
    reason: str
    tracked_accounts_on_trend: List[str] = field(default_factory=list)


@dataclass
class AccountScanReport:
    account: str
    mode: str  # maker | catcher
    checked_count: int = 0
    new_items_count: int = 0
    ignored_count: int = 0
    skipped_quote_count: int = 0
    skipped_non_quote_count: int = 0
    results: List[EvaluationResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None
