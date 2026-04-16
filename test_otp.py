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

# --- REFERRAL ---
REFERRAL_FIRST_BONUS = 3000
REFERRAL_PERCENT = 0.10
REFERRAL_MIN_DEPOSIT = 20000
BOT_USERNAME_CACHE = None
QR_EXPIRE_MINUTES = 30

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

    # Bảng referral: lưu ai giới thiệu ai
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            invited_user_id INTEGER NOT NULL UNIQUE,
            invited_full_name TEXT,
            invited_username TEXT,
            ref_code TEXT,
            first_bonus_amount INTEGER NOT NULL DEFAULT 0,
            first_bonus_paid INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # FIX DB CŨ: nếu bảng referrals đã tồn tại từ trước mà thiếu cột first_bonus_amount / first_bonus_paid
    cur.execute("PRAGMA table_info(referrals)")
    referral_columns = [column[1] for column in cur.fetchall()]
    if referral_columns and 'first_bonus_amount' not in referral_columns:
        cur.execute("ALTER TABLE referrals ADD COLUMN first_bonus_amount INTEGER NOT NULL DEFAULT 0")
    if referral_columns and 'first_bonus_paid' not in referral_columns:
        cur.execute("ALTER TABLE referrals ADD COLUMN first_bonus_paid INTEGER NOT NULL DEFAULT 0")

    # Bảng log hoa hồng 10% theo từng lần nạp
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referral_commissions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            invited_user_id INTEGER NOT NULL,
            deposit_amount INTEGER NOT NULL,
            commission_amount INTEGER NOT NULL,
            source TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO deposit_orders(user_id, amount, memo, status, provider)
            VALUES (?, ?, ?, 'pending', 'sepay')
        """, (user_id, amount, memo))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()

def get_deposit_order_by_id(order_id: int):
    conn = db()
    try:
        row = conn.execute("""
            SELECT * FROM deposit_orders
            WHERE id = ?
            LIMIT 1
        """, (order_id,)).fetchone()
        return row
    finally:
        conn.close()

def expire_old_pending_orders(minutes: int = QR_EXPIRE_MINUTES):
    conn = db()
    try:
        conn.execute("""
            UPDATE deposit_orders
            SET status = 'expired'
            WHERE status = 'pending'
              AND datetime(created_at, '+' || ? || ' minutes') <= datetime('now')
        """, (minutes,))
        conn.commit()
    finally:
        conn.close()

def is_order_expired(order_row, minutes: int = QR_EXPIRE_MINUTES):
    conn = db()
    try:
        row = conn.execute("""
            SELECT CASE
                WHEN datetime(?, '+' || ? || ' minutes') <= datetime('now') THEN 1
                ELSE 0
            END AS expired
        """, (order_row['created_at'], minutes)).fetchone()
        return bool(row['expired']) if row else False
    finally:
        conn.close()

def mark_order_expired(order_id: int):
    conn = db()
    try:
        conn.execute("""
            UPDATE deposit_orders
            SET status = 'expired'
            WHERE id = ? AND status = 'pending'
        """, (order_id,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()

def mark_order_rejected(order_id: int):
    conn = db()
    try:
        conn.execute("""
            UPDATE deposit_orders
            SET status = 'rejected'
            WHERE id = ? AND status = 'pending'
        """, (order_id,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()

async def auto_expire_deposit_order_later(order_id: int, user_id: int, amount: int, memo: str):
    await asyncio.sleep(QR_EXPIRE_MINUTES * 60)
    try:
        expired = mark_order_expired(order_id)
        if not expired:
            return
        try:
            await bot.send_message(
                user_id,
                f"⏰ Mã QR nạp tiền đã hết hạn sau <b>{QR_EXPIRE_MINUTES} phút</b>.\n"
                f"💰 Số tiền: <b>{amount:,}đ</b>\n"
                f"📝 Nội dung cũ: <code>{memo}</code>\n\n"
                "Vui lòng tạo lại mã QR mới nếu bạn vẫn muốn nạp tiền."
            )
        except Exception:
            logging.exception("Không gửi được thông báo hết hạn QR cho khách")
    except Exception:
        logging.exception("Lỗi auto_expire_deposit_order_later")

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

# --- REFERRAL DATABASE ---
def get_referral_by_invited(invited_user_id: int):
    conn = db()
    try:
        row = conn.execute("""
            SELECT * FROM referrals
            WHERE invited_user_id = ?
            LIMIT 1
        """, (invited_user_id,)).fetchone()
        return row
    finally:
        conn.close()

def get_referral_stats(user_id: int):
    conn = db()
    try:
        row1 = conn.execute("""
            SELECT COUNT(*) AS total_invited
            FROM referrals
            WHERE referrer_id = ?
        """, (user_id,)).fetchone()

        row2 = conn.execute("""
            SELECT COALESCE(SUM(first_bonus_amount), 0) AS total_first_bonus
            FROM referrals
            WHERE referrer_id = ?
        """, (user_id,)).fetchone()

        row3 = conn.execute("""
            SELECT COALESCE(SUM(commission_amount), 0) AS total_commission
            FROM referral_commissions
            WHERE referrer_id = ?
        """, (user_id,)).fetchone()

        total_invited = int(row1["total_invited"]) if row1 else 0
        total_first_bonus = int(row2["total_first_bonus"]) if row2 else 0
        total_commission = int(row3["total_commission"]) if row3 else 0
        total_bonus = total_first_bonus + total_commission

        return total_invited, total_bonus
    finally:
        conn.close()

def get_referral_history(user_id: int, limit: int = 20):
    conn = db()
    try:
        rows = conn.execute("""
            SELECT invited_user_id, invited_full_name, invited_username, ref_code, first_bonus_amount, created_at
            FROM referrals
            WHERE referrer_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
        return rows
    finally:
        conn.close()

