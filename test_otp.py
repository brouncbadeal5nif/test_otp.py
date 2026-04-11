import asyncio
import logging
import sqlite3
import html
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from io import BytesIO

import httpx
import uvicorn
from fastapi import FastAPI, Request
from PIL import Image
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    BufferedInputFile,
    FSInputFile
)

# --- CẤU HÌNH ---
BOT_TOKEN = "8762970436:AAHpz95Ua00kER-R7eLIij9lm1XGyR7nRDM"
ADMIN_ID = 7078570432
OTP_API_KEY = "8fc8e078133cde11"
OTP_BASE_URL = "https://chaycodeso3.com/api"

BANK_BIN = "970422"
BANK_ACCOUNT = "346641789567"
ACCOUNT_NAME = "VU VAN CUONG"

BASE_DIR = Path(__file__).resolve().parent
DB_NAME = str(BASE_DIR / "shop_bot.db")
PORT = int(os.getenv("PORT", "8000"))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
app = FastAPI()

HTTP_CLIENT = httpx.AsyncClient(
    timeout=httpx.Timeout(15.0, connect=5.0),
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    follow_redirects=True
)

BALANCE_LOCK = asyncio.Lock()
DEFAULT_NOTE = "📌 Ghi chú: OTP về sẽ tính tiền. Nếu sau thời gian chờ không có OTP thì hệ thống sẽ hoàn tiền."
QR_TEMPLATE_PATH = BASE_DIR / "qr_mau_nguoi_cam_giay.jpg"

# Tọa độ vùng tờ giấy theo ảnh bạn đã gửi
QR_PASTE_X = 220
QR_PASTE_Y = 500
QR_PASTE_W = 270
QR_PASTE_H = 270

# --- DANH SÁCH APP CỐ ĐỊNH HIỂN THỊ TRONG BOT ---
FIXED_APP_LIST = [
    {"Id": 1001, "Name": "Facebook"},
    {"Id": 1002, "Name": "Shopee/shopee pay"},
    {"Id": 1005, "Name": "Gmail/Google"},
    {"Id": 1010, "Name": "Instagram"},
    {"Id": 1007, "Name": "Lazada"},
    {"Id": 1021, "Name": "Grab"},
    {"Id": 1022, "Name": "wechat"},
    {"Id": 1024, "Name": "WhatsApp"},
    {"Id": 1032, "Name": "TikTok"},
    {"Id": 1030, "Name": "Twitter"},
    {"Id": 1034, "Name": "Momo"},
    {"Id": 1090, "Name": "Paypal"},
    {"Id": 1097, "Name": "Tiki"},
    {"Id": 1095, "Name": "Amazon"},
    {"Id": 1102, "Name": "My Viettel"},
    {"Id": 1136, "Name": "Roblox"},
    {"Id": 1160, "Name": "Garena"},
    {"Id": 1176, "Name": "ZaloPay"},
    {"Id": 1289, "Name": "Netflix"},
    {"Id": 1301, "Name": "MY VNPT/ DIGILIFE/MYTV/VNPT Money"},
    {"Id": 1425, "Name": "Youtube"},
    {"Id": 1432, "Name": "Highlands"},
    {"Id": 1472, "Name": "Shopee Food"},
    {"Id": 1477, "Name": "VNPAY"},
    {"Id": 1561, "Name": "Binance"},
    {"Id": 1656, "Name": "Katinat"},
    {"Id": 1869, "Name": "Claude"},
    {"Id": 1247, "Name": "Id Apple"},
    {"Id": 1195, "Name": "Dịch Vụ Khác"},
]

# --- FSM ---
class DepositState(StatesGroup):
    waiting_for_amount = State()

# --- DATABASE ---
def db():
    conn = sqlite3.connect(DB_NAME, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            balance INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_notes(
            keyword TEXT PRIMARY KEY,
            note TEXT NOT NULL
        )
    """)

    cur.execute("PRAGMA table_info(users)")
    columns = [column[1] for column in cur.fetchall()]
    if 'balance' not in columns:
        cur.execute("ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS balance_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            change_amount INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS deposit_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            memo TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            provider TEXT DEFAULT 'sepay',
            transaction_id TEXT,
            raw_payload TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            paid_at TEXT
        )
    """)

    conn.commit()
    conn.close()

def get_user(user_id):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return user

def get_balance(user_id):
    conn = db()
    try:
        row = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        return int(row["balance"]) if row else 0
    finally:
        conn.close()

