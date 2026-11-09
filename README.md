# Telegram Reward Bot (Railway + Supabase)

## Included
- Async Telegram bot using aiogram 3.
- Supabase PostgreSQL persistence (users, groups, balances/points, referral ledger, withdrawal requests, settings).
- Required-group verification on `/start` and before every menu action. Leaving a required group blocks use until rejoining and verifying.
- User menu: balance, referral link, points withdrawal request.
- Admin-only menu: lookup by Telegram ID, lock/unlock, add/subtract points, referral/withdrawal history, statistics, broadcast text/photo, maintenance toggle, manage required groups.
- Withdrawal requests are point-ledger requests for manual admin review. This bot does not transfer money or store bank account numbers.
- Structured logs and database transactions for balance holds and approval/rejection.

## Railway variables
Set `BOT_TOKEN`, `ADMIN_IDS` (comma-separated numeric Telegram IDs), and `DATABASE_URL`.
Optional: `MIN_WITHDRAW`, `REFERRAL_REWARD`, `LOG_LEVEL`. Defaults: `MIN_WITHDRAW=33000` and `REFERRAL_REWARD=1000` (points). If these variables already exist in Railway, update them there too.

Use the Supabase PostgreSQL connection string with `?sslmode=require`. URL-encode special characters in the password (for example `@` becomes `%40`).

## Deploy
1. Upload this project to a GitHub repository.
2. Create a Railway project from that repository.
3. Add the variables above in Railway.
4. Deploy. Railway runs `python bot.py` as a worker.
5. Add the bot as an administrator in every required group/channel so Telegram permits `getChatMember` checks. The bot must have permission to view members.
6. In the bot, open Admin → Manage groups → Add group and send `@groupusername | https://t.me/groupusername`.

## Notes
- The bot starts with zero balances. Admin can grant test points from the user lookup screen.
- Referral reward is granted once after the invitee passes group verification. Self-referrals are ignored.
- Telegram bot API cannot reliably check membership in groups/channels where the bot lacks appropriate admin access.
- This starter does not perform bank transfers, KYC, or payment processing. Do not use points as a promise of cash value without appropriate legal and financial review.
