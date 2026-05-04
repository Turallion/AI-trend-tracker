import json
import os
from dataclasses import dataclass
from typing import List


MAKERS = [
    "OpenAI",
    "Kimi_Moonshot",
    "AnthropicAI",
    "deepseek_ai",
    "GeminiApp",
    "sama",
    "imagine",
]

CATCHERS = [
    "sharbel",
    "mattshumer_",
    "AlexFinn",
    "skirano",
    "skeptrune",
    "IterIntellectus",
    "cryptopunk7213",
    "vllm_project",
    "Yuchenj_UW",
    "VibeMarketer_",
    "kanavtwt",
]


@dataclass
class Settings:
    x_api_key: str
    x_api_base_url: str
    x_api_requests_per_second: float
    telegram_bot_token: str
    telegram_chat_id: str
    check_interval_minutes: int
    search_window_minutes: int
    quote_threshold: int
    original_max_age_hours: int
    timezone_name: str
    daily_digest_hour: int
    db_path: str
    account_config_path: str
    makers: List[str]
    catchers: List[str]


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _load_dotenv(filename: str = ".env") -> None:
    if not os.path.exists(filename):
        return
    with open(filename, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def _load_accounts(filename: str) -> tuple[List[str], List[str]]:
    if not os.path.exists(filename):
        return MAKERS, CATCHERS
    with open(filename, "r", encoding="utf-8") as f:
        data = json.load(f)
    makers = data.get("makers") or MAKERS
    catchers = data.get("catchers") or CATCHERS
    return list(makers), list(catchers)


def load_settings() -> Settings:
    _load_dotenv()
    account_config_path = _env("ACCOUNT_CONFIG_PATH", "project_accounts.json")
    makers, catchers = _load_accounts(account_config_path)
    return Settings(
        x_api_key=_env("X_API_KEY"),
        x_api_base_url=_env("X_API_BASE_URL", "https://api.twitterapi.io"),
        x_api_requests_per_second=float(_env("X_API_REQUESTS_PER_SECOND", "20")),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        check_interval_minutes=int(_env("CHECK_INTERVAL_MINUTES", "60")),
        search_window_minutes=int(_env("SEARCH_WINDOW_MINUTES", "120")),
        quote_threshold=int(_env("QUOTE_THRESHOLD", "100")),
        original_max_age_hours=int(_env("ORIGINAL_MAX_AGE_HOURS", "12")),
        timezone_name=_env("TIMEZONE", "Europe/Moscow"),
        daily_digest_hour=int(_env("DAILY_DIGEST_HOUR", "10")),
        db_path=_env("DB_PATH", "ct_trend_hunter_state.json"),
        account_config_path=account_config_path,
        makers=makers,
        catchers=catchers,
    )