def update_balance(user_id, amount, full_name=None, username=None, note=""):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO users (user_id, full_name, username, balance)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = COALESCE(excluded.full_name, users.full_name),
                username = COALESCE(excluded.username, users.username)
        """, (user_id, full_name, username))

        cur.execute("""
            UPDATE users
            SET balance = balance + ?
            WHERE user_id = ?
        """, (amount, user_id))

        cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        new_balance = int(row["balance"]) if row else None

        if new_balance is not None:
            cur.execute("""
                INSERT INTO balance_logs(user_id, change_amount, balance_after, note)
                VALUES (?, ?, ?, ?)
            """, (user_id, amount, new_balance, note))

        conn.commit()
        return new_balance
    except Exception:
        conn.rollback()
        logging.exception("Lỗi update_balance")
        return None
    finally:
        conn.close()

def set_balance(user_id, new_balance, full_name=None, username=None, note=""):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO users (user_id, full_name, username, balance)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = COALESCE(excluded.full_name, users.full_name),
                username = COALESCE(excluded.username, users.username)
        """, (user_id, full_name, username))

        cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        old_row = cur.fetchone()
        old_balance = int(old_row["balance"]) if old_row else 0

        cur.execute("""
            UPDATE users
            SET balance = ?
            WHERE user_id = ?
        """, (new_balance, user_id))

        cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        final_balance = int(row["balance"]) if row else None

        if final_balance is not None:
            change_amount = final_balance - old_balance
            cur.execute("""
                INSERT INTO balance_logs(user_id, change_amount, balance_after, note)
                VALUES (?, ?, ?, ?)
            """, (user_id, change_amount, final_balance, note))

        conn.commit()
        return final_balance
    except Exception:
        conn.rollback()
        logging.exception("Lỗi set_balance")
        return None
    finally:
        conn.close()

def save_user(user):
    conn = db()
    conn.execute("""
        INSERT INTO users (user_id, full_name, username, balance)
        VALUES (?, ?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            full_name = excluded.full_name,
            username = excluded.username
    """, (user.id, user.full_name, user.username))
    conn.commit()
    conn.close()

def get_users_with_balance():
    conn = db()
    users = conn.execute("""
        SELECT user_id, full_name, username, balance
        FROM users
        WHERE balance > 0
        ORDER BY balance DESC, user_id ASC
    """).fetchall()
    conn.close()
    return users

def create_deposit_order(user_id: int, amount: int, memo: str):
    conn = db()
    try:
        conn.execute("""
            INSERT INTO deposit_orders(user_id, amount, memo, status, provider)
            VALUES (?, ?, ?, 'pending', 'sepay')
        """, (user_id, amount, memo))
        conn.commit()
    finally:
        conn.close()

def get_pending_orders():
    conn = db()
    try:
        rows = conn.execute("""
            SELECT * FROM deposit_orders
            WHERE status = 'pending'
            ORDER BY id DESC
        """).fetchall()
        return rows
    finally:
        conn.close()

def mark_order_paid(order_id: int, transaction_id: str = "", raw_payload: str = ""):
    conn = db()
    try:
        conn.execute("""
            UPDATE deposit_orders
            SET status = 'paid',
                transaction_id = ?,
                raw_payload = ?,
                paid_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'pending'
        """, (transaction_id, raw_payload, order_id))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()

# --- APP NOTES DATABASE ---
def set_app_note(keyword, note):
    conn = db()
    conn.execute("""
        INSERT INTO app_notes(keyword, note)
        VALUES(?, ?)
        ON CONFLICT(keyword) DO UPDATE SET note=excluded.note
    """, (keyword.lower().strip(), note.strip()))
    conn.commit()
    conn.close()

def delete_app_note(keyword):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM app_notes WHERE keyword = ?", (keyword.lower().strip(),))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_all_app_notes():
    conn = db()
    rows = conn.execute("SELECT keyword, note FROM app_notes ORDER BY keyword ASC").fetchall()
    conn.close()
    return rows

def get_app_note(app_name: str):
    conn = db()
    rows = conn.execute("SELECT keyword, note FROM app_notes ORDER BY LENGTH(keyword) DESC").fetchall()
    conn.close()

    app_name_lower = app_name.lower()
    for row in rows:
        if row["keyword"] in app_name_lower:
            return row["note"]

    return DEFAULT_NOTE

def normalize_phone_vn(phone: str) -> str:
    s = "".join(ch for ch in str(phone) if ch.isdigit())

    if s.startswith("84"):
        s = "0" + s[2:]
    elif not s.startswith("0"):
        s = "0" + s

    return s

def is_valid_phone_vn(phone: str) -> bool:
    s = normalize_phone_vn(phone)
    return s.isdigit() and len(s) == 10 and s.startswith("0")

# --- API OTP ---
class ChayCodeAPI:
    def __init__(self, api_key):
        self.api_key = api_key

    async def _get(self, params):
        params['apik'] = self.api_key
        try:
            response = await HTTP_CLIENT.get(OTP_BASE_URL, params=params)
            return response.json()
        except Exception:
            logging.exception("Lỗi gọi OTP API")
            return {"ResponseCode": 1, "Msg": "Lỗi kết nối Server"}

    async def get_apps(self):
        return await self._get({'act': 'app'})

    async def request_number(self, app_id, carrier=None, prefix=None, number=None):
        params = {'act': 'number', 'appId': app_id}
        if carrier:
            params['carrier'] = carrier
        if prefix:
            params['prefix'] = prefix
        if number:
            params['number'] = number
        return await self._get(params)

    async def get_otp_code(self, request_id):
        return await self._get({'act': 'code', 'id': request_id})

otp_api = ChayCodeAPI(OTP_API_KEY)
QR_TEMPLATE_CACHE = None