def get_referral_commission_history(user_id: int, limit: int = 20):
    conn = db()
    try:
        rows = conn.execute("""
            SELECT invited_user_id, deposit_amount, commission_amount, source, created_at
            FROM referral_commissions
            WHERE referrer_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
        return rows
    finally:
        conn.close()

def build_ref_code(referrer_id: int) -> str:
    return f"ref_{referrer_id}"

def extract_referrer_id_from_start(text: str):
    try:
        parts = (text or "").split(maxsplit=1)
        if len(parts) < 2:
            return None

        payload = parts[1].strip()
        if not payload.startswith("ref_"):
            return None

        referrer_id = int(payload.replace("ref_", "", 1))
        return referrer_id
    except Exception:
        return None

def register_referral_atomic(referrer_id: int, invited_user):
    """
    Chỉ ghi nhận quan hệ giới thiệu, KHÔNG cộng thưởng ngay.
    Thưởng người mới + hoa hồng chỉ được trả khi user nạp >= REFERRAL_MIN_DEPOSIT.
    """
    if not referrer_id:
        return ("error", None, 0)

    if int(referrer_id) == int(invited_user.id):
        return ("self_ref", None, 0)

    conn = db()
    cur = conn.cursor()

    try:
        cur.execute("BEGIN IMMEDIATE")

        referrer = cur.execute("""
            SELECT user_id
            FROM users
            WHERE user_id = ?
            LIMIT 1
        """, (referrer_id,)).fetchone()

        if not referrer:
            conn.rollback()
            return ("referrer_not_found", None, 0)

        existed = cur.execute("""
            SELECT id
            FROM referrals
            WHERE invited_user_id = ?
            LIMIT 1
        """, (invited_user.id,)).fetchone()

        if existed:
            conn.rollback()
            return ("already_referred", None, 0)

        ref_code = build_ref_code(referrer_id)

        cur.execute("""
            INSERT INTO referrals(
                referrer_id,
                invited_user_id,
                invited_full_name,
                invited_username,
                ref_code,
                first_bonus_amount,
                first_bonus_paid
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            referrer_id,
            invited_user.id,
            invited_user.full_name,
            invited_user.username,
            ref_code,
            0,
            0
        ))

        conn.commit()
        return ("registered_pending", None, 0)

    except sqlite3.IntegrityError:
        conn.rollback()
        return ("already_referred", None, 0)
    except Exception:
        conn.rollback()
        logging.exception("Lỗi register_referral_atomic")
        return ("error", None, 0)
    finally:
        conn.close()


