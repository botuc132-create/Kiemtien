import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import json
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, FSInputFile
)
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = os.getenv("DB_PATH", "data/bot_data.sqlite3").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "33000"))
REFERRAL_REWARD = int(os.getenv("REFERRAL_REWARD", "1000"))

if not TOKEN or not ADMIN_IDS:
    raise RuntimeError("Thiếu BOT_TOKEN hoặc ADMIN_IDS trong Railway Variables")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("reward_bot")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
pool = None

MAIN_MENU = ["🏧 Rút + nạp 🏧", "👨‍💼 Cá Nhân 👨‍💼", "💌 Mời Bạn bè 💌"]
ADMIN_MENU = ["🛠 Admin"]
EXIT_TEXT = "❌ Thoát"


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def main_keyboard(uid: int) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=MAIN_MENU[0]), KeyboardButton(text=MAIN_MENU[1])],
            [KeyboardButton(text=MAIN_MENU[2])]]
    if is_admin(uid):
        rows.append([KeyboardButton(text=ADMIN_MENU[0])])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def exit_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=EXIT_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=False
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Kiểm tra người dùng", callback_data="adm:user")],
        [InlineKeyboardButton(text="📊 Thống kê", callback_data="adm:stats"), InlineKeyboardButton(text="📢 Thông báo", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="💸 Yêu cầu rút", callback_data="adm:withdrawals")],
        [InlineKeyboardButton(text="👥 Quản lý nhóm", callback_data="adm:groups")],
        [InlineKeyboardButton(text="🔧 Bảo trì", callback_data="adm:maintenance")],
    ])


class PgConnection:
    def __init__(self, conn):
        self.conn = conn

    async def execute(self, sql, *args):
        return await self.conn.execute(sql, *args)

    async def fetchrow(self, sql, *args):
        row = await self.conn.fetchrow(sql, *args)
        return dict(row) if row else None

    async def fetch(self, sql, *args):
        return [dict(r) for r in await self.conn.fetch(sql, *args)]

    async def fetchval(self, sql, *args):
        return await self.conn.fetchval(sql, *args)

    def transaction(self):
        return self.conn.transaction()


class PgPool:
    def __init__(self, pool):
        self.pool = pool

    class Acquire:
        def __init__(self, pool): self.pool, self.conn = pool, None
        async def __aenter__(self):
            self.conn = await self.pool.acquire()
            return PgConnection(self.conn)
        async def __aexit__(self, *args):
            await self.pool.release(self.conn)

    def acquire(self): return self.Acquire(self.pool)
    async def close(self): await self.pool.close()


