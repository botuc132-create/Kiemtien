import asyncio
import html
import logging
import os
import re
from itertools import count

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
MIN_WITHDRAW = max(1, int(os.getenv("MIN_WITHDRAW", "33000")))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

if not TOKEN:
    raise RuntimeError("Thiếu BOT_TOKEN trong Railway Variables")
if not ADMIN_IDS:
    raise RuntimeError("Thiếu ADMIN_IDS trong Railway Variables (ID Telegram của Admin)")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("stateless_withdraw_bot")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# RAM only: no PostgreSQL, Supabase, SQLite, JSON files, or other persistent storage.
# These temporary dictionaries are cleared when the process restarts.
user_flows = {}       # user_id -> {step, draft}
pending_requests = {} # request_id -> withdrawal request
request_counter = count(1)

WITHDRAW_BUTTON = "🏧 Rút tiền"
ADMIN_BUTTON = "🛠 Admin"
CANCEL_BUTTON = "❌ Hủy thao tác"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=WITHDRAW_BUTTON)]]
    if is_admin(user_id):
        rows.append([KeyboardButton(text=ADMIN_BUTTON)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL_BUTTON)]],
        resize_keyboard=True,
    )


def confirmation_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Xác nhận gửi Admin", callback_data=f"userconfirm:{request_id}"),
        InlineKeyboardButton(text="❌ Hủy", callback_data=f"usercancel:{request_id}"),
    ]])


def admin_decision_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Đã duyệt", callback_data=f"adminapprove:{request_id}"),
        InlineKeyboardButton(text="❌ Từ chối", callback_data=f"adminreject:{request_id}"),
    ]])


@dp.message(CommandStart())
async def start(message: Message):
    user_flows.pop(message.from_user.id, None)
    await message.answer(
        "Xin chào! Bot hiện hỗ trợ gửi yêu cầu rút tiền đến Admin.\n\n"
        "Chọn <b>🏧 Rút tiền</b> để bắt đầu.",
        reply_markup=main_keyboard(message.from_user.id),
    )


@dp.message(Command("cancel"))
async def cancel_command(message: Message):
    user_flows.pop(message.from_user.id, None)
    await message.answer("Đã hủy thao tác.", reply_markup=main_keyboard(message.from_user.id))


@dp.message(F.text == CANCEL_BUTTON)
async def cancel_button(message: Message):
    user_flows.pop(message.from_user.id, None)
    await message.answer("Đã hủy thao tác.", reply_markup=main_keyboard(message.from_user.id))


@dp.message(F.text == WITHDRAW_BUTTON)
async def begin_withdrawal(message: Message):
    user_flows[message.from_user.id] = {"step": "amount", "draft": {}}
    await message.answer(
        f"💸 Nhập số tiền bạn muốn rút (tối thiểu {MIN_WITHDRAW:,}).\n"
        "Bot không lưu số dư; yêu cầu sẽ được gửi Admin để kiểm tra.",
        reply_markup=cancel_keyboard(),
    )


@dp.message(F.text == ADMIN_BUTTON)
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    active = len(pending_requests)
    await message.answer(
        "🛠 <b>BẢNG ADMIN</b>\n"
        f"Yêu cầu đang chờ trong phiên chạy hiện tại: <b>{active}</b>\n\n"
        "Yêu cầu rút mới sẽ được gửi trực tiếp vào cuộc trò chuyện này."
    )


