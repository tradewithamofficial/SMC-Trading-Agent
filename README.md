# Trade With AM — SMC Trading Agent

Smart Money Concepts chart analysis for Windows. The application uses your existing
TradingView Desktop/MCP connection and authenticated Codex CLI. It creates and groups
chart ideas, checks their entry conditions, and optionally sends complete reports to
Telegram. **It does not automatically execute trades.**

## V1.0.0 functionality

- **Find SMC Trade Setups:** 1D → 4H → 1H for directional context, liquidity,
  support/resistance and targets; 15m and 5m for potential intraday execution.
- Considers Turtle Soup, IFVG, ICT 2022, CRT, Silver Bullet and Unicorn.
  **All six checkboxes default to selected and are demonstration-only in V1.
  Every analysis considers all six models, even if all boxes are deselected.**
- Identifies up to three evidence-based ideas, distinguishing WATCHLIST SETUP from
  CONFIRMED TRIGGER. Fewer ideas or NO TRADE / WAIT are valid outcomes.
- Drawings are organized directly through the existing MCP connection into
  `SMC Intraday Setup 1`, `SMC Intraday Setup 2`, `SMC Intraday Setup 3`, only as
  needed. Existing objects are preserved; there is no automatic deletion or
  replacement. An occupied requested group name stops creation.
- **Check Entry Conditions:** reads existing SMC groups/current market evidence,
  gives WAIT/MONITOR/conditional-entry guidance in a copyable popup, and never
  sends Telegram. It ignores the demonstration checkboxes.
- Successful setup analysis optionally sends the **complete original response**
  to Telegram, with HTML formatting, mobile-friendly tables and numbered parts.
  No summarization, automatic resend, or sending after an incomplete/failed run.

## External prerequisites

- Windows x64 and TradingView Desktop, running with your existing configured
  debug/MCP connection.
- Node.js and a working TradingView MCP server. Default connector location:
  `%USERPROFILE%\tradingview-mcp`.
- Installed and authenticated Codex CLI, plus access/usage allowance for a
  supported model. The dropdown uses the local Codex model catalogue; listing a
  model alone does not prove account access.
- Optional Telegram bot and destination chat for notifications.

TradingView, Node.js, Codex CLI, the MCP server, external accounts and their
authentication are not bundled. No global configuration is installed or changed.
The existing `SR_CODEX_PATH`, `SR_NODE_PATH`, `SR_TRADINGVIEW_MCP_DIR`, and
`SR_SYMBOLS` overrides remain supported; their names are retained for compatibility.

## Install or Customize with ChatGPT — No Coding Experience Required to Start

Copy the [installation guide](INSTALL-WITH-CHATGPT.md) into a new ChatGPT conversation
for one-step-at-a-time setup. The [full ChatGPT Master Prompt](docs/CHATGPT_MASTER_PROMPT.md)
also guides optional, focused development using Codex after installation works.
Start with the [Windows v1.0.0 release](https://github.com/tradewithamofficial/SMC-Trading-Agent/releases/tag/v1.0.0); Python/PyCharm are not needed
for its EXE, but the existing external Codex/TradingView MCP setup is required for
analysis. Future examples include implementing the visible **All Charts** checkbox;
multi-chart processing is not implemented in V1. No code or release asset changes
are made by this documentation update.

## Windows executable

Download `SMCTradingAgent-v1.0.0-windows-x64.zip` from this
repository's [Releases](https://github.com/tradewithamofficial/SMC-Trading-Agent/releases).
Extract the **whole ZIP** into a writable folder, then run
`SMCTradingAgent/SMCTradingAgent.exe`. **Python is not required separately.**
Keep `_internal`, `reference_simple`, the blank Telegram example and notices
beside the executable. Do not run inside the ZIP or copy only the EXE.
The package is unsigned; install only from the release you trust.

1. Start TradingView Desktop with your existing MCP/debug setup.
2. Launch the app. The chart/watchlist selector and local model dropdown initialize.
3. Choose one chart and an available model. All Charts is not implemented in V1.
4. Review the exact selected-chart confirmation before starting setup analysis.
5. Inspect the chart/result. Use Check Entry Conditions to reassess existing ideas.

No analysis starts merely by opening the application. Do not treat a model's
narrative or a drawing as proof that a trade trigger is confirmed. Infrastructure
errors and partial results require inspection; do not retry blindly.

## Editable prompts and local responses

Edit `reference_simple/smc_setups.txt` and
`reference_simple/smc_entry_conditions.txt` beside the EXE or source. Keep their
names and selected-chart/read-only safeguards intact. Prompt changes affect future
runs; they do not update existing chart objects.

Setup results are saved to `responses/smc_<unique>/smc-response.txt`.
Entry checks use `responses/smc_entry_conditions_<unique>/entry-conditions-response.txt`.
These local reports and logs may contain private analysis and are excluded from Git.

## Private Telegram configuration

Copy `telegram_config.example.json` to `telegram_config.local.json` beside the
EXE (or `main.py` when using source). Set `bot_token` and `chat_id` as JSON
strings; `chat_id` must be the exact numeric destination ID. Start/contact your
bot and grant the destination permissions it needs. Never publish credentials.

Only the blank example ships. Missing configuration leaves analysis available
in the copyable popup and reports that Telegram was not sent. Multipart delivery
stops at the first failure; inspect the chat before manually resending.

For source-only saved-report testing, edit `tests/manual_telegram_test.py` in
PyCharm: set `RESPONSE_FILE` (project-relative or absolute), `SYMBOL`, and
`ANALYSIS_TYPE="SMC Intraday"`. Keep `SEND_TELEGRAM=False` for a local preview in
`tests/telegram_preview.txt`; set it True only when you intend a real delivery.
This script uses the same formatter/sender as the GUI and never invokes analysis.

## Run from source / build

From the project folder, using Windows Python with Tkinter (release build: 3.14):

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Focused offline tests mock model/chart/Telegram operations:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py"
```

To build the onedir package from source:

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

The existing release directory must be preserved/moved before rebuilding.
See [installation details](INSTALL.md) and [v1.0.0 release notes](docs/RELEASE-v1.0.0.md).
The included rose icon is converted from the operator-supplied PNG. The current
transparent chart illustration is retained with operator approval. Artwork paths
remain `assets/TradeWithAM_Robot.ico` and
`assets/Futuristic AI Robot with Breakout Chart.png`.

## License

[MIT](LICENSE) for project code. Keep [third-party notices](THIRD-PARTY-NOTICES.md)
and `third_party_licenses/` with binary distributions. External products retain
their own licenses and service terms. Analysis is conditional; users decide and
execute any trades themselves.
