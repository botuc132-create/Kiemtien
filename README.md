# Telegram Bot — Railway (không dùng database)

## Chức năng
- `/start` hiển thị menu rút tiền.
- Người dùng nhập lần lượt: số tiền, số tài khoản, tên ngân hàng, tên chủ tài khoản.
- Bot hiển thị bản xác nhận; người dùng bấm xác nhận mới gửi yêu cầu cho Admin.
- Admin nhận thông tin và có nút duyệt/từ chối; bot báo kết quả cho người dùng.

## Không sử dụng dữ liệu lưu trữ
- Không dùng Supabase, PostgreSQL, SQLite, JSON hay database khác.
- Không lưu hồ sơ người dùng, số dư, lịch sử rút hoặc lịch sử referral.
- Trạng thái nhập liệu và yêu cầu đang chờ chỉ nằm tạm trong RAM của tiến trình bot. Khi Railway restart/redeploy, các trạng thái và yêu cầu chờ sẽ mất.
- Vì không có dữ liệu số dư, bot không thể kiểm tra số dư, trừ tiền, hoàn tiền hoặc lưu lịch sử. Admin tự kiểm tra và xử lý thanh toán bên ngoài bot.

## Railway Variables
Bắt buộc:
- `BOT_TOKEN`: token Telegram bot.
- `ADMIN_IDS`: ID Telegram Admin; nhiều ID cách nhau bằng dấu phẩy.

Tùy chọn:
- `MIN_WITHDRAW`: số tiền rút tối thiểu (mặc định 33000).
- `LOG_LEVEL`: mặc định INFO.

Xóa `DATABASE_URL`, `DB_POOL_SIZE`, `REFERRAL_REWARD` nếu còn tồn tại; phiên bản này không đọc các biến đó.

## Deploy
- Build command: để trống hoặc để Railway tự cài từ `requirements.txt`.
- Start command: `python bot.py`.
- Nếu repository dùng thư mục con `telegram_reward_bot`, đặt Root Directory đúng thư mục đó.

Lưu ý: người dùng cần mở chat với bot trước để bot có thể gửi thông báo kết quả. Yêu cầu chờ chỉ tồn tại trong RAM, không được khôi phục sau khi bot khởi động lại.