def apply_referral_commission_atomic(invited_user_id: int, deposit_amount: int, source: str = ""):
    """
    Logic referral đúng theo yêu cầu:
    - Lần nạp đầu tiên của user được giới thiệu phải >= REFERRAL_MIN_DEPOSIT
      => referrer nhận REFERRAL_FIRST_BONUS + 10% tiền nạp
    - Từ lần nạp thứ 2 trở đi: nạp bao nhiêu cũng được, referrer luôn nhận 10%
    """
    if deposit_amount <= 0:
        return {
            "status": "ignored",
            "referrer_id": None,
            "commission_amount": 0,
            "first_bonus_amount": 0,
            "referrer_new_balance": 0
        }

    conn = db()
    cur = conn.cursor()

    try:
        cur.execute("BEGIN IMMEDIATE")

        ref = cur.execute("""
            SELECT referrer_id, invited_user_id, first_bonus_paid
            FROM referrals
            WHERE invited_user_id = ?
            LIMIT 1
        """, (invited_user_id,)).fetchone()

        if not ref:
            conn.rollback()
            return {
                "status": "no_referrer",
                "referrer_id": None,
                "commission_amount": 0,
                "first_bonus_amount": 0,
                "referrer_new_balance": 0
            }

        referrer_id = int(ref["referrer_id"])
        is_first_qualified_reward = int(ref["first_bonus_paid"] or 0) == 0

        if is_first_qualified_reward and int(deposit_amount) < int(REFERRAL_MIN_DEPOSIT):
            conn.rollback()
            return {
                "status": "first_deposit_not_enough",
                "referrer_id": referrer_id,
                "commission_amount": 0,
                "first_bonus_amount": 0,
                "referrer_new_balance": 0
            }

        commission = int(deposit_amount * REFERRAL_PERCENT)
        first_bonus_amount = 0

        if is_first_qualified_reward:
            first_bonus_amount = int(REFERRAL_FIRST_BONUS)
            cur.execute("""
                UPDATE referrals
                SET first_bonus_paid = 1,
                    first_bonus_amount = ?
                WHERE invited_user_id = ?
            """, (first_bonus_amount, invited_user_id))

        total_reward = commission + first_bonus_amount

        if total_reward <= 0:
            conn.rollback()
            return {
                "status": "ignored",
                "referrer_id": referrer_id,
                "commission_amount": 0,
                "first_bonus_amount": 0,
                "referrer_new_balance": 0
            }

        cur.execute("""
            INSERT INTO users (user_id, full_name, username, balance)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id) DO NOTHING
        """, (referrer_id, None, None))

        cur.execute("""
            UPDATE users
            SET balance = balance + ?
            WHERE user_id = ?
        """, (total_reward, referrer_id))

        row = cur.execute("""
            SELECT balance
            FROM users
            WHERE user_id = ?
            LIMIT 1
        """, (referrer_id,)).fetchone()

        new_balance = int(row["balance"]) if row else 0

        if first_bonus_amount > 0:
            cur.execute("""
                INSERT INTO balance_logs(user_id, change_amount, balance_after, note)
                VALUES (?, ?, ?, ?)
            """, (
                referrer_id,
                first_bonus_amount,
                new_balance,
                f"Thưởng người mới referral từ user {invited_user_id} đạt lần nạp đầu tiên >= {REFERRAL_MIN_DEPOSIT}đ | nạp {deposit_amount}đ | source={source}"
            ))

        if commission > 0:
            cur.execute("""
                INSERT INTO balance_logs(user_id, change_amount, balance_after, note)
                VALUES (?, ?, ?, ?)
            """, (
                referrer_id,
                commission,
                new_balance,
                f"Hoa hồng referral 10% từ user {invited_user_id} nạp {deposit_amount}đ | source={source}"
            ))

            cur.execute("""
                INSERT INTO referral_commissions(
                    referrer_id,
                    invited_user_id,
                    deposit_amount,
                    commission_amount,
                    source
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                referrer_id,
                invited_user_id,
                deposit_amount,
                commission,
                source
            ))

        conn.commit()
        return {
            "status": "credited",
            "referrer_id": referrer_id,
            "commission_amount": commission,
            "first_bonus_amount": first_bonus_amount,
            "referrer_new_balance": new_balance
        }

    except Exception:
        conn.rollback()
        logging.exception("Lỗi apply_referral_commission_atomic")
        return {
            "status": "error",
            "referrer_id": None,
            "commission_amount": 0,
            "first_bonus_amount": 0,
            "referrer_new_balance": 0
        }
    finally:
        conn.close()

async def get_bot_username_cached():
    global BOT_USERNAME_CACHE
    if BOT_USERNAME_CACHE:
        return BOT_USERNAME_CACHE

    me = await bot.get_me()
    BOT_USERNAME_CACHE = me.username
    return BOT_USERNAME_CACHE

async def build_referral_link(referrer_id: int) -> str:
    username = await get_bot_username_cached()
    return f"https://t.me/{username}?start={build_ref_code(referrer_id)}"

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

# --- ADMIN / STATS HELPERS ---
def get_balance_history(user_id: int, limit: int = 20):
    conn = db()
    try:
        rows = conn.execute("""
            SELECT change_amount, balance_after, note, created_at
            FROM balance_logs
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
        return rows
    finally:
        conn.close()


def get_revenue_stats():
    conn = db()
    try:
        total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        paid_users = conn.execute("SELECT COUNT(DISTINCT user_id) AS c FROM deposit_orders WHERE status = 'paid'").fetchone()["c"]
        pending_orders = conn.execute("SELECT COUNT(*) AS c FROM deposit_orders WHERE status = 'pending'").fetchone()["c"]
        paid_orders = conn.execute("SELECT COUNT(*) AS c FROM deposit_orders WHERE status = 'paid'").fetchone()["c"]
        total_revenue = conn.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM deposit_orders WHERE status = 'paid'").fetchone()["s"]
        today_revenue = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) AS s
            FROM deposit_orders
            WHERE status = 'paid'
              AND DATE(COALESCE(paid_at, created_at), '+7 hours') = DATE('now', '+7 hours')
        """).fetchone()["s"]
        month_revenue = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) AS s
            FROM deposit_orders
            WHERE status = 'paid'
              AND strftime('%Y-%m', COALESCE(paid_at, created_at), '+7 hours') = strftime('%Y-%m', 'now', '+7 hours')
        """).fetchone()["s"]
        total_referral_paid = conn.execute("SELECT COALESCE(SUM(first_bonus_amount), 0) AS s FROM referrals").fetchone()["s"]
        total_referral_commission = conn.execute("SELECT COALESCE(SUM(commission_amount), 0) AS s FROM referral_commissions").fetchone()["s"]
        return {
            'total_users': int(total_users or 0),
            'paid_users': int(paid_users or 0),
            'pending_orders': int(pending_orders or 0),
            'paid_orders': int(paid_orders or 0),
            'total_revenue': int(total_revenue or 0),
            'today_revenue': int(today_revenue or 0),
            'month_revenue': int(month_revenue or 0),
            'total_referral_paid': int(total_referral_paid or 0),
            'total_referral_commission': int(total_referral_commission or 0),
        }
    finally:
        conn.close()


def admin_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Users", callback_data="admin_users"),
            InlineKeyboardButton(text="📊 Stats", callback_data="admin_stats")
        ],
        [
            InlineKeyboardButton(text="💰 Khách đang dư", callback_data="admin_positive_balance"),
            InlineKeyboardButton(text="💾 Backup DB", callback_data="admin_backup_menu")
        ],
        [InlineKeyboardButton(text="🧾 Xem lịch sử số dư", callback_data="admin_history_help")],
        [InlineKeyboardButton(text="⬅️ Về menu chính", callback_data="menu")]
    ])


def format_stats_text():
    s = get_revenue_stats()
    return (
        "📊 <b>THỐNG KÊ NHANH ADMIN</b>\n\n"
        f"👥 Tổng user: <b>{s['total_users']}</b>\n"
        f"💳 Số user đã từng nạp: <b>{s['paid_users']}</b>\n"
        f"🧾 Đơn nạp đã thanh toán: <b>{s['paid_orders']}</b>\n"
        f"⏳ Đơn nạp chờ xử lý: <b>{s['pending_orders']}</b>\n\n"
        f"💵 Doanh thu hôm nay: <b>{s['today_revenue']:,}đ</b>\n"
        f"📆 Doanh thu tháng này: <b>{s['month_revenue']:,}đ</b>\n"
        f"🏆 Tổng doanh thu all time: <b>{s['total_revenue']:,}đ</b>\n\n"
        f"🎁 Tổng thưởng người mới đã trả: <b>{s['total_referral_paid']:,}đ</b>\n"
        f"💸 Tổng hoa hồng referral đã trả: <b>{s['total_referral_commission']:,}đ</b>"
    )