async def init_db():
    global pool
    if not DATABASE_URL:
        raise RuntimeError("Thiếu DATABASE_URL. Hãy thêm PostgreSQL service trên Railway và liên kết DATABASE_URL.")
    dsn = DATABASE_URL
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]

    # Supabase requires TLS. Its direct database hostname may be IPv6-only;
    # on Railway use the Supabase Session/Shared Pooler URL (IPv4) instead.
    db_host = dsn.split("@", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    ssl_setting = "require" if ("supabase.co" in db_host or "pooler.supabase.com" in db_host) else None
    pool_size = max(1, min(int(os.getenv("DB_POOL_SIZE", "5")), 20))

    # Retry temporary database/network startup failures instead of crashing
    # immediately while Railway/Supabase is waking up.
    last_error = None
    for attempt in range(1, 9):
        try:
            pg = await asyncpg.create_pool(
                dsn=dsn,
                min_size=1,
                max_size=pool_size,
                command_timeout=60,
                timeout=20,
                ssl=ssl_setting,
            )
            break
        except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as exc:
            last_error = exc
            log.warning("Database connection attempt %s/8 failed: %s", attempt, type(exc).__name__)
            if attempt < 8:
                await asyncio.sleep(min(attempt * 3, 20))
    else:
        raise RuntimeError(
            "Không kết nối được PostgreSQL sau 8 lần thử. "
            "Hãy kiểm tra DATABASE_URL trong Railway. Nếu dùng Supabase, "
            "hãy dùng URL Session/Shared Pooler (host dạng *.pooler.supabase.com), "
            "không dùng hostname db.<project>.supabase.co trực tiếp. "
            f"Lỗi cuối: {type(last_error).__name__}"
        ) from last_error

    pool = PgPool(pg)
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS users (
          user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
          balance BIGINT NOT NULL DEFAULT 0, locked BOOLEAN NOT NULL DEFAULT FALSE,
          invited_by BIGINT, verified BOOLEAN NOT NULL DEFAULT FALSE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS required_groups (
          id BIGSERIAL PRIMARY KEY, chat_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
          join_url TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
          id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(user_id),
          amount BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), decided_at TIMESTAMPTZ, decided_by BIGINT
        );
        CREATE TABLE IF NOT EXISTS ledger (
          id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(user_id),
          amount BIGINT NOT NULL, reason TEXT NOT NULL, actor_id BIGINT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS user_state (user_id BIGINT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status, created_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_referral_once ON ledger(user_id, reason) WHERE reason LIKE 'referral:%';
        CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, created_at DESC);
        """)
        await c.execute("INSERT INTO settings(key,value) VALUES('maintenance','0') ON CONFLICT(key) DO NOTHING")
    log.info("PostgreSQL database ready")


async def get_state(uid: int):
    async with pool.acquire() as c:
        return await c.fetchrow("SELECT state,data FROM user_state WHERE user_id=$1", uid)

async def set_state(uid: int, state: str, data: str = ""):
    async with pool.acquire() as c:
        await c.execute("""INSERT INTO user_state(user_id,state,data) VALUES($1,$2,$3)
        ON CONFLICT(user_id) DO UPDATE SET state=EXCLUDED.state,data=EXCLUDED.data""", uid, state, data)

async def clear_state(uid: int):
    async with pool.acquire() as c:
        await c.execute("DELETE FROM user_state WHERE user_id=$1", uid)

async def ensure_user(user):
    async with pool.acquire() as c:
        await c.execute("""INSERT INTO users(user_id,username,first_name) VALUES($1,$2,$3)
        ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name, updated_at=NOW()""",
                        user.id, user.username, user.first_name)

async def user_row(uid: int):
    async with pool.acquire() as c:
        return await c.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)

async def maintenance_on() -> bool:
    async with pool.acquire() as c:
        v = await c.fetchval("SELECT value FROM settings WHERE key='maintenance'")
    return v == "1"

async def group_membership(uid: int, chat_id: str) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=uid)
        return member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.warning("Cannot check membership for group %s: %s", chat_id, e)
        return False

async def missing_groups(uid: int):
    async with pool.acquire() as c:
        groups = await c.fetch("SELECT * FROM required_groups ORDER BY id")
    missing = []
    for g in groups:
        if not await group_membership(uid, g["chat_id"]):
            missing.append(g)
    return missing

async def verify_keyboard(groups):
    rows = [[InlineKeyboardButton(text=f"📢 Tham gia nhóm {i+1}", url=g["join_url"])] for i, g in enumerate(groups)]
    rows.append([InlineKeyboardButton(text="✅ Tôi đã tham gia", callback_data="verify")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def show_verification(target, uid: int, rejoin=False):
    groups = await missing_groups(uid)
    if not groups:
        async with pool.acquire() as c:
            await c.execute("UPDATE users SET verified=TRUE, updated_at=NOW() WHERE user_id=$1", uid)
        if isinstance(target, Message):
            await target.answer("✅ Xác minh thành công! Menu bot đã được mở.", reply_markup=main_keyboard(uid))
        else:
            await target.message.answer("✅ Xác minh thành công! Menu bot đã được mở.", reply_markup=main_keyboard(uid))
        return True
    async with pool.acquire() as c:
        await c.execute("UPDATE users SET verified=FALSE WHERE user_id=$1", uid)
    title = "🔐 XÁC MINH THAM GIA NHÓM" if not rejoin else "🔒 BẠN CẦN THAM GIA LẠI NHÓM"
    body = f"{title}\n━━━━━━━━━━━━━━━━━━\nVui lòng tham gia tất cả nhóm bên dưới rồi bấm “Tôi đã tham gia”."
    kb = await verify_keyboard(groups)
    if isinstance(target, Message):
        await target.answer(body, reply_markup=kb)
    else:
        await target.message.answer(body, reply_markup=kb)
    return False

async def gate(message: Message) -> bool:
    uid = message.from_user.id
    row = await user_row(uid)
    if row and row["locked"] and not is_admin(uid):
        await message.answer("🔒 Tài khoản của bạn hiện đang bị khóa.")
        return False
    if await maintenance_on() and not is_admin(uid):
        await message.answer("🛠 Bot đang bảo trì. Vui lòng quay lại sau.")
        return False
    missing = await missing_groups(uid)
    if missing:
        await show_verification(message, uid, rejoin=True)
        return False
    return True

async def credit(uid: int, amount: int, reason: str, actor: int):
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("UPDATE users SET balance=balance+$2,updated_at=NOW() WHERE user_id=$1", uid, amount)
            await c.execute("INSERT INTO ledger(user_id,amount,reason,actor_id) VALUES($1,$2,$3,$4)", uid, amount, reason, actor)


async def award_referral_if_ready(uid: int):
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT invited_by FROM users WHERE user_id=$1", uid)
        if not row or not row["invited_by"]:
            return
        inviter = row["invited_by"]
        async with c.transaction():
            inviter_exists = await c.fetchval("SELECT 1 FROM users WHERE user_id=$1 FOR UPDATE", inviter)
            if not inviter_exists:
                return
            # Insert first; only the transaction that wins the unique constraint credits the balance.
            inserted = await c.fetchval(
                "INSERT INTO ledger(user_id,amount,reason,actor_id) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING RETURNING id",
                inviter, REFERRAL_REWARD, f"referral:{uid}", uid
            )
            if inserted:
                await c.execute("UPDATE users SET balance=balance+$2,updated_at=NOW() WHERE user_id=$1", inviter, REFERRAL_REWARD)


async def callback_user_gate(call: CallbackQuery) -> bool:
    uid = call.from_user.id
    row = await user_row(uid)
    if row and row["locked"] and not is_admin(uid):
        await call.answer("Tài khoản đang bị khóa.", show_alert=True)
        return False
    if await maintenance_on() and not is_admin(uid):
        await call.answer("Bot đang bảo trì.", show_alert=True)
        return False
    missing = await missing_groups(uid)
    if missing:
        await call.answer("Bạn cần tham gia lại nhóm.", show_alert=True)
        await show_verification(call, uid, rejoin=True)
        return False
    return True


@dp.message(Command("backup"))
async def backup_database(message: Message):
    """Export all PostgreSQL tables to a portable JSON backup."""
    if not is_admin(message.from_user.id):
        return
    backup_path = None
    try:
        tables = ["users", "required_groups", "withdrawals", "ledger", "settings", "user_state"]
        data = {}
        async with pool.acquire() as c:
            for table in tables:
                rows = await c.fetch(f"SELECT * FROM {table}")
                data[table] = rows
        fd, backup_path = tempfile.mkstemp(prefix="telegram_bot_backup_", suffix=".json")
        os.close(fd)
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x), indent=2)
        await message.answer_document(
            FSInputFile(backup_path, filename="telegram_bot_backup.json"),
            caption="✅ Bản sao lưu PostgreSQL dạng JSON. Hãy lưu file ở nơi an toàn."
        )
    except Exception:
        log.exception("Database backup failed")
        await message.answer("❌ Sao lưu thất bại. Hãy kiểm tra Deploy Logs.")
    finally:
        if backup_path and os.path.exists(backup_path):
            os.remove(backup_path)


@dp.message(CommandStart())
async def start(message: Message):
    await ensure_user(message.from_user)
    args = (message.text or "").split(maxsplit=1)
    inviter = None
    if len(args) == 2 and args[1].startswith("ref"):
        raw = args[1][3:]
        if raw.isdigit() and int(raw) != message.from_user.id:
            inviter = int(raw)
    if inviter:
        async with pool.acquire() as c:
            await c.execute("UPDATE users SET invited_by=$2 WHERE user_id=$1 AND invited_by IS NULL", message.from_user.id, inviter)
    await clear_state(message.from_user.id)
    if not await show_verification(message, message.from_user.id):
        return
    await award_referral_if_ready(message.from_user.id)
    await message.answer("Chào mừng bạn! Chọn chức năng bên dưới.", reply_markup=main_keyboard(message.from_user.id))


@dp.callback_query(F.data == "verify")
async def verify_callback(call: CallbackQuery):
    await call.answer()
    await ensure_user(call.from_user)
    ok = await show_verification(call, call.from_user.id)
    if ok:
        await award_referral_if_ready(call.from_user.id)


@dp.message(F.text | F.photo)
async def text_router(message: Message):
    uid = message.from_user.id
    await ensure_user(message.from_user)
    text_in = (message.text or "").strip()
    if text_in.startswith("/"):
        return
    if not is_admin(uid) and not await gate(message):
        return
    # The persistent reply-keyboard exit button must be handled before
    # state-specific input validation (otherwise it is mistaken for an amount).
    if text_in == EXIT_TEXT:
        await clear_state(uid)
        await message.answer("✅ Đã thoát thao tác.", reply_markup=main_keyboard(uid))
        return

    state = await get_state(uid)
    if state:
        st, data = state["state"], state["data"]
        if st == "withdraw_amount":
            if not text_in.isdigit() or int(text_in) < MIN_WITHDRAW:
                await message.answer(f"❌ Số tiền tối thiểu là {MIN_WITHDRAW:,} điểm. Vui lòng nhập lại hoặc bấm nút ❌ Thoát bên dưới.", reply_markup=exit_keyboard())
                return
            amount = int(text_in)
            row = await user_row(uid)
            if not row or row["balance"] < amount:
                await message.answer("❌ Số dư không đủ. Vui lòng nhập lại hoặc bấm nút ❌ Thoát bên dưới.", reply_markup=exit_keyboard())
                return
            async with pool.acquire() as c:
                async with c.transaction():
                    changed = await c.fetchval("UPDATE users SET balance=balance-$2 WHERE user_id=$1 AND balance >= $2 RETURNING user_id", uid, amount)
                    if not changed:
                        await message.answer("❌ Số dư không đủ.")
                        return
                    wid = await c.fetchval("INSERT INTO withdrawals(user_id,amount) VALUES($1,$2) RETURNING id", uid, amount)
                    await c.execute("INSERT INTO ledger(user_id,amount,reason,actor_id) VALUES($1,$2,$3,$4)", uid, -amount, f"withdraw_hold:{wid}", uid)
            await clear_state(uid)
            await message.answer("Đã nhận số điểm. Chọn xác nhận bên dưới để gửi yêu cầu.", reply_markup=main_keyboard(uid))
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Confirm", callback_data=f"wconfirm:{wid}"), InlineKeyboardButton(text="🚫 Cancel", callback_data=f"wcancel:{wid}")]])
            await message.answer(f"Bạn có chắc chắn tạo yêu cầu rút: <b>{amount:,} điểm</b>?\n\n❗ Đây là yêu cầu chờ Admin xem xét, không phải lệnh chuyển tiền tự động.", reply_markup=kb)
            return
        if st == "admin_lookup":
            if not text_in.isdigit():
                await message.answer("Hãy gửi ID Telegram dạng số.")
                return
            target = await user_row(int(text_in))
            await clear_state(uid)
            if not target:
                await message.answer("Không tìm thấy người dùng.", reply_markup=admin_keyboard())
                return
            uname = f"@{target['username']}" if target["username"] else "(không có username)"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔒 Khóa", callback_data=f"u:lock:{target['user_id']}"), InlineKeyboardButton(text="🔓 Mở khóa", callback_data=f"u:unlock:{target['user_id']}")],
                [InlineKeyboardButton(text="➕ Cộng điểm", callback_data=f"u:add:{target['user_id']}"), InlineKeyboardButton(text="➖ Trừ điểm", callback_data=f"u:sub:{target['user_id']}")],
                [InlineKeyboardButton(text="📩 Lịch sử mời", callback_data=f"u:refs:{target['user_id']}"), InlineKeyboardButton(text="💸 Lịch sử rút", callback_data=f"u:withdraws:{target['user_id']}")]
            ])
            await message.answer(f"👤 <b>Thông tin người dùng</b>\nID: <code>{target['user_id']}</code>\nUsername: {uname}\nTên: {target['first_name'] or '-'}\nSố dư: {target['balance']:,} điểm\nKhóa: {'Có' if target['locked'] else 'Không'}\nNgày tạo: {target['created_at']:%d/%m/%Y}", reply_markup=kb)
            return
        if st in ("admin_add", "admin_sub"):
            if not text_in.isdigit() or int(text_in) <= 0:
                await message.answer("Nhập số điểm nguyên dương.")
                return
            target_id = int(data)
            amount = int(text_in) * (1 if st == "admin_add" else -1)
            if amount < 0:
                async with pool.acquire() as c:
                    bal = await c.fetchval("SELECT balance FROM users WHERE user_id=$1", target_id)
                if bal is None or bal < -amount:
                    await message.answer("Số dư người dùng không đủ để trừ.")
                    return
            async with pool.acquire() as c:
                async with c.transaction():
                    if amount < 0:
                        changed = await c.fetchval("UPDATE users SET balance=balance+$2,updated_at=NOW() WHERE user_id=$1 AND balance >= $3 RETURNING user_id", target_id, amount, -amount)
                    else:
                        changed = await c.fetchval("UPDATE users SET balance=balance+$2,updated_at=NOW() WHERE user_id=$1 RETURNING user_id", target_id, amount)
                    if not changed:
                        await message.answer("Không thể cập nhật: người dùng không tồn tại hoặc số dư không đủ.")
                        return
                    await c.execute("INSERT INTO ledger(user_id,amount,reason,actor_id) VALUES($1,$2,$3,$4)", target_id, amount, "admin_adjustment", uid)
            await clear_state(uid)
            await message.answer("✅ Đã cập nhật số dư.", reply_markup=admin_keyboard())
            return
        if st == "admin_broadcast":
            await clear_state(uid)
            async with pool.acquire() as c:
                ids = await c.fetch("SELECT user_id FROM users WHERE locked=FALSE")
            ok = fail = 0
            for r in ids:
                try:
                    if message.photo:
                        await bot.send_photo(r["user_id"], message.photo[-1].file_id, caption=message.caption or "")
                    else:
                        await bot.send_message(r["user_id"], message.text)
                    ok += 1
                    await asyncio.sleep(0.04)
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                    try:
                        await bot.copy_message(r["user_id"], message.chat.id, message.message_id)
                        ok += 1
                    except Exception:
                        fail += 1
                except (TelegramForbiddenError, TelegramBadRequest):
                    fail += 1
                except Exception:
                    fail += 1
                    log.exception("Broadcast failed for %s", r["user_id"])
            await message.answer(f"📢 Hoàn tất gửi thông báo. Thành công: {ok}, thất bại: {fail}", reply_markup=admin_keyboard())
            return
        if st == "group_chat":
            # Chỉ cần gửi một link nhóm/kênh công khai, ví dụ https://t.me/luxyffshare
            join_url = text_in.strip()
            try:
                match = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]{5,})/?(?:\?.*)?$", join_url)
                if match:
                    chat_ref = "@" + match.group(1)
                    if not join_url.startswith(("http://", "https://")):
                        join_url = "https://" + join_url
                elif re.fullmatch(r"@[A-Za-z0-9_]{5,}", join_url):
                    chat_ref = join_url
                    join_url = "https://t.me/" + join_url[1:]
                else:
                    raise ValueError("invalid public Telegram link")

                chat = await bot.get_chat(chat_ref)
                # Bot cần có quyền xem thành viên (nên đặt bot làm quản trị viên nhóm/kênh).
                bot_member = await bot.get_chat_member(chat_id=chat.id, user_id=(await bot.get_me()).id)
                if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                    await message.answer("⚠️ Đã tìm thấy nhóm nhưng bot chưa là quản trị viên. Hãy thêm bot làm admin trong nhóm/kênh rồi gửi lại link.")
                    return
                async with pool.acquire() as c:
                    await c.execute("INSERT INTO required_groups(chat_id,title,join_url) VALUES($1,$2,$3) ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title,join_url=EXCLUDED.join_url", str(chat.id), chat.title or chat_ref, join_url)
                await clear_state(uid)
                await message.answer(f"✅ Đã thêm nhóm: <b>{chat.title or chat_ref}</b>", reply_markup=admin_keyboard())
            except Exception as e:
                log.warning("Group add failed: %s", e)
                await message.answer("❌ Không thêm được nhóm. Chỉ hỗ trợ link công khai dạng https://t.me/username. Hãy kiểm tra link và thêm bot làm quản trị viên nhóm/kênh.")
            return
    if text_in == "🏧 Rút + nạp 🏧":
        row = await user_row(uid)
        await set_state(uid, "withdraw_amount")
        await message.answer(f"✅ Số tiền rút tối thiểu là {MIN_WITHDRAW:,} điểm\n\n🏧 Số dư hiện tại: {row['balance']:,} điểm\nNhập số điểm muốn yêu cầu rút. Muốn dừng, bấm ❌ Thoát bên dưới.", reply_markup=exit_keyboard())
    elif text_in == "👨‍💼 Cá Nhân 👨‍💼":
        row = await user_row(uid)
        await message.answer(f"🏧 Số Dư: <b>{row['balance']:,} điểm</b>\n🆔 ID: <code>{uid}</code>", reply_markup=exit_keyboard())
    elif text_in == "💌 Mời Bạn bè 💌":
        me = await bot.get_me()
        async with pool.acquire() as c:
            invited = await c.fetchval("SELECT COUNT(*) FROM users WHERE invited_by=$1 AND verified=TRUE", uid)
        link = f"https://t.me/{me.username}?start=ref{uid}"
        await message.answer(f"💌 Mời bạn bè\n\nMời 1 người tham gia hợp lệ\n🏧 Bạn nhận {REFERRAL_REWARD:,} điểm\n\nLink giới thiệu của bạn:\n{link}\n\nSố lượt hợp lệ: {invited}")
    elif text_in == "🛠 Admin" and is_admin(uid):
        await message.answer("🛠 <b>BẢNG ĐIỀU KHIỂN ADMIN</b>", reply_markup=admin_keyboard())


@dp.message(Command("cancel"))
async def cancel(message: Message):
    await clear_state(message.from_user.id)
    await message.answer("Đã hủy thao tác.", reply_markup=main_keyboard(message.from_user.id))


@dp.callback_query(F.data.startswith("wconfirm:"))
async def withdrawal_confirm(call: CallbackQuery):
    if not await callback_user_gate(call):
        return
    await call.answer()
    wid = int(call.data.split(":")[1])
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT w.*,u.username FROM withdrawals w JOIN users u USING(user_id) WHERE w.id=$1", wid)
    if not row or row["user_id"] != call.from_user.id or row["status"] != "pending":
        await call.message.answer("Yêu cầu không còn hợp lệ.")
        return
    async with pool.acquire() as c:
        await c.execute("UPDATE withdrawals SET status='awaiting_admin' WHERE id=$1 AND status='pending'", wid)
    for aid in ADMIN_IDS:
        uname = f"@{row['username']}" if row["username"] else "Không có username"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Duyệt", callback_data=f"wd:approve:{wid}"), InlineKeyboardButton(text="❌ Từ chối", callback_data=f"wd:reject:{wid}")]])
        try:
            await bot.send_message(aid, f"💸 <b>YÊU CẦU RÚT #{wid}</b>\nID: <code>{row['user_id']}</code>\nUsername: {uname}\nSố điểm: <b>{row['amount']:,}</b>\nTrạng thái: Chờ duyệt", reply_markup=kb)
        except Exception:
            log.exception("Cannot notify admin %s", aid)
    await call.message.edit_text("✅ Đã gửi yêu cầu đến Admin. Đây chưa phải xác nhận chuyển tiền.")


@dp.callback_query(F.data.startswith("wcancel:"))
async def withdrawal_cancel(call: CallbackQuery):
    if not await callback_user_gate(call):
        return
    await call.answer()
    wid = int(call.data.split(":")[1])
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow("SELECT * FROM withdrawals WHERE id=$1 FOR UPDATE", wid)
            if not row or row["user_id"] != call.from_user.id or row["status"] not in ("pending", "awaiting_admin"):
                await call.message.answer("Không thể hủy yêu cầu này.")
                return
            await c.execute("UPDATE withdrawals SET status='cancelled',decided_at=NOW() WHERE id=$1", wid)
            await c.execute("UPDATE users SET balance=balance+$2 WHERE user_id=$1", row["user_id"], row["amount"])
            await c.execute("INSERT INTO ledger(user_id,amount,reason,actor_id) VALUES($1,$2,$3,$4)", row["user_id"], row["amount"], f"withdraw_cancel:{wid}", call.from_user.id)
    await call.message.edit_text("🚫 Đã hủy yêu cầu. Điểm đã được hoàn lại.")


@dp.callback_query(F.data.startswith("wd:"))
async def admin_withdraw_decision(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Không có quyền.", show_alert=True); return
    _, action, wid_s = call.data.split(":")
    wid = int(wid_s)
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow("SELECT * FROM withdrawals WHERE id=$1 FOR UPDATE", wid)
            if not row or row["status"] != "awaiting_admin":
                await call.answer("Yêu cầu đã được xử lý.", show_alert=True); return
            status = "approved" if action == "approve" else "rejected"
            await c.execute("UPDATE withdrawals SET status=$2,decided_at=NOW(),decided_by=$3 WHERE id=$1", wid, status, call.from_user.id)
            if status == "rejected":
                await c.execute("UPDATE users SET balance=balance+$2 WHERE user_id=$1", row["user_id"], row["amount"])
                await c.execute("INSERT INTO ledger(user_id,amount,reason,actor_id) VALUES($1,$2,$3,$4)", row["user_id"], row["amount"], f"withdraw_rejected:{wid}", call.from_user.id)
    await call.answer("Đã xử lý.")
    await call.message.edit_text(f"Yêu cầu #{wid}: {'ĐÃ DUYỆT' if status == 'approved' else 'ĐÃ TỪ CHỐI'}")
    if status == "approved":
        await bot.send_message(row["user_id"], f"📢 <b>THÔNG BÁO RÚT THÀNH CÔNG</b>\n━━━━━━━━━━━━━━━━━━\n✅ Yêu cầu #{wid} đã được Admin duyệt. Việc chuyển tiền cần được thực hiện riêng ngoài bot.")
        # Broadcast status notice without claiming an automatic bank transfer.
        async with pool.acquire() as c:
            ids = await c.fetch("SELECT user_id FROM users WHERE locked=FALSE")
        for r in ids:
            try:
                await bot.send_message(r["user_id"], f"📢 <b>THÔNG BÁO RÚT THÀNH CÔNG</b>\n━━━━━━━━━━━━━━━━━━\n✅ Một yêu cầu rút điểm đã được Admin duyệt.")
                await asyncio.sleep(0.04)
            except Exception:
                pass
    else:
        await bot.send_message(row["user_id"], f"❌ Yêu cầu rút #{wid} bị từ chối. Điểm đã được hoàn lại.")


@dp.callback_query(F.data.startswith("adm:"))
async def admin_actions(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Không có quyền.", show_alert=True); return
    action = call.data.split(":", 1)[1]
    await call.answer()
    if action == "user":
        await set_state(call.from_user.id, "admin_lookup")
        await call.message.answer("Gửi ID Telegram người dùng cần kiểm tra.")
    elif action == "stats":
        async with pool.acquire() as c:
            total, locked, bal, pending, approved = await c.fetchrow("""SELECT
              (SELECT COUNT(*) FROM users), (SELECT COUNT(*) FROM users WHERE locked),
              (SELECT COALESCE(SUM(balance),0) FROM users),
              (SELECT COUNT(*) FROM withdrawals WHERE status='awaiting_admin'),
              (SELECT COUNT(*) FROM withdrawals WHERE status='approved')""")
        await call.message.answer(f"📊 <b>THỐNG KÊ</b>\nNgười dùng: {total}\nĐang khóa: {locked}\nTổng số dư: {bal:,} điểm\nYêu cầu chờ duyệt: {pending}\nĐã duyệt: {approved}")
    elif action == "broadcast":
        await set_state(call.from_user.id, "admin_broadcast")
        await call.message.answer("Gửi nội dung hoặc ảnh kèm chú thích để phát cho tất cả người dùng. Gửi một tin nhắn duy nhất.")
    elif action == "withdrawals":
        async with pool.acquire() as c:
            rows = await c.fetch("SELECT id,user_id,amount,created_at FROM withdrawals WHERE status='awaiting_admin' ORDER BY created_at DESC LIMIT 20")
        if not rows:
            await call.message.answer("Không có yêu cầu đang chờ.")
        for r in rows:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Duyệt", callback_data=f"wd:approve:{r['id']}"), InlineKeyboardButton(text="❌ Từ chối", callback_data=f"wd:reject:{r['id']}")]])
            await call.message.answer(f"#{r['id']} | ID {r['user_id']} | {r['amount']:,} điểm", reply_markup=kb)
    elif action == "groups":
        await call.message.answer("Quản lý nhóm:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Thêm nhóm", callback_data="grp:add")],
            [InlineKeyboardButton(text="📋 Danh sách nhóm", callback_data="grp:list")]
        ]))
    elif action == "maintenance":
        cur = await maintenance_on()
        new = "0" if cur else "1"
        async with pool.acquire() as c:
            await c.execute("UPDATE settings SET value=$1 WHERE key='maintenance'", new)
        await call.message.answer(f"🛠 Đã {'TẮT' if cur else 'BẬT'} chế độ bảo trì.")


@dp.callback_query(F.data.startswith("grp:"))
async def group_actions(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Không có quyền.", show_alert=True); return
    await call.answer()
    action = call.data.split(":", 1)[1]
    if action == "add":
        await set_state(call.from_user.id, "group_chat")
        await call.message.answer("Chỉ cần gửi link nhóm/kênh công khai, ví dụ: <code>https://t.me/luxyffshare</code>. Bot phải là quản trị viên nhóm/kênh để kiểm tra thành viên.")
    elif action == "list":
        async with pool.acquire() as c:
            groups = await c.fetch("SELECT * FROM required_groups ORDER BY id")
        if not groups:
            await call.message.answer("Chưa có nhóm bắt buộc.")
        for g in groups:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗑 Xóa nhóm", callback_data=f"grp:del:{g['id']}")]])
            await call.message.answer(f"{g['title']}\nID: <code>{g['chat_id']}</code>\n{g['join_url']}", reply_markup=kb)
    elif action.startswith("del:"):
        gid = int(action.split(":")[1])
        async with pool.acquire() as c:
            await c.execute("DELETE FROM required_groups WHERE id=$1", gid)
        await call.message.answer("✅ Đã xóa nhóm.")


@dp.callback_query(F.data.startswith("u:"))
async def user_admin_actions(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Không có quyền.", show_alert=True); return
    _, action, uid_s = call.data.split(":")
    uid = int(uid_s)
    await call.answer()
    if action in ("lock", "unlock"):
        async with pool.acquire() as c:
            await c.execute("UPDATE users SET locked=$2 WHERE user_id=$1", uid, action == "lock")
        await call.message.answer("✅ Đã cập nhật trạng thái khóa.")
    elif action in ("add", "sub"):
        await set_state(call.from_user.id, "admin_add" if action == "add" else "admin_sub", str(uid))
        await call.message.answer("Nhập số điểm cần điều chỉnh.")
    elif action in ("refs", "withdraws"):
        async with pool.acquire() as c:
            if action == "refs":
                rows = await c.fetch("SELECT user_id,created_at FROM users WHERE invited_by=$1 ORDER BY created_at DESC LIMIT 20", uid)
                body = "📩 <b>LỊCH SỬ MỜI</b>\n" + ("\n".join(f"ID {r['user_id']} — {r['created_at']:%d/%m %H:%M}" for r in rows) or "Chưa có lượt mời.")
            else:
                rows = await c.fetch("SELECT id,amount,status,created_at FROM withdrawals WHERE user_id=$1 ORDER BY created_at DESC LIMIT 20", uid)
                body = "💸 <b>LỊCH SỬ RÚT</b>\n" + ("\n".join(f"#{r['id']} — {r['amount']:,} — {r['status']} — {r['created_at']:%d/%m %H:%M}" for r in rows) or "Chưa có yêu cầu rút.")
        await call.message.answer(body)


async def main():
    await init_db()
    me = await bot.get_me()
    log.info("Bot started as @%s", me.username)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if pool:
            await pool.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
