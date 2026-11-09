# Telegram Reward Bot (Railway)

## Deploy
1. Upload this folder to GitHub and connect it to Railway.
2. In Railway Variables, set:
   - `BOT_TOKEN`: token from BotFather
   - `ADMIN_IDS`: Telegram numeric admin IDs, comma-separated
   - `MIN_WITHDRAW=33000`
   - `REFERRAL_REWARD=1000`
   - `LOG_LEVEL=INFO`
3. Deploy. No Supabase, PostgreSQL URL, or external database credentials are required.

## Storage note
The bot now uses a local SQLite file (`bot_data.sqlite3`) instead of Supabase/PostgreSQL. To keep balances, users, and histories across Railway redeploys/restarts, attach a Railway Volume to the service and mount it at `/app/data`, then set `DB_PATH=/app/data/bot_data.sqlite3`. Without a persistent volume, Railway may replace the local file during redeploys and stored data can be lost.

## Logs
Startup and runtime errors are written to Railway Deploy Logs. Keep `LOG_LEVEL=INFO` for normal operation.
