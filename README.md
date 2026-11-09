# Telegram Reward Bot — Railway + Supabase

## Railway variables
- `BOT_TOKEN`: Telegram bot token
- `ADMIN_IDS`: Telegram admin numeric IDs, comma-separated
- `DATABASE_URL`: Supabase Session Pooler connection URI (port 5432)
- Optional: `DB_POOL_SIZE=5`, `LOG_LEVEL=INFO`

## Deploy
1. Replace the repository's `telegram_reward_bot/bot.py` with this version.
2. Keep the existing Railway variables, especially `BOT_TOKEN`, `ADMIN_IDS`, and `DATABASE_URL`.
3. Commit and let Railway redeploy.
4. Check the latest deployment logs.

## Database compatibility
At startup the bot creates missing tables/columns and applies a compatibility default to `users.joined_at`. Existing rows are retained; no tables are dropped. Back up the Supabase database before deploying any schema change.

Use the Supabase Session Pooler URI, normally port 5432. Never share `DATABASE_URL` or bot tokens publicly.
