# Installation and usage

For one-step-at-a-time help, copy the [ChatGPT installation guide](INSTALL-WITH-CHATGPT.md)
or [full setup/customization Master Prompt](docs/CHATGPT_MASTER_PROMPT.md).

## Windows ZIP

1. Install/configure TradingView Desktop, Node.js, TradingView MCP, and an
   authenticated Codex CLI with access to your selected model.
2. Download the Windows x64 ZIP from GitHub Releases and extract the whole folder
   into a writable location. No separate Python installation is required.
3. Keep `_internal` and both `reference_simple/` prompts beside `SMCTradingAgent.exe`.
4. Start TradingView with its existing MCP/debug setup and launch the EXE.
5. Select one chart and model, then review the confirmation before analysis.

The six model boxes are demonstration-only: all six models are always considered.
The app creates up to three justified SMC ideas; Check Entry Conditions reads
existing ideas without sending Telegram. There is no automated trade execution.

## Source

Follow the relative virtual-environment commands in [README](README.md).
Runtime dependencies are pinned in `requirements.txt`; build dependencies are
in `requirements-build.txt`. Asset and prompt paths resolve relative to the
application rather than PyCharm's current working directory.

## Telegram / troubleshooting

Use your own `telegram_config.local.json`, copied from the blank example, beside
the EXE/main.py. Keys: `bot_token` and numeric-string `chat_id`. Keep it private.
No private configuration or authentication is distributed. Missing configuration
does not discard the result. The right button never sends notifications.

If the catalogue is empty, check your Codex installation/authentication. If the
watchlist falls back or the chart is ambiguous, check TradingView/MCP and use one
dedicated chart tab/pane. Review tool/approval failures instead of treating them
as a successful WAIT. Preserve partial chart results and inspect before rerunning.

See [README](README.md) for editable prompts, saved reports, binary prerequisites,
optional existing environment overrides, and the manual Telegram preview script.
