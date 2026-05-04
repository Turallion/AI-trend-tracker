import argparse
import time
from datetime import datetime, timedelta

from bot import CTTrendHunterBot
from config import load_settings


def _seconds_to_next_slot(interval_minutes: int) -> float:
    """Return seconds until the next wall-clock slot aligned to the interval.

    For interval=30 -> next HH:00 or HH:30.
    For interval=15 -> next HH:00, HH:15, HH:30, HH:45, etc.
    """
    now = datetime.now()
    interval = max(1, int(interval_minutes))
    next_minute = ((now.minute // interval) + 1) * interval
    if next_minute >= 60:
        target = (now + timedelta(hours=1)).replace(minute=next_minute - 60, second=0, microsecond=0)
    else:
        target = now.replace(minute=next_minute, second=0, microsecond=0)
    return max(1.0, (target - now).total_seconds())


def validate_settings(*, dry_run: bool = False) -> None:
    s = load_settings()
    missing = []
    if not s.x_api_key:
        missing.append("X_API_KEY")
    if not dry_run and not s.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not dry_run and not s.telegram_chat_id:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


def run_once(*, debug: bool = False, dry_run: bool = False) -> None:
    validate_settings(dry_run=dry_run)
    settings = load_settings()
    bot = CTTrendHunterBot(settings)
    report = bot.run_once(send_telegram=not dry_run, save_state=not dry_run)
    if debug:
        print(report)


def run(*, debug: bool = False, dry_run: bool = False, run_now: bool = False) -> None:
    validate_settings(dry_run=dry_run)
    settings = load_settings()
    bot = CTTrendHunterBot(settings)
    interval = settings.check_interval_minutes

    # Optional immediate first run so operator gets feedback on startup.
    if run_now:
        _do_run(bot, debug=debug, dry_run=dry_run)

    while True:
        wait_seconds = _seconds_to_next_slot(interval)
        next_at = (datetime.now() + timedelta(seconds=wait_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        if debug:
            print(f"[scheduler] sleeping {int(wait_seconds)}s until next slot at {next_at}")
        time.sleep(wait_seconds)
        _do_run(bot, debug=debug, dry_run=dry_run)


def _do_run(bot: CTTrendHunterBot, *, debug: bool, dry_run: bool) -> None:
    try:
        report = bot.run_once(send_telegram=not dry_run, save_state=not dry_run)
        digest = bot.send_daily_digest_if_due(send_telegram=not dry_run, save_state=not dry_run)
        if debug:
            print(report)
            if digest:
                print(digest)
    except Exception as e:
        try:
            if dry_run:
                print(f"runtime error: {e}")
            else:
                bot.tg.send_message(
                    "AI trend bot\n"
                    "Report was not generated\n"
                    "Trends detected: no\n\n"
                    "System\n"
                    f"runtime error: {e}"
                )
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AI trend bot.")
    parser.add_argument("--once", action="store_true", help="Run a single scan and exit.")
    parser.add_argument("--debug", action="store_true", help="Print the generated report to stdout.")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram messages or save state.")
    parser.add_argument("--run-now", action="store_true", help="Run an immediate scan on startup, then align to wall clock.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.once:
        run_once(debug=args.debug, dry_run=args.dry_run)
    else:
        run(debug=args.debug, dry_run=args.dry_run, run_now=args.run_now)