@dp.message(F.text)
async def collect_withdrawal_details(message: Message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    if text.startswith("/"):
        return

    flow = user_flows.get(uid)
    if not flow:
        if text not in (WITHDRAW_BUTTON, ADMIN_BUTTON, CANCEL_BUTTON):
            await message.answer("Chọn 🏧 Rút tiền trên menu để bắt đầu.", reply_markup=main_keyboard(uid))
        return

    step = flow["step"]
    draft = flow["draft"]

    if step == "amount":
        normalized = text.replace(",", "").replace(".", "").replace(" ", "")
        if not normalized.isdigit() or int(normalized) < MIN_WITHDRAW:
            await message.answer(f"❌ Số tiền không hợp lệ. Nhập số nguyên từ {MIN_WITHDRAW:,} trở lên.", reply_markup=cancel_keyboard())
            return
        draft["amount"] = int(normalized)
        flow["step"] = "bank_account"
        await message.answer("🏦 Vui lòng nhập <b>số tài khoản ngân hàng</b>:", reply_markup=cancel_keyboard())
        return

    if step == "bank_account":
        account = re.sub(r"\s+", "", text)
        if not re.fullmatch(r"[0-9]{6,25}", account):
            await message.answer("❌ Số tài khoản chưa hợp lệ. Hãy nhập từ 6 đến 25 chữ số.", reply_markup=cancel_keyboard())
            return
        draft["bank_account"] = account
        flow["step"] = "bank_name"
        await message.answer("🏦 Tiếp theo, vui lòng nhập <b>tên ngân hàng</b>:", reply_markup=cancel_keyboard())
        return

    if step == "bank_name":
        if len(text) < 2 or len(text) > 60:
            await message.answer("❌ Tên ngân hàng không hợp lệ. Vui lòng nhập lại.", reply_markup=cancel_keyboard())
            return
        draft["bank_name"] = text
        flow["step"] = "account_holder"
        await message.answer("👤 Vui lòng nhập <b>tên chủ tài khoản</b> đúng như đăng ký tại ngân hàng:", reply_markup=cancel_keyboard())
        return

    if step == "account_holder":
        if len(text) < 2 or len(text) > 100:
            await message.answer("❌ Tên chủ tài khoản không hợp lệ. Vui lòng nhập lại.", reply_markup=cancel_keyboard())
            return
        draft["account_holder"] = text
        request_id = next(request_counter)
        draft["request_id"] = request_id
        draft["user_id"] = uid
        draft["username"] = message.from_user.username or "Không có username"
        draft["full_name"] = message.from_user.full_name or "Không rõ"
        flow["step"] = "confirm"
        flow["draft"] = draft
        safe_bank = html.escape(draft["bank_name"])
        safe_holder = html.escape(draft["account_holder"])
        await message.answer(
            "📋 <b>XÁC NHẬN YÊU CẦU RÚT TIỀN</b>\n━━━━━━━━━━━━━━━━━━"
            f"\nSố tiền: <b>{draft['amount']:,}</b>"
            f"\nSố tài khoản: <code>{draft['bank_account']}</code>"
            f"\nNgân hàng: <b>{safe_bank}</b>"
            f"\nChủ tài khoản: <b>{safe_holder}</b>"
            "\n\nKiểm tra kỹ thông tin trước khi gửi Admin.",
            reply_markup=confirmation_keyboard(request_id),
        )


@dp.callback_query(F.data.startswith("userconfirm:"))
async def confirm_withdrawal(call: CallbackQuery):
    uid = call.from_user.id
    try:
        request_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Yêu cầu không hợp lệ.", show_alert=True)
        return

    flow = user_flows.get(uid)
    if not flow or flow.get("step") != "confirm" or flow["draft"].get("request_id") != request_id:
        await call.answer("Yêu cầu đã hết hạn. Hãy bắt đầu lại.", show_alert=True)
        return

    draft = dict(flow["draft"])
    pending_requests[request_id] = draft
    user_flows.pop(uid, None)

    username = f"@{html.escape(draft['username'])}" if draft["username"] != "Không có username" else "Không có username"
    admin_text = (
        f"💸 <b>YÊU CẦU RÚT TIỀN #{request_id}</b>\n━━━━━━━━━━━━━━━━━━"
        f"\nNgười dùng: <code>{uid}</code>"
        f"\nTên: {html.escape(draft['full_name'])}"
        f"\nUsername: {username}"
        f"\nSố tiền: <b>{draft['amount']:,}</b>"
        f"\nSố tài khoản: <code>{draft['bank_account']}</code>"
        f"\nNgân hàng: <b>{html.escape(draft['bank_name'])}</b>"
        f"\nChủ tài khoản: <b>{html.escape(draft['account_holder'])}</b>"
        "\nTrạng thái: <b>Chờ Admin xử lý</b>"
    )

    sent = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text, reply_markup=admin_decision_keyboard(request_id))
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            log.exception("Cannot send withdrawal request to admin %s", admin_id)
        except Exception:
            log.exception("Unexpected error notifying admin %s", admin_id)

    await call.answer()
    if sent:
        await call.message.edit_text("✅ Đã gửi yêu cầu rút tiền đến Admin. Vui lòng chờ phản hồi.")
        await call.message.answer("Bạn có thể quay lại menu.", reply_markup=main_keyboard(uid))
    else:
        pending_requests.pop(request_id, None)
        await call.message.edit_text("❌ Chưa gửi được yêu cầu đến Admin. Vui lòng báo Admin kiểm tra bot đã được mở chat chưa.")
        await call.message.answer("Chọn lại chức năng khi cần.", reply_markup=main_keyboard(uid))


@dp.callback_query(F.data.startswith("usercancel:"))
async def cancel_withdrawal_confirmation(call: CallbackQuery):
    uid = call.from_user.id
    user_flows.pop(uid, None)
    await call.answer("Đã hủy yêu cầu.")
    await call.message.edit_text("🚫 Đã hủy yêu cầu rút tiền. Chưa gửi thông tin cho Admin.")
    await call.message.answer("Chọn chức năng bên dưới.", reply_markup=main_keyboard(uid))


@dp.callback_query(F.data.startswith("adminapprove:") | F.data.startswith("adminreject:"))
async def admin_decision(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Bạn không có quyền Admin.", show_alert=True)
        return

    action, request_id_text = call.data.split(":", 1)
    try:
        request_id = int(request_id_text)
    except ValueError:
        await call.answer("Mã yêu cầu không hợp lệ.", show_alert=True)
        return

    request = pending_requests.pop(request_id, None)
    if not request:
        await call.answer("Yêu cầu không còn trong bộ nhớ bot. Có thể bot vừa khởi động lại.", show_alert=True)
        return

    approved = action == "adminapprove"
    result_text = "✅ Admin đã duyệt yêu cầu rút tiền của bạn." if approved else "❌ Admin đã từ chối yêu cầu rút tiền của bạn."
    try:
        await bot.send_message(request["user_id"], result_text)
    except Exception:
        log.exception("Cannot notify user %s about decision", request["user_id"])

    await call.answer("Đã ghi nhận quyết định.")
    status = "ĐÃ DUYỆT" if approved else "ĐÃ TỪ CHỐI"
    await call.message.edit_text(call.message.text + f"\n\n<b>Trạng thái: {status}</b>\nAdmin: <code>{call.from_user.id}</code>")


async def main():
    me = await bot.get_me()
    log.info("Bot started as @%s (no database mode)", me.username)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