# --- KEYBOARDS ---
def main_menu_keyboard(user_id):
    user = get_user(user_id)
    balance = user['balance'] if user else 0
    bal_text = "Vô hạn" if user_id == ADMIN_ID else f"{balance:,}đ"

    rows = [
        [InlineKeyboardButton(text=f"💰 Số dư: {bal_text}", callback_data="refresh_bal")],
        [InlineKeyboardButton(text="📱 Thuê số OTP", callback_data="otp_list")],
        [InlineKeyboardButton(text="🎁 Giới thiệu bạn bè", callback_data="referral_menu")],
        [
            InlineKeyboardButton(text="💳 Nạp tiền", callback_data="deposit"),
            InlineKeyboardButton(text="☎️ Hỗ trợ", callback_data="contact")
        ],
    ]

    if user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton(text="👑 Menu Admin", callback_data="admin_menu")])

    return InlineKeyboardMarkup(inline_keyboard=rows)

# --- HANDLERS ---
@dp.message(Command("start"))
async def show_menu(m: Message):
    save_user(m.from_user)

    referral_notice = ""
    referrer_id = extract_referrer_id_from_start(m.text)

    if referrer_id:
        async with BALANCE_LOCK:
            status, referrer_new_balance, first_bonus = register_referral_atomic(
                referrer_id=referrer_id,
                invited_user=m.from_user
            )

        if status == "registered_pending":
            referral_notice = (
                "\n\n🎉 Link giới thiệu hợp lệ."
                "\nTài khoản của bạn đã được ghi nhận người giới thiệu."
                f"\nNgười giới thiệu sẽ nhận thưởng khi lần nạp đầu tiên của bạn từ <b>{REFERRAL_MIN_DEPOSIT:,}đ</b> trở lên."
                f"\nKhi đạt điều kiện lần đầu, họ sẽ nhận <b>{REFERRAL_FIRST_BONUS:,}đ</b> + <b>10%</b> hoa hồng."
                "\nTừ các lần nạp sau, họ vẫn nhận <b>10%</b> dù bạn nạp bao nhiêu."
            )

            try:
                await bot.send_message(
                    referrer_id,
                    "📌 <b>ĐÃ GHI NHẬN 1 REFERRAL MỚI</b>\n\n"
                    f"👤 Người dùng: <b>{html.escape(m.from_user.full_name)}</b>\n"
                    f"🆔 ID: <code>{m.from_user.id}</code>\n\n"
                    f"⏳ Chưa cộng thưởng ngay.\n"
                    f"Người này cần nạp từ <b>{REFERRAL_MIN_DEPOSIT:,}đ</b> trở lên để bạn nhận:\n"
                    f"- Thưởng người mới: <b>{REFERRAL_FIRST_BONUS:,}đ</b>\n"
                    "- Hoa hồng: <b>10%</b> tiền nạp"
                )
            except Exception:
                logging.exception("Không gửi được thông báo referral cho referrer")

            try:
                await bot.send_message(
                    ADMIN_ID,
                    "📣 <b>PHÁT SINH REFERRAL MỚI</b>\n\n"
                    f"👤 Referrer ID: <code>{referrer_id}</code>\n"
                    f"👥 User mới: <b>{html.escape(m.from_user.full_name)}</b>\n"
                    f"🆔 Invited ID: <code>{m.from_user.id}</code>\n"
                    f"🛡 Chế độ chống spam: chỉ trả thưởng khi user nạp từ <b>{REFERRAL_MIN_DEPOSIT:,}đ</b> trở lên."
                )
            except Exception:
                logging.exception("Không gửi được thông báo referral cho admin")

        elif status == "self_ref":
            referral_notice = "\n\n⚠️ Bạn không thể tự dùng link giới thiệu của chính mình."
        elif status == "already_referred":
            referral_notice = "\n\nℹ️ Tài khoản này đã được ghi nhận referral từ trước."
        elif status == "referrer_not_found":
            referral_notice = "\n\nℹ️ Link giới thiệu không hợp lệ."
        else:
            referral_notice = "\n\n⚠️ Có lỗi khi xử lý giới thiệu, vui lòng thử lại."

    await m.answer(
        f"👋 Chào <b>{html.escape(m.from_user.full_name)}</b>!{referral_notice}",
        reply_markup=main_menu_keyboard(m.from_user.id)
    )

