"""PyCharm RUN: preview a saved report; sending is opt-in. No AI/chart calls."""

# ==================== USER SETTINGS ====================
RESPONSE_FILE = r"responses/PUT_YOUR_EXISTING_RESPONSE_PATH_HERE/smc-response.txt"

SYMBOL = "BYBIT.P"
ANALYSIS_TYPE = "SMC Intraday"

SEND_TELEGRAM = False

# =======================================================

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from telegram_sender import TelegramError, format_smc_response, send_trade_analysis


def response_path(value):
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def main():
    """Read-only preview by default; production sender only when explicitly set."""
    try:
        scope = ANALYSIS_TYPE.strip().casefold()
        if scope not in ("smc", "smc intraday"):
            raise TelegramError("Set ANALYSIS_TYPE to SMC Intraday (or SMC).")
        text = response_path(RESPONSE_FILE).read_text(encoding="utf-8-sig")
        if not text.strip():
            raise TelegramError("The selected response file is empty.")
        messages = format_smc_response(text, SYMBOL, ANALYSIS_TYPE)
        print("SAVED RESPONSE PREVIEW — no new market analysis\n")
        preview_text = "\n\n----- NEXT TELEGRAM MESSAGE -----\n\n".join(messages)
        for message in messages:
            print(message)
            print("\n----- END TELEGRAM MESSAGE -----\n")
        preview = PROJECT_ROOT / "tests" / "telegram_preview.txt"
        preview.write_text(preview_text, encoding="utf-8")
        print("\nPreview saved:", preview)
        if SEND_TELEGRAM is not True:
            print("SEND_TELEGRAM=False — no Telegram connection or credential read.")
            return 0
        report = dict(analysis_kind="simple_smc", cli_completed=True,
                      categories=["smc"], analysis_type=ANALYSIS_TYPE,
                      final_analysis=text)
        receipt = send_trade_analysis(report, SYMBOL, [], chart_verified=False, live_send_enabled=True)
        if receipt.accepted:
            print(f"Telegram accepted all {receipt.accepted_parts}/{receipt.required_parts} parts. This is a saved report, not a current entry signal.")
            return 0
        print(f"Telegram delivery NOT complete ({receipt.accepted_parts}/{receipt.required_parts} parts accepted):", receipt.error)
        print("No automatic retry. Check your chat before manually sending again.")
        return 1
    except TelegramError as exc:
        print("Telegram preview/test stopped:", exc)
    except (OSError, UnicodeError):
        print("Cannot read the saved response or write the preview. Check paths, UTF-8 encoding and permissions.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