async def build_qr_on_paper_image(qr_url: str) -> BufferedInputFile:
    global QR_TEMPLATE_CACHE

    resp = await HTTP_CLIENT.get(qr_url)
    resp.raise_for_status()
    qr_bytes = resp.content

    if QR_TEMPLATE_CACHE is None:
        QR_TEMPLATE_CACHE = Image.open(QR_TEMPLATE_PATH).convert("RGBA")

    template = QR_TEMPLATE_CACHE.copy()
    qr_img = Image.open(BytesIO(qr_bytes)).convert("RGBA")

    qr_size = min(QR_PASTE_W, QR_PASTE_H)
    qr_img = qr_img.resize((qr_size, qr_size))

    white_bg = Image.new("RGBA", (qr_size + 20, qr_size + 20), (255, 255, 255, 255))
    white_bg.paste(qr_img, (10, 10))

    template.paste(white_bg, (QR_PASTE_X, QR_PASTE_Y))

    output = BytesIO()
    template.save(output, format="PNG")
    output.seek(0)

    return BufferedInputFile(
        file=output.getvalue(),
        filename="qr_thanh_toan.png"
    )

async def get_fixed_apps_from_api():
    res = await otp_api.get_apps()
    if res.get("ResponseCode") != 0:
        return res

    api_apps = res.get("Result", [])
    api_map = {int(app["Id"]): app for app in api_apps if "Id" in app}

    filtered_apps = []
    for item in FIXED_APP_LIST:
        app_id = int(item["Id"])
        if app_id in api_map:
            api_item = api_map[app_id]
            filtered_apps.append({
                "Id": app_id,
                "Name": item["Name"],
                "Cost": api_item.get("Cost", 0)
            })

    return {
        "ResponseCode": 0,
        "Msg": "OK",
        "Result": filtered_apps
    }

# --- KEYBOARDS ---
def main_menu_keyboard(user_id):
    user = get_user(user_id)
    balance = user['balance'] if user else 0
    bal_text = "Vô hạn" if user_id == ADMIN_ID else f"{balance:,}đ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Số dư: {bal_text}", callback_data="refresh_bal")],
        [InlineKeyboardButton(text="📱 Thuê số OTP", callback_data="otp_list")],
        [
            InlineKeyboardButton(text="💳 Nạp tiền", callback_data="deposit"),
            InlineKeyboardButton(text="☎️ Hỗ trợ", callback_data="contact")
        ],
    ])

# --- HANDLERS ---
@dp.message(Command("start"))
async def show_menu(m: Message):
    save_user(m.from_user)
    await m.answer(
        f"👋 Chào <b>{m.from_user.full_name}</b>!",
        reply_markup=main_menu_keyboard(m.from_user.id)
    )

@dp.message(Command("help"))
async def help_command(m: Message):
    await m.answer(
        "<b>📖 Danh sách lệnh</b>\n\n"
        "/start - Mở menu\n"
        "/help - Xem lệnh\n"
        "/users - Xem danh sách user (admin)\n"
        "/thongbao [nội dung] - Gửi thông báo (admin)\n"
        "/sodu [user_id] - Xem số dư 1 user (admin)\n"
        "/khachdangdu - Xem khách còn dư tiền (admin)\n"
        "/congtien [user_id] [số_tiền] - Cộng tiền (admin)\n"
        "/trutien [user_id] [số_tiền] - Trừ tiền (admin)\n"
        "/setsodu [user_id] [số_dư_mới] - Đặt số dư (admin)\n"
        "/setnote app | nội dung - Ghi chú app (admin)\n"
        "/delnote keyword - Xóa ghi chú app (admin)\n"
        "/notes - Xem tất cả ghi chú (admin)\n"
        "/mualai [ID_App] [Số_điện_thoại] - Mua lại số cũ\n"
        "/backup - Gửi file shop_bot.db về admin (admin)\n"
    )

@dp.callback_query(F.data == "refresh_bal")
async def refresh_bal(c: CallbackQuery):
    save_user(c.from_user)
    await c.message.edit_reply_markup(reply_markup=main_menu_keyboard(c.from_user.id))
    await c.answer("Đã cập nhật số dư!")

@dp.callback_query(F.data == "contact")
async def contact_callback(c: CallbackQuery):
    await c.answer()
    await c.message.answer("☎️ Hỗ trợ: liên hệ admin của bot: @tai_khoan_xin")