@dp.message(Command("help"))
async def help_command(m: Message):
    await m.answer(
        "<b>📖 Danh sách lệnh</b>\n\n"
        "/start - Mở menu\n"
        "/help - Xem lệnh\n"
        "/mualai [ID_App] [Số_điện_thoại] - Mua lại số cũ\n"
        "\n"
        "<b>🎁 Referral</b>\n"
        "Bấm nút 'Giới thiệu bạn bè' trong menu để lấy link mời\n"
        "Người mới vào đúng link sẽ được ghi nhận referral, chưa cộng tiền ngay\n"
        f"Lần nạp đầu tiên của người được giới thiệu phải từ {REFERRAL_MIN_DEPOSIT:,}đ trở lên thì bạn mới nhận {REFERRAL_FIRST_BONUS:,}đ + 10%\n"
        "Từ lần nạp thứ 2 trở đi, người đó nạp bao nhiêu cũng được và bạn vẫn nhận 10% hoa hồng\n"
        "\n"
        "<b>👑 Lệnh admin</b>\n"
        "/users - Xem danh sách user\n"
        "/thongbao [nội dung] - Gửi thông báo\n"
        "/sodu [user_id] - Xem số dư 1 user\n"
        "/khachdangdu - Xem khách còn dư tiền\n"
        "/congtien [user_id] [số_tiền] - Cộng tiền\n"
        "/trutien [user_id] [số_tiền] - Trừ tiền\n"
        "/setsodu [user_id] [số_dư_mới] - Đặt số dư\n"
        "/refstats [user_id] - Xem thống kê giới thiệu\n"
        "/setnote app | nội dung - Ghi chú app\n"
        "/delnote keyword - Xóa ghi chú app\n"
        "/notes - Xem tất cả ghi chú\n"
        "/backup - Gửi file shop_bot.db về admin\n"
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

@dp.callback_query(F.data == "referral_menu")
async def referral_menu_callback(c: CallbackQuery):
    save_user(c.from_user)

    total_invited, total_bonus = get_referral_stats(c.from_user.id)
    ref_link = await build_referral_link(c.from_user.id)

    text = (
        "<b>🎁 GIỚI THIỆU BẠN BÈ</b>\n\n"
        f"🎉 Thưởng người mới: <b>{REFERRAL_FIRST_BONUS:,}đ</b>\n"
        "💰 Hoa hồng nạp tiền: <b>10% số tiền nạp</b>\n"
        "📌 Điều kiện: người được giới thiệu bấm đúng link của bạn và vào /start\n"
        f"📌 Lần nạp đầu tiên của người đó phải từ <b>{REFERRAL_MIN_DEPOSIT:,}đ</b> trở lên\n"
        f"📌 Khi đạt điều kiện lần đầu, bạn nhận <b>{REFERRAL_FIRST_BONUS:,}đ</b> + <b>10%</b>\n"
        "📌 Từ lần nạp thứ 2 trở đi, họ nạp bao nhiêu cũng được và bạn vẫn nhận <b>10%</b>\n"
        "📌 Mỗi tài khoản chỉ ghi nhận 1 người giới thiệu\n\n"
        f"👥 Tổng số người đã giới thiệu: <b>{total_invited}</b>\n"
        f"💵 Tổng thưởng + hoa hồng đã nhận: <b>{total_bonus:,}đ</b>\n\n"
        f"🔗 Link giới thiệu của bạn:\n<code>{html.escape(ref_link)}</code>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Mở link giới thiệu", url=ref_link)],
        [InlineKeyboardButton(text="⬅️ Quay lại menu", callback_data="menu")]
    ])

    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data == "admin_menu")
