# Telegram Reward Bot — PostgreSQL

## Railway setup
1. Add a PostgreSQL database service to the Railway project.
2. In the bot service Variables, add/reference `DATABASE_URL` from that PostgreSQL service (Railway variable reference commonly looks like `${{Postgres.DATABASE_URL}}`; use your actual service name).
3. Keep `BOT_TOKEN` and `ADMIN_IDS` configured. Optional: `MIN_WITHDRAW`, `REFERRAL_REWARD`, `DB_POOL_SIZE`, `LOG_LEVEL`.
4. Deploy. Tables and indexes are created automatically.

## Persistence
Users, balances, locked status, referrals, withdrawals, ledger, required groups, settings, and in-progress states are stored in PostgreSQL. Redeploys preserve data while you retain the same database service. Do not delete the database.

## Existing SQLite data — important
This version does not automatically import the previous SQLite database. Keep the old bot/database or Railway Volume until the new PostgreSQL bot has been verified. Before switching, save the old SQLite file or `/backup` from the old version. The old SQLite data can be migrated separately; do not run two bot instances polling with the same Telegram token simultaneously.

## Backup
Admin `/backup` exports all tables to `telegram_bot_backup.json` and sends it in Telegram. Keep this file securely.

## Local run
Set `BOT_TOKEN`, `ADMIN_IDS`, and `DATABASE_URL`, install `requirements.txt`, then run `python bot.py`.


## Fix lỗi Railway `OSError: [Errno 101] Network is unreachable`
Nếu dùng Supabase, không dùng hostname direct `db.<project-ref>.supabase.co` trên Railway khi kết nối IPv6 không được hỗ trợ. Trong Supabase mở **Connect → Session pooler** (hoặc Shared Pooler), sao chép URI dạng `postgresql://...@aws-0-...pooler.supabase.com:5432/postgres` và đặt toàn bộ URI đó vào Railway → bot service → Variables → `DATABASE_URL`. Không tự thêm dấu ngoặc kép. Dùng cổng Session Pooler được Supabase hiển thị; tránh Transaction Pooler `6543` nếu cấu hình ứng dụng không tương thích. Giữ nguyên tên biến `BOT_TOKEN` và `ADMIN_IDS`. Sau đó Redeploy.

Bản cập nhật có retry khi PostgreSQL khởi động chậm và tự bật SSL cho Supabase. Retry không thể khắc phục URL sai hoặc máy chủ không thể truy cập; khi đó cần thay `DATABASE_URL` bằng đúng Pooler URI.


## Existing Supabase schema
On startup, the bot now applies additive `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migrations for columns used by the current code. Existing rows are retained; do not delete or recreate the Supabase tables.