# --- ADMIN HANDLERS ---
@dp.message(Command("users"))
async def admin_list_users(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    users = db().execute("SELECT * FROM users").fetchall()
    if not users:
        return await m.answer("📭 Trống.")
    lines = ["👥 <b>DANH SÁCH NGƯỜI DÙNG</b>\n"]
    for i, u in enumerate(users, 1):
        lines.append(f"{i}. {u['full_name']} (ID: <code>{u['user_id']}</code>) - <b>{u['balance']:,}đ</b>")
    await m.answer("\n".join(lines))

@dp.message(Command("backup"))
async def admin_backup_db(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    db_path = Path(DB_NAME)

    if not db_path.exists():
        return await m.answer(
            "❌ Không tìm thấy file database.\n"
            f"📂 Đường dẫn hiện tại: <code>{html.escape(str(db_path))}</code>"
        )

    try:
        file_size = db_path.stat().st_size
        time_text = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        backup_file = FSInputFile(str(db_path))

        await bot.send_document(
            chat_id=ADMIN_ID,
            document=backup_file,
            caption=(
                "✅ <b>BACKUP DATABASE THÀNH CÔNG</b>\n\n"
                f"📁 Tên file: <b>{html.escape(db_path.name)}</b>\n"
                f"📦 Dung lượng: <b>{file_size:,} bytes</b>\n"
                f"🕒 Thời gian: <b>{time_text}</b>\n"
                f"📂 Đường dẫn: <code>{html.escape(str(db_path))}</code>"
            )
        )

        await m.answer("✅ Bot đã gửi file shop_bot.db về Telegram admin.")
    except Exception as e:
        logging.exception("Lỗi backup database")
        await m.answer(
            "❌ Backup thất bại.\n"
            f"Lỗi: <code>{html.escape(str(e))}</code>"
        )

@dp.message(Command("thongbao"))
async def admin_broadcast(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    msg = m.text.replace("/thongbao", "", 1).strip()
    if not msg:
        return await m.answer("Sử dụng: /thongbao [nội dung]")
    users = db().execute("SELECT user_id FROM users").fetchall()
    sent = 0
    for u in users:
        try:
            await bot.send_message(u['user_id'], f"🔔 <b>THÔNG BÁO</b>\n\n{msg}")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await m.answer(f"✅ Đã gửi tới {sent} người.")

@dp.message(Command("sodu"))
async def admin_check_one_balance(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Sử dụng: /sodu [user_id]")

    try:
        user_id = int(parts[1])
    except Exception:
        return await m.answer("❌ user_id phải là số.")

    user = get_user(user_id)
    if not user:
        return await m.answer("Không tìm thấy.")

    balance = get_balance(user_id)
    await m.answer(f"👤 {user['full_name']}\n💰 Số dư: <b>{balance:,}đ</b>")

@dp.message(Command("khachdangdu"))
async def admin_list_positive_balance(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    users = get_users_with_balance()
    if not users:
        return await m.answer("Không có khách nào dư tiền.")
    res = ["💰 <b>KHÁCH CÒN DƯ TIỀN</b>"]
    for u in users:
        res.append(f"- {u['full_name']}: {u['balance']:,}đ")
    await m.answer("\n".join(res))

@dp.message(Command("setnote"))
async def admin_set_note(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    raw = m.text.replace("/setnote", "", 1).strip()
    if "|" not in raw:
        return await m.answer("Sử dụng: /setnote app | nội dung")
    kw, nt = raw.split("|", 1)
    set_app_note(kw, nt)
    await m.answer("✅ Đã lưu.")

@dp.message(Command("delnote"))
async def admin_delete_note(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Sử dụng: /delnote keyword")
    if delete_app_note(parts[1]):
        await m.answer("✅ Đã xóa.")
    else:
        await m.answer("❌ Không tìm thấy.")

@dp.message(Command("notes"))
async def admin_list_notes(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")
    rows = get_all_app_notes()
    if not rows:
        return await m.answer("Trống.")
    res = ["📝 <b>DANH SÁCH GHI CHÚ</b>"]
    for r in rows:
        res.append(f"- <code>{r['keyword']}</code>: {r['note']}")
    await m.answer("\n".join(res))

@dp.message(Command("congtien"))
async def admin_add_balance(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    parts = m.text.split()
    if len(parts) < 3:
        return await m.answer("Sử dụng: /congtien [user_id] [so_tien]")

    try:
        user_id = int(parts[1])
        amount = int(parts[2])
    except Exception:
        return await m.answer("❌ User ID và số tiền phải là số.")

    if amount <= 0:
        return await m.answer("❌ Số tiền phải lớn hơn 0.")

    async with BALANCE_LOCK:
        new_balance = update_balance(
            user_id,
            amount,
            note=f"Admin cộng tiền bởi {m.from_user.id}"
        )

    if new_balance is None:
        return await m.answer("❌ Không cộng được số dư.")

    await m.answer(
        f"✅ Đã cộng <b>{amount:,}đ</b> cho user <code>{user_id}</code>\n"
        f"💰 Số dư mới: <b>{new_balance:,}đ</b>"
    )

    try:
        await bot.send_message(
            user_id,
            f"💰 Admin vừa cộng thêm <b>{amount:,}đ</b> cho bạn.\n"
            f"💳 Số dư hiện tại: <b>{new_balance:,}đ</b>"
        )
    except Exception:
        logging.exception("Không gửi được thông báo cộng tiền cho khách")

@dp.message(Command("trutien"))
async def admin_sub_balance(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    parts = m.text.split()
    if len(parts) < 3:
        return await m.answer("Sử dụng: /trutien [user_id] [so_tien]")

    try:
        user_id = int(parts[1])
        amount = int(parts[2])
    except Exception:
        return await m.answer("❌ User ID và số tiền phải là số.")

    if amount <= 0:
        return await m.answer("❌ Số tiền phải lớn hơn 0.")

    async with BALANCE_LOCK:
        current_balance = get_balance(user_id)
        if amount > current_balance:
            return await m.answer(
                f"❌ Không thể trừ {amount:,}đ vì khách chỉ còn {current_balance:,}đ."
            )

        new_balance = update_balance(
            user_id,
            -amount,
            note=f"Admin trừ tiền bởi {m.from_user.id}"
        )

    if new_balance is None:
        return await m.answer("❌ Không trừ được số dư.")

    await m.answer(
        f"✅ Đã trừ <b>{amount:,}đ</b> của user <code>{user_id}</code>\n"
        f"💰 Số dư mới: <b>{new_balance:,}đ</b>"
    )

    try:
        await bot.send_message(
            user_id,
            f"💸 Admin vừa trừ <b>{amount:,}đ</b> khỏi số dư của bạn.\n"
            f"💳 Số dư hiện tại: <b>{new_balance:,}đ</b>"
        )
    except Exception:
        logging.exception("Không gửi được thông báo trừ tiền cho khách")

@dp.message(Command("setsodu"))
async def admin_set_user_balance(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    parts = m.text.split()
    if len(parts) < 3:
        return await m.answer("Sử dụng: /setsodu [user_id] [so_du_moi]")

    try:
        user_id = int(parts[1])
        new_balance_input = int(parts[2])
    except Exception:
        return await m.answer("❌ User ID và số dư phải là số.")

    if new_balance_input < 0:
        return await m.answer("❌ Số dư không được âm.")

    async with BALANCE_LOCK:
        final_balance = set_balance(
            user_id,
            new_balance_input,
            note=f"Admin đặt số dư bởi {m.from_user.id}"
        )

    if final_balance is None:
        return await m.answer("❌ Không đặt được số dư.")

    await m.answer(
        f"✅ Đã đặt số dư user <code>{user_id}</code> thành <b>{final_balance:,}đ</b>"
    )

    try:
        await bot.send_message(
            user_id,
            f"💳 Admin vừa cập nhật số dư của bạn.\n"
            f"💰 Số dư hiện tại: <b>{final_balance:,}đ</b>"
        )
    except Exception:
        logging.exception("Không gửi được thông báo set số dư cho khách")

# --- XỬ LÝ NẠP TIỀN ---
@dp.callback_query(F.data == "deposit")
async def deposit_start(c: CallbackQuery, state: FSMContext):
    await c.message.answer("⌨️ Nhập số tiền muốn nạp:\nVí dụ: 10000")
    await state.set_state(DepositState.waiting_for_amount)
    await c.answer()

@dp.message(DepositState.waiting_for_amount)
async def deposit_amount_received(m: Message, state: FSMContext):
    if not m.text or not m.text.isdigit():
        return await m.answer("Vui lòng nhập số.")

    amount = int(m.text)
    await state.clear()

    memo = f"NAP{m.from_user.id}_{int(datetime.now().timestamp())}"
    create_deposit_order(m.from_user.id, amount, memo)

    qr_url = (
        f"https://img.vietqr.io/image/"
        f"{BANK_BIN}-{BANK_ACCOUNT}-compact2.jpg"
        f"?amount={amount}&addInfo={quote(memo)}&accountName={quote(ACCOUNT_NAME)}"
    )

    customer_caption = (
        f"💰 Số tiền: {amount:,}đ\n"
        f"🏦 STK: <code>{BANK_ACCOUNT}</code>\n"
        f"👤 Chủ TK: <b>{ACCOUNT_NAME}</b>\n"
        f"📝 Nội dung CK: <code>{memo}</code>\n\n"
        f"Vui lòng quét mã QR để thanh toán.\n"
        f"Hệ thống sẽ tự cộng tiền khi SePay nhận được giao dịch khớp nội dung và số tiền."
    )

    admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Duyệt tay",
            callback_data=f"admin_approve|{m.from_user.id}|{amount}"
        ),
        InlineKeyboardButton(
            text="❌ Hủy",
            callback_data=f"admin_reject|{m.from_user.id}|{amount}"
        )
    ]])

    admin_caption = (
        f"💳 <b>YÊU CẦU NẠP TIỀN</b>\n\n"
        f"👤 Khách: {m.from_user.full_name}\n"
        f"🆔 ID: <code>{m.from_user.id}</code>\n"
        f"💰 Số tiền: <b>{amount:,}đ</b>\n"
        f"📝 Nội dung CK: <code>{memo}</code>\n"
        f"🤖 Bot đã lưu đơn chờ SePay tự duyệt."
    )

    try:
        final_img = await build_qr_on_paper_image(qr_url)
        await m.answer_photo(
            photo=final_img,
            caption=customer_caption
        )
    except Exception as e:
        logging.exception("Lỗi tạo ảnh QR thanh toán")
        safe_error = html.escape(str(e))

        await m.answer(
            f"❌ Không tạo được ảnh QR thanh toán.\n"
            f"Lỗi: <code>{safe_error}</code>\n\n"
            f"Bạn vẫn có thể chuyển khoản thủ công:\n"
            f"🏦 STK: <code>{BANK_ACCOUNT}</code>\n"
            f"👤 Chủ TK: <b>{ACCOUNT_NAME}</b>\n"
            f"📝 Nội dung CK: <code>{memo}</code>"
        )

    try:
        await bot.send_message(
            ADMIN_ID,
            admin_caption,
            reply_markup=admin_keyboard
        )
    except Exception:
        logging.exception("Không gửi được thông báo duyệt nạp tiền cho admin")

@dp.callback_query(F.data.startswith("admin_"))
async def admin_action_handler(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền.", show_alert=True)

    parts = c.data.split("|")
    action = parts[0]

    if action == "admin_approve":
        user_id = int(parts[1])
        amount = int(parts[2])

        async with BALANCE_LOCK:
            new_balance = update_balance(
                user_id,
                amount,
                note=f"Duyệt nạp tiền thủ công {amount}đ bởi admin {c.from_user.id}"
            )

        if new_balance is None:
            await c.message.edit_text(
                c.message.text + f"\n\n❌ Duyệt thất bại: không cộng được tiền cho khách."
            )
            return await c.answer("Không cộng được tiền!", show_alert=True)

        try:
            await bot.send_message(
                user_id,
                f"✅ Bạn đã được cộng <b>{amount:,}đ</b> vào số dư.\n"
                f"💰 Số dư mới: <b>{new_balance:,}đ</b>"
            )
        except Exception:
            logging.exception("Không gửi được tin nhắn cộng tiền cho khách")

        await c.message.edit_text(
            c.message.text + f"\n\n✅ Đã duyệt tay và cộng {amount:,}đ"
        )
        await c.answer("Đã duyệt.")

    elif action == "admin_reject":
        user_id = int(parts[1])
        amount = int(parts[2])

        try:
            await bot.send_message(
                user_id,
                f"❌ Yêu cầu nạp <b>{amount:,}đ</b> chưa được duyệt. Vui lòng liên hệ admin nếu cần."
            )
        except Exception:
            logging.exception("Không gửi được tin nhắn từ chối cho khách")

        await c.message.edit_text(
            c.message.text + f"\n\n❌ Đã từ chối yêu cầu nạp {amount:,}đ"
        )
        await c.answer("Đã từ chối.")

# --- XỬ LÝ OTP ---
@dp.callback_query(F.data == "otp_list")
async def otp_list_callback(c: CallbackQuery):
    save_user(c.from_user)
    res = await get_fixed_apps_from_api()

    if res.get("ResponseCode") == 0:
        btns = []

        for app_item in res["Result"]:
            try:
                cost = float(app_item.get("Cost", 0))
            except Exception:
                cost = 0.0

            sell_price = int(cost * 3000)
            app_id = int(app_item["Id"])

            btns.append([
                InlineKeyboardButton(
                    text=f"{app_item['Name']} [{app_id}] - {sell_price:,}đ",
                    callback_data=f"appinfo|{app_id}|{sell_price}|{app_item['Name']}"
                )
            ])

        btns.append([InlineKeyboardButton(text="⬅️ Quay lại", callback_data="menu")])

        await c.message.edit_text(
            "<b>Chọn dịch vụ OTP\nCHỈ BẢO HÀNH MÃ KHÔNG VỀ HOÀN TIỀN</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=btns)
        )
    else:
        await c.answer("Lỗi kết nối API", show_alert=True)

# --- XEM GHI CHÚ VÀ CHỌN NHÀ MẠNG ---
@dp.callback_query(F.data.startswith("appinfo|"))
async def app_info_callback(c: CallbackQuery):
    save_user(c.from_user)
    try:
        _, app_id, sell_price, app_name = c.data.split("|", 3)
    except Exception:
        return await c.answer("Lỗi dữ liệu!")

    carriers = ["Viettel", "Mobi", "Vina", "VNMB", "ITelecom"]
    btns = [[InlineKeyboardButton(text="🚀 Mua ngay (Ngẫu nhiên)", callback_data=f"buy|{app_id}|{sell_price}|{app_name}")]]

    row = []
    for net in carriers:
        row.append(InlineKeyboardButton(text=net, callback_data=f"buy|{app_id}|{sell_price}|{app_name}|{net}"))
        if len(row) == 3:
            btns.append(row)
            row = []
    if row:
        btns.append(row)

    btns.append([InlineKeyboardButton(text="⬅️ Quay lại danh sách", callback_data="otp_list")])

    note = get_app_note(app_name)
    await c.message.edit_text(
        f"📱 <b>{app_name}</b>\n💰 Giá: <b>{int(sell_price):,}đ</b>\n\n{note}\n\n<i>Chọn nhà mạng cụ thể:</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns)
    )

@dp.callback_query(F.data.startswith("buy|"))
async def otp_buy_callback(c: CallbackQuery):
    save_user(c.from_user)
    parts = c.data.split("|")
    app_id, sell_price, app_name = parts[1], int(parts[2]), parts[3]
    carrier = parts[4] if len(parts) > 4 else None

    user_id = c.from_user.id
    if user_id != ADMIN_ID:
        user = get_user(user_id)
        if not user or user['balance'] < sell_price:
            return await c.answer("Không đủ tiền!", show_alert=True)

    await c.message.edit_text(f"⏳ Đang lấy số {'mạng ' + carrier if carrier else ''}...")
    res = await otp_api.request_number(app_id, carrier=carrier)

    if res.get("ResponseCode") == 0:
        if user_id != ADMIN_ID:
            async with BALANCE_LOCK:
                new_balance = update_balance(
                    user_id,
                    -sell_price,
                    full_name=c.from_user.full_name,
                    username=c.from_user.username,
                    note=f"Mua số OTP app {app_name}"
                )
            if new_balance is None:
                return await c.message.edit_text("❌ Trừ tiền thất bại, vui lòng thử lại.")

        phone = res["Result"]["Number"]
        req_id = res["Result"]["Id"]
        display_phone = normalize_phone_vn(phone)
        await c.message.edit_text(
            f"✅ <b>ĐÃ LẤY SỐ</b>\n📱 App: <b>{app_name}</b>\n📞 Số: <code>{display_phone}</code>\n🕒 Đợi OTP..."
        )
        asyncio.create_task(wait_for_otp(user_id, req_id, display_phone, sell_price, (user_id == ADMIN_ID), app_name))
    else:
        await c.answer(f"Lỗi: {res.get('Msg')}", show_alert=True)

# --- MUA LẠI SỐ CŨ ---
@dp.message(Command("mualai"))
async def buy_back_number(m: Message):
    parts = m.text.split()
    if len(parts) < 3:
        return await m.answer("Cách dùng: <code>/mualai [ID_App] [Số_điện_thoại]</code>")

    try:
        app_id = int(parts[1])
    except Exception:
        return await m.answer("❌ ID App phải là số.")

    phone_number_raw = parts[2].strip()
    phone_number = normalize_phone_vn(phone_number_raw)

    if not is_valid_phone_vn(phone_number):
        return await m.answer(
            "❌ Số điện thoại không hợp lệ.\n"
            "Vui lòng nhập theo dạng <code>0xxxxxxxxx</code>"
        )

    apps_res = await get_fixed_apps_from_api()
    if apps_res.get("ResponseCode") != 0:
        return await m.answer("❌ Không lấy được danh sách app từ API.")

    selected_app = None
    for app_item in apps_res.get("Result", []):
        if int(app_item.get("Id", 0)) == app_id:
            selected_app = app_item
            break

    if not selected_app:
        return await m.answer("❌ Không tìm thấy app này trong danh sách bot đang bán.")

    try:
        cost = float(selected_app.get("Cost", 0))
    except Exception:
        cost = 0.0

    sell_price = int(cost * 3000)
    app_name = selected_app.get("Name", f"App {app_id}")

    user_id = m.from_user.id
    is_admin = (user_id == ADMIN_ID)

    if not is_admin:
        user = get_user(user_id)
        current_balance = int(user["balance"]) if user else 0

        if current_balance < sell_price:
            return await m.answer(
                f"❌ Không đủ tiền để mua lại số.\n"
                f"💰 Giá mua lại: <b>{sell_price:,}đ</b>\n"
                f"💳 Số dư hiện tại: <b>{current_balance:,}đ</b>"
            )

    await m.answer(
        f"⏳ Đang yêu cầu mua lại số <code>{phone_number}</code>...\n"
        f"📱 App: <b>{app_name}</b>\n"
        f"💰 Giá: <b>{sell_price:,}đ</b>"
    )

    res = await otp_api.request_number(app_id, number=phone_number)

    if res.get("ResponseCode") == 0:
        req_id = res["Result"]["Id"]

        if not is_admin:
            async with BALANCE_LOCK:
                new_balance = update_balance(
                    user_id,
                    -sell_price,
                    full_name=m.from_user.full_name,
                    username=m.from_user.username,
                    note=f"Mua lại số cũ app {app_name} - {phone_number}"
                )
            if new_balance is None:
                return await m.answer("❌ Trừ tiền thất bại, vui lòng thử lại.")

        await m.answer(
            f"✅ Đã kết nối lại số <code>{phone_number}</code>\n"
            f"📱 App: <b>{app_name}</b>\n"
            f"🕒 Đợi mã OTP..."
        )

        asyncio.create_task(
            wait_for_otp(
                user_id=user_id,
                req_id=req_id,
                phone=phone_number,
                sell_price=sell_price,
                is_admin=is_admin,
                app_name=app_name
            )
        )
    else:
        await m.answer(f"❌ Lỗi: {res.get('Msg')}")

async def wait_for_otp(user_id, req_id, phone, sell_price, is_admin, app_name):
    for _ in range(60):
        await asyncio.sleep(7)
        res = await otp_api.get_otp_code(req_id)
        if res.get("ResponseCode") == 0:
            await bot.send_message(
                user_id,
                f"🎯 <b>MÃ OTP:</b> <code>{res['Result']['Code']}</code>\n📱 App: <b>{app_name}</b>\n📞 Số: <code>{phone}</code>"
            )
            return
        elif res.get("ResponseCode") == 2:
            break

    if not is_admin:
        async with BALANCE_LOCK:
            new_balance = update_balance(
                user_id,
                sell_price,
                note=f"Hoàn tiền OTP hết hạn app {app_name} - {phone}"
            )

        if new_balance is not None:
            await bot.send_message(
                user_id,
                f"❌ Hết hạn số <code>{phone}</code>. Đã hoàn <b>{sell_price:,}đ</b>.\n"
                f"💰 Số dư mới: <b>{new_balance:,}đ</b>"
            )
        else:
            await bot.send_message(
                user_id,
                f"❌ Hết hạn số <code>{phone}</code> nhưng hoàn tiền lỗi, vui lòng liên hệ admin."
            )
    else:
        await bot.send_message(user_id, f"❌ Hết hạn số <code>{phone}</code> (Admin).")

@dp.callback_query(F.data == "menu")
async def menu_back(c: CallbackQuery):
    save_user(c.from_user)
    await c.message.edit_text("🏠 <b>Menu</b>", reply_markup=main_menu_keyboard(c.from_user.id))

# --- SEPAY WEBHOOK ---
def normalize_payment_text(text: str) -> str:
    if not text:
        return ""
    return "".join(ch.lower() for ch in str(text) if ch.isalnum())
def _flatten_payload(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), dict):
            return payload["data"]
        if isinstance(payload.get("transfer"), dict):
            return payload["transfer"]
    return payload if isinstance(payload, dict) else {}

def _extract_amount_content_txn(payload):
    data = _flatten_payload(payload)

    amount = 0
    content = ""
    txn_id = ""

    amount_keys = [
        "transferAmount", "amount", "transfer_amount", "creditAmount",
        "transactionAmount", "incomingAmount"
    ]
    content_keys = [
        "content", "description", "transferContent", "transactionContent",
        "referenceCode"
    ]
    txn_keys = [
        "id", "transaction_id", "transactionId", "reference", "code"
    ]

    for key in amount_keys:
        value = data.get(key)
        if value is None:
            continue
        try:
            amount = int(float(str(value).replace(",", "").strip()))
            if amount > 0:
                break
        except Exception:
            pass

    for key in content_keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            content = value.strip()
            break

    for key in txn_keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            txn_id = str(value).strip()
            break

    return amount, content, txn_id

@app.get("/")
async def root():
    return {"ok": True, "message": "Bot + SePay webhook is running"}

@app.get("/sepay/webhook")
async def sepay_webhook_get():
    return {"ok": True, "message": "SePay webhook endpoint is alive. Use POST."}

@app.post("/sepay/webhook")
async def sepay_webhook_post(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raw_text = await request.body()
        logging.warning(f"SEPAY WEBHOOK non-json body: {raw_text!r}")
        return {"ok": False, "message": "invalid json"}

    logging.info(f"SEPAY WEBHOOK payload: {payload}")

    amount, content, txn_id = _extract_amount_content_txn(payload)

    if amount <= 0 or not content:
        return {"ok": True, "message": "ignored"}

    orders = get_pending_orders()
    matched = None

    normalized_content = normalize_payment_text(content)

    for order in orders:
        normalized_memo = normalize_payment_text(order["memo"])

        if normalized_memo in normalized_content and int(order["amount"]) == int(amount):
            matched = order
            break

    if not matched:
        logging.info(
            f"SEPAY no match | amount={amount} | content={content} | normalized={normalized_content}"
        )
        return {"ok": True, "message": "no match"}

    async with BALANCE_LOCK:
        updated = mark_order_paid(
            matched["id"],
            transaction_id=txn_id,
            raw_payload=str(payload)
        )

        if not updated:
            return {"ok": True, "message": "already paid"}

        new_balance = update_balance(
            matched["user_id"],
            matched["amount"],
            note=f"SePay auto nạp tiền - memo={matched['memo']} - txn={txn_id}"
        )

    if new_balance is None:
        return {"ok": False, "message": "balance update failed"}

    try:
        await bot.send_message(
            matched["user_id"],
            f"✅ Đã nhận tiền tự động.\n"
            f"💰 Số tiền: <b>{matched['amount']:,}đ</b>\n"
            f"📝 Mã nạp: <code>{matched['memo']}</code>\n"
            f"💳 Số dư mới: <b>{new_balance:,}đ</b>"
        )
    except Exception:
        logging.exception("Không gửi được thông báo nạp tiền cho khách")

    try:
        await bot.send_message(
            ADMIN_ID,
            f"💸 <b>TỰ ĐỘNG DUYỆT NẠP TIỀN</b>\n"
            f"👤 User: <code>{matched['user_id']}</code>\n"
            f"💰 Số tiền: <b>{matched['amount']:,}đ</b>\n"
            f"📝 Memo: <code>{matched['memo']}</code>\n"
            f"🏦 Txn: <code>{html.escape(txn_id or 'N/A')}</code>"
        )
    except Exception:
        logging.exception("Không gửi được thông báo cho admin")

    return {"ok": True, "message": "processed"}

# --- RUN ---
async def run_bot():
    await dp.start_polling(bot)

async def run_web():
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    init_db()
    print("Bot + SePay webhook is running...")
    try:
        await asyncio.gather(
            run_bot(),
            run_web()
        )
    finally:
        await HTTP_CLIENT.aclose()

if __name__ == "__main__":
    asyncio.run(main())