async def admin_menu_callback(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền!", show_alert=True)
    await c.message.edit_text("👑 <b>MENU ADMIN</b>", reply_markup=admin_menu_keyboard())
    await c.answer()


@dp.callback_query(F.data == "admin_stats")
async def admin_stats_callback(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền!", show_alert=True)
    await c.message.edit_text(format_stats_text(), reply_markup=admin_menu_keyboard())
    await c.answer()


@dp.callback_query(F.data == "admin_users")
async def admin_users_callback(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền!", show_alert=True)

    conn = db()
    try:
        users = conn.execute("""
            SELECT * FROM users
            ORDER BY user_id DESC
        """).fetchall()
    finally:
        conn.close()

    if not users:
        await c.message.edit_text("📭 Trống.", reply_markup=admin_menu_keyboard())
        return await c.answer()

    header = f"👥 <b>TỔNG SỐ NGƯỜI DÙNG:</b> <b>{len(users)}</b>\n\n"
    chunks = []
    current = header

    for i, u in enumerate(users, 1):
        full_name = html.escape(u["full_name"] or "Không rõ tên")
        username = f"@{html.escape(u['username'])}" if u["username"] else "không username"
        line = (
            f"{i}. {full_name} | {username} | "
            f"ID: <code>{u['user_id']}</code> | "
            f"Số dư: <b>{int(u['balance']):,}đ</b>\n"
        )
        if len(current) + len(line) > 3500:
            chunks.append(current)
            current = line
        else:
            current += line

    if current.strip():
        chunks.append(current)

    await c.message.edit_text(chunks[0], reply_markup=admin_menu_keyboard())
    for idx, chunk in enumerate(chunks[1:], 2):
        await c.message.answer(f"<b>📄 Trang {idx}/{len(chunks)}</b>\n\n{chunk}")
    await c.answer()


@dp.callback_query(F.data == "admin_positive_balance")
async def admin_positive_balance_callback(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền!", show_alert=True)
    users = get_users_with_balance()
    if not users:
        await c.message.edit_text("Không có khách nào dư tiền.", reply_markup=admin_menu_keyboard())
        return await c.answer()
    res = ["💰 <b>KHÁCH CÒN DƯ TIỀN</b>"]
    for u in users[:100]:
        full_name = html.escape(u['full_name'] or 'Không rõ tên')
        res.append(f"- {full_name}: {int(u['balance']):,}đ | ID <code>{u['user_id']}</code>")
    if len(users) > 100:
        res.append(f"\n... và còn <b>{len(users) - 100}</b> khách nữa")
    await c.message.edit_text("\n".join(res), reply_markup=admin_menu_keyboard())
    await c.answer()


@dp.callback_query(F.data == "admin_backup_menu")
async def admin_backup_menu_callback(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền!", show_alert=True)

    db_path = Path(DB_NAME)
    if not db_path.exists():
        return await c.answer("❌ Không tìm thấy file database.", show_alert=True)

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
        await c.answer("Đã gửi file backup về Telegram admin")
    except Exception:
        logging.exception("Lỗi backup database từ menu admin")
        await c.answer("❌ Backup thất bại", show_alert=True)


@dp.callback_query(F.data == "admin_history_help")
async def admin_history_help_callback(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền!", show_alert=True)
    await c.message.edit_text(
        "🧾 <b>XEM LỊCH SỬ SỐ DƯ</b>\n\n"
        "Dùng lệnh:\n"
        "<code>/lichsu [user_id]</code>\n\n"
        "Ví dụ:\n"
        "<code>/lichsu 123456789</code>",
        reply_markup=admin_menu_keyboard()
    )
    await c.answer()


# --- ADMIN HANDLERS ---
@dp.message(Command("lichsu"))
async def admin_balance_history(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Sử dụng: /lichsu [user_id]")

    try:
        user_id = int(parts[1])
    except Exception:
        return await m.answer("❌ user_id phải là số.")

    user = get_user(user_id)
    if not user:
        return await m.answer("❌ Không tìm thấy user này.")

    rows = get_balance_history(user_id, limit=20)
    if not rows:
        return await m.answer(
            f"🧾 <b>LỊCH SỬ SỐ DƯ</b>\n\n"
            f"👤 User: <b>{html.escape(user['full_name'] or 'Không rõ')}</b>\n"
            f"🆔 ID: <code>{user_id}</code>\n\n"
            "Chưa có biến động số dư nào."
        )

    lines = [
        "🧾 <b>LỊCH SỬ BIẾN ĐỘNG SỐ DƯ</b>",
        f"👤 User: <b>{html.escape(user['full_name'] or 'Không rõ')}</b>",
        f"🆔 ID: <code>{user_id}</code>",
        ""
    ]

    for i, row in enumerate(rows, 1):
        change_amount = int(row["change_amount"] or 0)
        balance_after = int(row["balance_after"] or 0)
        note = html.escape(row["note"] or "Không có ghi chú")

        sign = "+" if change_amount >= 0 else ""
        lines.append(
            f"{i}. Biến động: <b>{sign}{change_amount:,}đ</b>\n"
            f"   Số dư sau: <b>{balance_after:,}đ</b>\n"
            f"   Ghi chú: {note}\n"
            f"   Thời gian: {row['created_at']}\n"
        )

    await m.answer("\n".join(lines))
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

@dp.message(Command("refstats"))
async def admin_refstats(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("❌ Bạn không có quyền!")

    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Sử dụng: /refstats [user_id]")

    try:
        user_id = int(parts[1])
    except Exception:
        return await m.answer("❌ user_id phải là số.")

    user = get_user(user_id)
    if not user:
        return await m.answer("❌ Không tìm thấy user này.")

    total_invited, total_bonus = get_referral_stats(user_id)
    history = get_referral_history(user_id, limit=20)
    commission_history = get_referral_commission_history(user_id, limit=20)

    lines = [
        "🎁 <b>THỐNG KÊ REFERRAL</b>",
        f"👤 User: <b>{html.escape(user['full_name'] or 'Không rõ')}</b>",
        f"🆔 ID: <code>{user_id}</code>",
        f"👥 Tổng số người đã giới thiệu: <b>{total_invited}</b>",
        f"💰 Tổng thưởng + hoa hồng đã nhận: <b>{total_bonus:,}đ</b>",
        "",
        "<b>🕒 20 user được giới thiệu gần nhất:</b>"
    ]

    if not history:
        lines.append("Chưa có referral nào.")
    else:
        for i, row in enumerate(history, 1):
            invited_name = row["invited_full_name"] or "Không rõ tên"
            invited_username = f"@{row['invited_username']}" if row["invited_username"] else "không username"
            first_bonus_amount = int(row["first_bonus_amount"]) if row["first_bonus_amount"] else 0
            lines.append(
                f"{i}. {html.escape(invited_name)} | {invited_username} | "
                f"ID <code>{row['invited_user_id']}</code> | "
                f"Thưởng mới <b>{first_bonus_amount:,}đ</b> | {row['created_at']}"
            )

    lines.append("")
    lines.append("<b>💸 20 lượt hoa hồng gần nhất:</b>")

    if not commission_history:
        lines.append("Chưa có hoa hồng nào.")
    else:
        for i, row in enumerate(commission_history, 1):
            lines.append(
                f"{i}. User <code>{row['invited_user_id']}</code> nạp <b>{int(row['deposit_amount']):,}đ</b> | "
                f"HH <b>{int(row['commission_amount']):,}đ</b> | "
                f"{row['created_at']}"
            )

    await m.answer("\n".join(lines))

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

    expire_old_pending_orders()

    memo = f"NAP{m.from_user.id}_{int(datetime.now().timestamp())}"
    order_id = create_deposit_order(m.from_user.id, amount, memo)

    qr_url = (
        f"https://img.vietqr.io/image/"
        f"{BANK_BIN}-{BANK_ACCOUNT}-compact2.jpg"
        f"?amount={amount}&addInfo={quote(memo)}&accountName={quote(ACCOUNT_NAME)}"
    )

    customer_caption = (
        f"💰 Số tiền: {amount:,}đ\n"
        f"🏦 STK: <code>{BANK_ACCOUNT}</code>\n"
        f"👤 Chủ TK: <b>{ACCOUNT_NAME}</b>\n"
        f"📝 Nội dung CK: <code>{memo}</code>\n"
        f"🧾 Mã đơn: <code>{order_id}</code>\n"
        f"⏰ Mã QR có hiệu lực trong <b>{QR_EXPIRE_MINUTES} phút</b>.\n\n"
        f"Vui lòng quét mã QR để thanh toán.\n"
        f"Hết {QR_EXPIRE_MINUTES} phút mà chưa thanh toán, đơn sẽ tự hủy và cần tạo mã mới."
    )

    admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Duyệt tay",
            callback_data=f"admin_approve|{order_id}"
        ),
        InlineKeyboardButton(
            text="❌ Hủy",
            callback_data=f"admin_reject|{order_id}"
        )
    ]])

    admin_caption = (
        f"💳 <b>YÊU CẦU NẠP TIỀN</b>\n\n"
        f"🧾 Order ID: <code>{order_id}</code>\n"
        f"👤 Khách: {m.from_user.full_name}\n"
        f"🆔 ID: <code>{m.from_user.id}</code>\n"
        f"💰 Số tiền: <b>{amount:,}đ</b>\n"
        f"📝 Nội dung CK: <code>{memo}</code>\n"
        f"⏰ Hết hạn sau: <b>{QR_EXPIRE_MINUTES} phút</b>\n"
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
            f"📝 Nội dung CK: <code>{memo}</code>\n"
            f"🧾 Mã đơn: <code>{order_id}</code>\n"
            f"⏰ Đơn có hiệu lực trong <b>{QR_EXPIRE_MINUTES} phút</b>."
        )

    try:
        await bot.send_message(
            ADMIN_ID,
            admin_caption,
            reply_markup=admin_keyboard
        )
    except Exception:
        logging.exception("Không gửi được thông báo duyệt nạp tiền cho admin")

    asyncio.create_task(auto_expire_deposit_order_later(order_id, m.from_user.id, amount, memo))

@dp.callback_query(F.data.startswith("admin_"))
async def admin_action_handler(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("❌ Bạn không có quyền.", show_alert=True)

    expire_old_pending_orders()

    parts = c.data.split("|")
    action = parts[0]

    if len(parts) < 2:
        return await c.answer("❌ Dữ liệu không hợp lệ.", show_alert=True)

    try:
        order_id = int(parts[1])
    except Exception:
        return await c.answer("❌ Order ID không hợp lệ.", show_alert=True)

    order = get_deposit_order_by_id(order_id)
    if not order:
        return await c.answer("❌ Không tìm thấy đơn nạp.", show_alert=True)

    if action == "admin_approve":
        if order["status"] != "pending":
            return await c.answer(f"❌ Đơn này đã ở trạng thái: {order['status']}", show_alert=True)

        if is_order_expired(order):
            mark_order_expired(order_id)
            try:
                await bot.send_message(
                    order["user_id"],
                    f"⏰ Đơn nạp <code>{order_id}</code> đã hết hạn sau <b>{QR_EXPIRE_MINUTES} phút</b>, nên admin không thể duyệt nữa.\n"
                    "Vui lòng tạo mã QR mới nếu bạn vẫn muốn nạp tiền."
                )
            except Exception:
                logging.exception("Không gửi được thông báo đơn hết hạn cho khách")
            await c.message.edit_text(c.message.text + f"\n\n⏰ Đơn {order_id} đã hết hạn, không thể duyệt.")
            return await c.answer("Đơn đã hết hạn!", show_alert=True)

        user_id = int(order["user_id"])
        amount = int(order["amount"])

        async with BALANCE_LOCK:
            updated = mark_order_paid(
                order_id,
                transaction_id=f"manual_admin_{c.from_user.id}",
                raw_payload=f"manual approve by {c.from_user.id}"
            )

            if not updated:
                return await c.answer("❌ Đơn không còn ở trạng thái chờ.", show_alert=True)

            new_balance = update_balance(
                user_id,
                amount,
                note=f"Duyệt nạp tiền thủ công order {order_id} số tiền {amount}đ bởi admin {c.from_user.id}"
            )

            referral_result = apply_referral_commission_atomic(
                invited_user_id=user_id,
                deposit_amount=amount,
                source=f"manual_admin_approve_order_{order_id}_by_{c.from_user.id}"
            )

        commission_status = referral_result.get("status")
        referrer_id = referral_result.get("referrer_id")
        commission_amount = int(referral_result.get("commission_amount", 0) or 0)
        first_bonus_amount = int(referral_result.get("first_bonus_amount", 0) or 0)
        referrer_new_balance = int(referral_result.get("referrer_new_balance", 0) or 0)

        if new_balance is None:
            await c.message.edit_text(
                c.message.text + f"\n\n❌ Duyệt thất bại: không cộng được tiền cho khách."
            )
            return await c.answer("Không cộng được tiền!", show_alert=True)

        try:
            await bot.send_message(
                user_id,
                f"✅ Bạn đã được cộng <b>{amount:,}đ</b> vào số dư.\n"
                f"🧾 Order ID: <code>{order_id}</code>\n"
                f"💰 Số dư mới: <b>{new_balance:,}đ</b>"
            )
        except Exception:
            logging.exception("Không gửi được tin nhắn cộng tiền cho khách")

        if commission_status == "credited" and referrer_id:
            try:
                await bot.send_message(
                    referrer_id,
                    "🎁 <b>BẠN VỪA NHẬN HOA HỒNG GIỚI THIỆU</b>\n\n"
                    f"👤 Người được giới thiệu vừa nạp: <code>{user_id}</code>\n"
                    f"💵 Số tiền nạp: <b>{amount:,}đ</b>\n"
                    f"💰 Hoa hồng 10%: <b>{commission_amount:,}đ</b>\n"
                    f"💳 Số dư mới: <b>{referrer_new_balance:,}đ</b>"
                )
            except Exception:
                logging.exception("Không gửi được thông báo hoa hồng referral cho referrer")

            try:
                await bot.send_message(
                    ADMIN_ID,
                    "💸 <b>ĐÃ CỘNG HOA HỒNG REFERRAL</b>\n\n"
                    f"👤 Referrer: <code>{referrer_id}</code>\n"
                    f"👥 Invited: <code>{user_id}</code>\n"
                    f"💰 Tiền nạp: <b>{amount:,}đ</b>\n"
                    f"🎁 Hoa hồng: <b>{commission_amount:,}đ</b>"
                )
            except Exception:
                logging.exception("Không gửi được log hoa hồng cho admin")

        await c.message.edit_text(
            c.message.text + f"\n\n✅ Đã duyệt tay order <code>{order_id}</code> và cộng {amount:,}đ"
        )
        await c.answer("Đã duyệt.")

    elif action == "admin_reject":
        if order["status"] != "pending":
            return await c.answer(f"❌ Đơn này đã ở trạng thái: {order['status']}", show_alert=True)

        if is_order_expired(order):
            mark_order_expired(order_id)
            await c.message.edit_text(c.message.text + f"\n\n⏰ Đơn {order_id} đã hết hạn.")
            return await c.answer("Đơn đã hết hạn!", show_alert=True)

        rejected = mark_order_rejected(order_id)
        if not rejected:
            return await c.answer("❌ Không hủy được đơn.", show_alert=True)

        user_id = int(order["user_id"])
        amount = int(order["amount"])

        try:
            await bot.send_message(
                user_id,
                f"❌ Yêu cầu nạp <b>{amount:,}đ</b> với đơn <code>{order_id}</code> đã bị hủy. Vui lòng tạo mã QR mới nếu cần."
            )
        except Exception:
            logging.exception("Không gửi được tin nhắn từ chối cho khách")

        await c.message.edit_text(
            c.message.text + f"\n\n❌ Đã hủy yêu cầu nạp order <code>{order_id}</code>"
        )
        await c.answer("Đã hủy.")

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

    expire_old_pending_orders()
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

    if is_order_expired(matched):
        mark_order_expired(int(matched["id"]))
        try:
            await bot.send_message(
                matched["user_id"],
                f"⏰ Đơn nạp <code>{matched['id']}</code> đã quá hạn {QR_EXPIRE_MINUTES} phút nên hệ thống không cộng tiền tự động.\n"
                "Vui lòng tạo mã QR mới và chuyển khoản lại đúng đơn mới."
            )
        except Exception:
            logging.exception("Không gửi được thông báo order hết hạn khi webhook tới")
        return {"ok": True, "message": "order expired"}

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
            note=f"SePay auto nạp tiền - order={matched['id']} - memo={matched['memo']} - txn={txn_id}"
        )

        referral_result = apply_referral_commission_atomic(
            invited_user_id=matched["user_id"],
            deposit_amount=matched["amount"],
            source=f"sepay:{txn_id}"
        )

    commission_status = referral_result.get("status")
    referrer_id = referral_result.get("referrer_id")
    commission_amount = int(referral_result.get("commission_amount", 0) or 0)
    first_bonus_amount = int(referral_result.get("first_bonus_amount", 0) or 0)
    referrer_new_balance = int(referral_result.get("referrer_new_balance", 0) or 0)

    if new_balance is None:
        return {"ok": False, "message": "balance update failed"}

    try:
        await bot.send_message(
            matched["user_id"],
            f"✅ Đã nhận tiền tự động.\n"
            f"🧾 Order ID: <code>{matched['id']}</code>\n"
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
            f"🧾 Order ID: <code>{matched['id']}</code>\n"
            f"👤 User: <code>{matched['user_id']}</code>\n"
            f"💰 Số tiền: <b>{matched['amount']:,}đ</b>\n"
            f"📝 Memo: <code>{matched['memo']}</code>\n"
            f"🏦 Txn: <code>{html.escape(txn_id or 'N/A')}</code>"
        )
    except Exception:
        logging.exception("Không gửi được thông báo cho admin")

    if commission_status == "credited" and referrer_id and commission_amount > 0:
        try:
            await bot.send_message(
                referrer_id,
                "🎁 <b>BẠN VỪA NHẬN HOA HỒNG GIỚI THIỆU</b>\n\n"
                f"👤 Người được giới thiệu: <code>{matched['user_id']}</code>\n"
                f"💵 Số tiền nạp: <b>{matched['amount']:,}đ</b>\n"
                f"💰 Hoa hồng 10%: <b>{commission_amount:,}đ</b>\n"
                f"💳 Số dư mới: <b>{referrer_new_balance:,}đ</b>"
            )
        except Exception:
            logging.exception("Không gửi được thông báo referral commission cho referrer")

        try:
            await bot.send_message(
                ADMIN_ID,
                "💸 <b>REFERRAL HOA HỒNG TỰ ĐỘNG</b>\n\n"
                f"👤 Referrer: <code>{referrer_id}</code>\n"
                f"👥 Invited: <code>{matched['user_id']}</code>\n"
                f"💰 Tiền nạp: <b>{matched['amount']:,}đ</b>\n"
                f"🎁 Hoa hồng: <b>{commission_amount:,}đ</b>\n"
                f"🏦 Txn: <code>{html.escape(txn_id or 'N/A')}</code>"
            )
        except Exception:
            logging.exception("Không gửi được log referral auto cho admin")

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
