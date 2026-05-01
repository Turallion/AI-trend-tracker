# CT Trend Hunter

Telegram-connected X monitor.

Only signal used: **original tweet quote_count >= `QUOTE_THRESHOLD`**.

## What it does
- Monitors makers' original tweets.
- Monitors catchers' quote tweets, resolves root original tweet.
- Uses advanced search for the last `SEARCH_WINDOW_MINUTES` window.
- Filters out replies and retweets.
- Ignores self-quotes and self quote chains.
- Ignores originals older than `ORIGINAL_MAX_AGE_HOURS`.
- Deduplicates alerts by original tweet ID.
- Re-evaluates when seen tweet quote_count increases.
- Sends `ALERT` messages and one compact English report per run.
- Runs every `CHECK_INTERVAL_MINUTES` minutes.

## Setup
1. Install deps:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy env file:
   ```bash
   cp env.example .env
   ```
3. Export env vars from `.env` (or set in your process manager).
4. Run:
   ```bash
   python main.py
   ```
5. Run one scan with terminal output:
   ```bash
   python main.py --once --debug
   ```
6. Run a scan without Telegram sends or state writes:
   ```bash
   python main.py --once --debug --dry-run
   ```

## Config
- `X_API_KEY`
- `X_API_BASE_URL` (default `https://api.twitterapi.io`)
- `X_API_REQUESTS_PER_SECOND` (default `20`)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `CHECK_INTERVAL_MINUTES` (default `60`)
- `SEARCH_WINDOW_MINUTES` (default `120`)
- `QUOTE_THRESHOLD` (default `100`)
- `ORIGINAL_MAX_AGE_HOURS` (default `12`)
- `DB_PATH` (default `ct_trend_hunter_state.json`)
- `ACCOUNT_CONFIG_PATH` (default `project_accounts.json`)

Tracked accounts live in `project_accounts.json`:
```json
{
  "makers": ["OpenAI"],
  "catchers": ["sharbel"]
}
```

To change X API keys, update `X_API_KEY` in `.env` or in your process manager environment. To use the new faster key, keep `X_API_REQUESTS_PER_SECOND=20`.

## Note
`x_client.py` uses defensive parsing because response schemas can vary by API endpoint/account plan. If your endpoint path differs, update:
- `/twitter/tweet/advanced_search`
- `/twitter/tweet`

No other trend criteria are used.
