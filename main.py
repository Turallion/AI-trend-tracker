import argparse
import time

from bot import CTTrendHunterBot
from config import load_settings


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


def run(*, debug: bool = False, dry_run: bool = False) -> None:
    validate_settings(dry_run=dry_run)
    settings = load_settings()
    bot = CTTrendHunterBot(settings)

    while True:
        try:
            report = bot.run_once(send_telegram=not dry_run, save_state=not dry_run)
            if debug:
                print(report)
        except Exception as e:
            # keep loop alive and post error into same chat
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
        time.sleep(settings.check_interval_minutes * 60)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AI trend bot.")
    parser.add_argument("--once", action="store_true", help="Run a single scan and exit.")
    parser.add_argument("--debug", action="store_true", help="Print the generated report to stdout.")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram messages or save state.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.once:
        run_once(debug=args.debug, dry_run=args.dry_run)
    else:
        run(debug=args.debug, dry_run=args.dry_run)
