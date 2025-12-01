# 📰 News & Stock Watcher Telegram Bot

A Telegram bot that:
- Monitors news for selected companies (via Google News / SerpAPI)
- Tracks stock prices and alerts when changes exceed a threshold
- Supports multiple price APIs (AlphaVantage → Finnhub → TwelveData → Yahoo RapidAPI fallback)

## ⚙️ Setup

bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

Set environment variables:
bash
$env:TELEGRAM_BOT_TOKEN = "your_token"
$env:SERPAPI_KEY = "your_serpapi_key"
$env:ALPHAVANTAGE_KEY = "your_alphavantage_key"
$env:FINNHUB_KEY = "your_finnhub_key"
$env:TWELVEDATA_KEY = "your_twelvedata_key"
$env:NEWS_LANG = "en"

Then run:
bash
python bot.py


| Command                    | Description                                               |
| -------------------------- | --------------------------------------------------------- |
| `/start`                   | Show help and available commands                          |
| `/help`                    | Same as `/start`                                          |
| `/watch_company <name>`    | Start tracking a company’s news                           |
| `/unwatch_company <name>`  | Stop tracking company news                                |
| `/watch_ticker <symbol>`   | Start tracking a stock ticker (e.g. `/watch_ticker NVDA`) |
| `/unwatch_ticker <symbol>` | Stop tracking a stock ticker                              |
| `/list`                    | Show current subscriptions and settings                   |
| `/interval <minutes>`      | Set how often to check (default: 10 min, min: 2)          |
| `/threshold <percent>`     | Set price change alert threshold (default: 2%)            |
| `/start_feed`              | Start background monitoring loop                          |
| `/stop_feed`               | Stop monitoring loop                                      |
