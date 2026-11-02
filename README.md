# Stock news alert bot (GitHub Actions)

Checks US stock news every ~5 minutes and sends big good/bad news to Telegram.
Free on a **public** repo. Your keys live in GitHub Secrets, never in the code.

## Secrets (Settings → Secrets and variables → Actions → New repository secret)
| Name | Value |
|---|---|
| TELEGRAM_TOKEN | from @BotFather |
| TELEGRAM_CHAT_ID | your chat id |
| ALPACA_KEY | Alpaca API key (PK...) |
| ALPACA_SECRET | Alpaca secret |
| ANTHROPIC_API_KEY | optional — leave out for free mode |

## Optional tuning (same page → Variables tab)
- `WATCHLIST` = `NVDA,TSLA,PLTR` (only these stocks)
- `CONFIRM_MOVE_PCT` = `3` (raise to 4–5 for fewer alerts)
- `MIN_IMPACT` = `7` (AI mode only)

## Test
Actions tab → Stock news bot → Run workflow. You get a ✅ status message in Telegram.

## Notes
- Alerts arrive 5–15 min after the news (GitHub's schedule limit).
- GitHub pauses scheduled workflows after 60 days with no repo activity.
  If alerts stop, open Actions and click "Enable workflow", or edit any file.
