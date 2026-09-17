#!/usr/bin/env python3
"""Касса в группе Telegram: запись оплаты + PDF-квитанция для WhatsApp."""

from __future__ import annotations

import csv
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import telebot
from telebot.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from fpdf import FPDF

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "kassa.db"
RECEIPTS = BASE / "receipts"
RECEIPTS.mkdir(exist_ok=True)

BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "העסק שלי")
BUSINESS_ID = os.environ.get("BUSINESS_ID", "")
BUSINESS_ADDRESS = os.environ.get("BUSINESS_ADDRESS", "")
BUSINESS_PHONE = os.environ.get("BUSINESS_PHONE", "")
RECEIPT_SERIES = int(os.environ.get("RECEIPT_SERIES", "30000"))
FONT = BASE / "fonts" / "Heebo-Regular.ttf"
FONT_B = BASE / "fonts" / "Heebo-Bold.ttf"
FONT_RU = BASE / "fonts" / "DejaVuSans.ttf"


def public_no(internal_id: int) -> int:
    return RECEIPT_SERIES + internal_id


_HEB_RE = re.compile(r"[\u0590-\u05FF]+")


def heb(text: str) -> str:
    """FPDF пишет слева направо — переворачиваем только ивритские слова."""
    return _HEB_RE.sub(lambda m: m.group(0)[::-1], text)

DATE_RE = re.compile(r"^(\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)\s+(.+)$")
AMOUNT_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<amount>-?\d+(?:[.,]\d{1,2})?)"
    r"(?:\s+(?P<pay>нал|карта|перевод|bit|cash|card|מזומן|אשראי|העברה))?"
    r"\s*(?:р(?:уб(?:лей)?)?|₪|ils|nis)?\s*$",
    re.IGNORECASE,
)
PAY_MAP = {
    "нал": "מזומן",
    "cash": "מזומן",
    "מזומן": "מזומן",
    "карта": "אשראי",
    "card": "אשראי",
    "аשראי": "אשראי",
    "перевод": "העברה",
    "bit": "ביט",
    "העברה": "העברה",
}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            paid_on TEXT NOT NULL,
            client TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            client TEXT NOT NULL,
            ref TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            UNIQUE(chat_id, kind, client, ref)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (chat_id, name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            client TEXT NOT NULL,
            when_at TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def get_phone(chat_id: int, name: str) -> str:
    with db() as conn:
        row = conn.execute(
            """
            SELECT phone FROM clients
            WHERE chat_id = ? AND lower(name) = lower(?)
            """,
            (chat_id, name),
        ).fetchone()
        return row["phone"] if row else ""


def set_phone(chat_id: int, name: str, phone: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO clients (chat_id, name, phone) VALUES (?, ?, ?)
            ON CONFLICT(chat_id, name) DO UPDATE SET phone = excluded.phone
            """,
            (chat_id, name, phone),
        )


def add_appointment(chat_id: int, client: str, when_at: datetime, note: str = "") -> int:
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO appointments (chat_id, client, when_at, note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                client,
                when_at.strftime("%Y-%m-%d %H:%M"),
                note,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return int(cur.lastrowid)


def list_appointments(chat_id: int, upcoming_only: bool = False) -> list[sqlite3.Row]:
    with db() as conn:
        if upcoming_only:
            return list(
                conn.execute(
                    """
                    SELECT * FROM appointments
                    WHERE chat_id = ? AND when_at >= ?
                    ORDER BY when_at
                    """,
                    (chat_id, datetime.now().strftime("%Y-%m-%d %H:%M")),
                )
            )
        return list(
            conn.execute(
                """
                SELECT * FROM appointments
                WHERE chat_id = ?
                ORDER BY when_at DESC
                LIMIT 40
                """,
                (chat_id,),
            )
        )


def get_appointment(chat_id: int, appt_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM appointments WHERE id = ? AND chat_id = ?",
            (appt_id, chat_id),
        ).fetchone()


def delete_appointment(chat_id: int, appt_id: int) -> sqlite3.Row | None:
    row = get_appointment(chat_id, appt_id)
    if not row:
        return None
    with db() as conn:
        conn.execute(
            "DELETE FROM appointments WHERE id = ? AND chat_id = ?",
            (appt_id, chat_id),
        )
    return row


def due_appointment_reminders() -> list[sqlite3.Row]:
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    with db() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM appointments
                WHERE substr(when_at, 1, 10) = ?
                """,
                (tomorrow,),
            )
        )


def known_chat_ids() -> list[int]:
    with db() as conn:
        rows = conn.execute("SELECT DISTINCT chat_id FROM payments").fetchall()
        return [int(r["chat_id"]) for r in rows]


PENDING_PHONE: dict[int, str] = {}
LAST_MSG: dict[int, dict] = {}


def feel_ru(name: str) -> str:
    return f"Здравствуйте, {name}! Как самочувствие после процедуры?"


def feel_he(name: str) -> str:
    return f"שלום {name}, מה שלומך אחרי הטיפול? איך את מרגישה?"


def invite_ru(name: str) -> str:
    return f"Здравствуйте, {name}! Давно не виделись. Приглашаем на массаж, есть удобное время."


def invite_he(name: str) -> str:
    return f"שלום {name}, מזמן לא נפגשנו. מזמינים אותך לעיסוי, יש לנו זמן פנוי."


def visit_ru(name: str, when: datetime) -> str:
    return (
        f"Здравствуйте, {name}! Напоминаем, завтра вы записаны на "
        f"{when.strftime('%d.%m.%Y в %H:%M')}. Ждём вас."
    )


def visit_he(name: str, when: datetime) -> str:
    return (
        f"שלום {name}, תזכורת: מחר יש לך תור ב-"
        f"{when.strftime('%d.%m.%Y בשעה %H:%M')}. מחכים לך."
    )


def lang_buttons(kind: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("Русский — копировать", callback_data=f"c|{kind}|ru"),
        InlineKeyboardButton("Иврит — копировать", callback_data=f"c|{kind}|he"),
    )
    return kb


def pick_text(kind: str, name: str, when: datetime | None, lang: str) -> str:
    if kind == "feel":
        return feel_ru(name) if lang == "ru" else feel_he(name)
    if kind == "invite":
        return invite_ru(name) if lang == "ru" else invite_he(name)
    if when is None:
        when = datetime.now() + timedelta(days=1)
    return visit_ru(name, when) if lang == "ru" else visit_he(name, when)


def wa_feel(name: str) -> str:
    return (
        "Текст в WhatsApp — самочувствие\n"
        "Русский:\n"
        f"Здравствуйте, {name}! Как самочувствие после процедуры?\n\n"
        "עברית:\n"
        f"שלום {name}, מה שלומך אחרי הטיפול? איך את מרגישה?"
    )


def wa_invite(name: str) -> str:
    return (
        "Текст в WhatsApp — пригласить на массаж\n"
        "Русский:\n"
        f"Здравствуйте, {name}! Давно не виделись. Приглашаем на массаж, есть удобное время.\n\n"
        "עברית:\n"
        f"שלום {name}, מזמן לא נפגשנו. מזמינים אותך לעיסוי, יש לנו זמן פנוי."
    )


def wa_visit(name: str, when: datetime) -> str:
    ru_when = when.strftime("%d.%m.%Y в %H:%M")
    he_when = when.strftime("%d.%m.%Y בשעה %H:%M")
    return (
        "Текст в WhatsApp — напомнить о визите завтра\n"
        "Русский:\n"
        f"Здравствуйте, {name}! Напоминаем, завтра вы записаны на {ru_when}. "
        "Ждём вас.\n\n"
        "עברית:\n"
        f"שלום {name}, תזכורת: מחר יש לך תור ב-{he_when}. מחכים לך."
    )


def phone_line(chat_id: int, name: str) -> str:
    phone = get_phone(chat_id, name)
    return f"Телефон: {phone}" if phone else "Телефон не записан. /телефон Имя 050..."


def reminder_was_sent(chat_id: int, kind: str, client: str, ref: str) -> bool:
    with db() as conn:
        row = conn.execute(
            """
            SELECT id FROM reminders
            WHERE chat_id = ? AND kind = ? AND client = ? AND ref = ?
            """,
            (chat_id, kind, client, ref),
        ).fetchone()
        return row is not None


def mark_reminder(chat_id: int, kind: str, client: str, ref: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO reminders (chat_id, kind, client, ref, sent_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, kind, client, ref, datetime.now().isoformat(timespec="seconds")),
        )


def due_feel_reminders() -> list[sqlite3.Row]:
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    with db() as conn:
        return list(
            conn.execute(
                """
                SELECT p.chat_id, p.client, p.paid_on, p.id, p.created_at
                FROM payments p
                WHERE substr(p.created_at, 1, 10) = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM reminders r
                    WHERE r.chat_id = p.chat_id
                      AND r.kind = 'feel'
                      AND r.ref = CAST(p.id AS TEXT)
                  )
                ORDER BY p.id
                """,
                (yesterday,),
            )
        )


def due_invite_reminders() -> list[sqlite3.Row]:
    limit = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    with db() as conn:
        return list(
            conn.execute(
                """
                SELECT chat_id, client, MAX(paid_on) AS last_visit, COUNT(*) AS visits
                FROM payments
                GROUP BY chat_id, client
                HAVING last_visit <= ?
                """,
                (limit,),
            )
        )


def parse_date(text: str) -> datetime | None:
    text = text.replace("-", ".").replace("/", ".")
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d.%m"):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%d.%m":
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    return None


def parse_message(text: str) -> tuple[str, str, float] | None:
    text = text.strip()
    paid_on = datetime.now()
    rest = text
    m_date = DATE_RE.match(text)
    if m_date:
        parsed = parse_date(m_date.group(1))
        if parsed:
            paid_on = parsed
            rest = m_date.group(2).strip()
    m_amt = AMOUNT_RE.match(rest)
    if not m_amt:
        return None
    name = " ".join(m_amt.group("name").split())
    amount = float(m_amt.group("amount").replace(",", "."))
    pay_raw = (m_amt.group("pay") or "нал").lower()
    pay = PAY_MAP.get(pay_raw, "מזומן")
    if not name:
        return None
    return paid_on.strftime("%Y-%m-%d"), name, amount, pay


def add_payment(chat_id: int, user_id: int, paid_on: str, client: str, amount: float) -> int:
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO payments (chat_id, user_id, paid_on, client, amount, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, user_id, paid_on, client, amount, datetime.now().isoformat(timespec="seconds")),
        )
        return int(cur.lastrowid)


def day_report(chat_id: int, day: str) -> list[sqlite3.Row]:
    with db() as conn:
        return list(
            conn.execute(
                """
                SELECT paid_on, client, amount, id
                FROM payments
                WHERE chat_id = ? AND paid_on = ?
                ORDER BY id
                """,
                (chat_id, day),
            )
        )


def month_report(chat_id: int, year: int, month: int) -> list[sqlite3.Row]:
    prefix = f"{year:04d}-{month:02d}"
    with db() as conn:
        return list(
            conn.execute(
                """
                SELECT paid_on, client, amount, id
                FROM payments
                WHERE chat_id = ? AND paid_on LIKE ?
                ORDER BY paid_on, id
                """,
                (chat_id, f"{prefix}%"),
            )
        )


def client_report(chat_id: int, name: str) -> list[sqlite3.Row]:
    with db() as conn:
        return list(
            conn.execute(
                """
                SELECT paid_on, client, amount, id
                FROM payments
                WHERE chat_id = ? AND lower(client) = lower(?)
                ORDER BY paid_on, id
                """,
                (chat_id, name),
            )
        )


def delete_by_id(chat_id: int, internal_id: int) -> sqlite3.Row | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM payments WHERE id = ? AND chat_id = ?",
            (internal_id, chat_id),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM payments WHERE id = ? AND chat_id = ?", (internal_id, chat_id))
        return row


def delete_last(chat_id: int) -> sqlite3.Row | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM payments WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM payments WHERE id = ? AND chat_id = ?", (row["id"], chat_id))
        return row


def format_rows(rows: list[sqlite3.Row], title: str) -> str:
    if not rows:
        return f"{title}\nПока пусто."
    lines = [title, ""]
    total = 0.0
    by_client: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        total += row["amount"]
        by_client.setdefault(row["client"], []).append(row)
        d = datetime.strptime(row["paid_on"], "%Y-%m-%d").strftime("%d.%m.%Y")
        lines.append(f"{d}  {row['client']}  {row['amount']:.2f} ₪  №{public_no(row['id'])}")
    lines += ["", f"Всего: {total:.2f} ₪", f"Записей: {len(rows)}", "", "По клиентам:"]
    for client, items in sorted(by_client.items(), key=lambda x: x[0].lower()):
        s = sum(i["amount"] for i in items)
        lines.append(f"• {client}: {len(items)} раз, {s:.2f} ₪")
    return "\n".join(lines)


def write_excel(rows: list[sqlite3.Row], title: str) -> Path:
    EXPORTS = BASE / "exports"
    EXPORTS.mkdir(exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in title)
    path = EXPORTS / f"{safe}.csv"
    total = 0.0
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Дата", "Клиент", "Сумма", "Номер чека"])
        for row in rows:
            total += row["amount"]
            d = datetime.strptime(row["paid_on"], "%Y-%m-%d").strftime("%d.%m.%Y")
            w.writerow([d, row["client"], f"{row['amount']:.2f}", public_no(row["id"])])
        w.writerow([])
        w.writerow(["Всего", "", f"{total:.2f}", ""])
    return path


def make_receipt_pdf(
    receipt_no: int, paid_on: str, client: str, amount: float, pay: str
) -> Path:
    pdf = FPDF(format="A4")
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    if FONT.exists():
        pdf.add_font("He", "", str(FONT))
        pdf.add_font("He", "B", str(FONT_B if FONT_B.exists() else FONT))
        face = "He"
    else:
        face = "Helvetica"
    ru = face
    if FONT_RU.exists():
        pdf.add_font("Ru", "", str(FONT_RU))
        ru = "Ru"
    date_h = datetime.strptime(paid_on, "%Y-%m-%d").strftime("%d/%m/%Y")
    pdf.set_font(ru if ru == "Ru" else face, "B", 14)
    pdf.cell(0, 8, heb(BUSINESS_NAME), ln=True, align="C")
    pdf.set_font(face, "", 10)
    if BUSINESS_ID:
        pdf.cell(0, 6, heb("עוסק פטור") + f"  {BUSINESS_ID}", ln=True, align="C")
    if BUSINESS_ADDRESS:
        pdf.cell(0, 6, heb(BUSINESS_ADDRESS), ln=True, align="C")
    if BUSINESS_PHONE:
        pdf.cell(0, 6, BUSINESS_PHONE, ln=True, align="C")
    pdf.ln(2)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(4)
    pdf.set_font(face, "", 9)
    pdf.cell(95, 5, date_h + "  " + heb("תאריך"), align="L")
    pdf.set_font(ru, "", 9)
    pdf.cell(95, 5, heb(client), ln=True, align="R")
    pdf.set_font(face, "", 9)
    pdf.cell(0, 5, heb("[העתק נאמן למקור]"), ln=True, align="L")
    pdf.ln(8)
    pdf.set_font(face, "B", 18)
    pdf.cell(0, 10, heb("קבלה מספר") + f"  {receipt_no}", ln=True, align="C")
    pdf.set_font(face, "", 11)
    pdf.cell(0, 7, heb("קבלה עוסק פטור"), ln=True, align="C")
    pdf.ln(4)
    pdf.set_fill_color(180, 210, 230)
    pdf.set_font(face, "B", 10)
    col_w = [36, 40, 78, 26]
    headers = [heb("תאריך"), heb("אמצעי תשלום"), heb("פרטים"), heb("סכום")]
    for w, h in zip(col_w, headers):
        pdf.cell(w, 8, h, border=1, align="C", fill=True)
    pdf.ln()
    pdf.set_font(face, "", 10)
    vals = [date_h, heb(pay), "Услуга", f"{amount:.2f}"]
    for w, v in zip(col_w, vals):
        pdf.cell(w, 8, str(v), border=1, align="C")
    pdf.ln()
    pdf.set_font(ru if ru == "Ru" else face, "B", 16)
    pdf.cell(0, 12, f"Итого:  {amount:.2f}  ₪", ln=True, align="R")
    pdf.set_font(face, "", 11)
    pdf.cell(0, 8, 'סה"כ', ln=True, align="R")
    pdf.ln(12)
    pdf.set_font(ru if ru == "Ru" else face, "", 8)
    pdf.cell(0, 5, "Осек патур — без НДС", ln=True, align="C")
    path = RECEIPTS / f"kabala_{receipt_no}.pdf"
    pdf.output(path)
    return path


HELP = """Касса группы

Оплата и чек:
Иван 2500
Иван 2500 нал
15.09 Мария 1800 карта

Команды:
/отчет — этот месяц
/отчет 09.2026 — другой месяц
/клиент Иван
/excel — таблица за месяц
/удалить — последняя запись
/удалить 30001 — по номеру чека
/телефон Иван 0501234567
/текст Иван — тексты в WhatsApp
/запись Иван 18.09 12:00
/запись Иван 18.09 12:00 0501234567
/записи
/снять 12 — удалить визит по номеру
/итог — касса за сегодня
/тест
/напомин
/помощь

После записи бот спросит телефон.
Кнопки «Русский» и «Иврит» присылают текст, его копируете в WhatsApp.

Напоминания (компьютер включён):
на следующий день после чека — самочувствие;
если клиента не было 14 дней — приглашение;
в 18:00 за день до визита — запись;
в 21:00 — итог дня.
"""


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "Нет токена. В @BotFather возьмите ключ.\n"
            "В командной строке: set BOT_TOKEN=ключ\n"
            "Потом: py bot.py"
        )
    bot = telebot.TeleBot(TOKEN, parse_mode=None)
    db()

    def chat_id(message: telebot.types.Message) -> int:
        return message.chat.id

    @bot.message_handler(commands=["start", "help", "помощь"])
    def start(message: telebot.types.Message) -> None:
        bot.reply_to(message, HELP)

    @bot.message_handler(commands=["отчет", "отчёт", "report"])
    def report(message: telebot.types.Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        now = datetime.now()
        if len(parts) == 2:
            raw = parts[1].strip().replace("/", ".")
            day = parse_date(raw)
            bits = raw.split(".")
            if day and len(bits) >= 2 and len(bits[0]) <= 2 and int(bits[0]) > 12 or (
                day and len(bits) == 3
            ):
                rows = day_report(chat_id(message), day.strftime("%Y-%m-%d"))
                bot.reply_to(
                    message,
                    format_rows(rows, f"Отчёт за {day.strftime('%d.%m.%Y')}"),
                )
                return
            try:
                if len(bits) == 2 and int(bits[0]) <= 12:
                    month, year = int(bits[0]), int(bits[1])
                    if year < 100:
                        year += 2000
                elif raw.isdigit():
                    month, year = int(raw), now.year
                else:
                    raise ValueError
            except ValueError:
                bot.reply_to(message, "Формат: /отчет   /отчет 15.09.2026   /отчет 09.2026")
                return
        else:
            year, month = now.year, now.month
        rows = month_report(chat_id(message), year, month)
        bot.reply_to(message, format_rows(rows, f"Отчёт за {month:02d}.{year}"))

    @bot.message_handler(commands=["excel", "таблица"])
    def excel(message: telebot.types.Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        now = datetime.now()
        year, month = now.year, now.month
        if len(parts) == 2:
            raw = parts[1].strip().replace("/", ".")
            try:
                m, y = raw.split(".", 1)
                month, year = int(m), int(y)
                if year < 100:
                    year += 2000
            except ValueError:
                bot.reply_to(message, "Формат: /excel  или  /excel 09.2026")
                return
        rows = month_report(chat_id(message), year, month)
        path = write_excel(rows, f"kassa_{month:02d}_{year}")
        with path.open("rb") as f:
            bot.send_document(
                message.chat.id,
                f,
                visible_file_name=f"kassa_{month:02d}_{year}.csv",
                caption="Откройте в Excel. Дата, клиент, сумма, номер чека.",
                reply_to_message_id=message.message_id,
            )

    @bot.message_handler(commands=["клиент"])
    def by_client(message: telebot.types.Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "Напишите: /клиент Иван")
            return
        name = parts[1].strip()
        rows = client_report(chat_id(message), name)
        bot.reply_to(message, format_rows(rows, f"Клиент: {name}"))

    @bot.message_handler(commands=["тест", "test"])
    def test_reminders(message: telebot.types.Message) -> None:
        bot.reply_to(message, "Проверка прошла. Бот в этом чате отвечает.")
        bot.send_message(
            message.chat.id,
            "Напоминание (тест)\n"
            "Вчера выслали чек клиенту: Тест\n"
            "Напишите в WhatsApp: как самочувствие после процедуры.",
        )
        bot.send_message(
            message.chat.id,
            "Напоминание (тест)\n"
            "Клиент Тест не был уже 2 недели.\n"
            "Напишите и пригласите на массаж.",
        )

    @bot.message_handler(commands=["напомин"])
    def run_reminders_now(message: telebot.types.Message) -> None:
        sent = 0
        for row in due_feel_reminders():
            if row["chat_id"] != message.chat.id:
                continue
            bot.send_message(
                row["chat_id"],
                f"Напоминание\nВчера выслали чек клиенту: {row['client']}\n"
                f"Напишите в WhatsApp: как самочувствие после процедуры.",
            )
            mark_reminder(row["chat_id"], "feel", row["client"], str(row["id"]))
            sent += 1
        for row in due_invite_reminders():
            if row["chat_id"] != message.chat.id:
                continue
            ref = row["last_visit"]
            if reminder_was_sent(row["chat_id"], "invite", row["client"], ref):
                continue
            last = datetime.strptime(row["last_visit"], "%Y-%m-%d").strftime("%d.%m.%Y")
            bot.send_message(
                row["chat_id"],
                f"Напоминание\nКлиент {row['client']} не был уже 2 недели.\n"
                f"Последний визит: {last}\nНапишите и пригласите на массаж.",
            )
            mark_reminder(row["chat_id"], "invite", row["client"], ref)
            sent += 1
        if sent == 0:
            bot.reply_to(message, "Сейчас живых напоминаний нет. Для проверки напишите /тест")
        else:
            bot.reply_to(message, f"Отправил живых напоминаний: {sent}")

    @bot.message_handler(commands=["телефон"])
    def cmd_phone(message: telebot.types.Message) -> None:
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3:
            bot.reply_to(message, "Формат: /телефон Иван 0501234567")
            return
        name, phone = parts[1].strip(), parts[2].strip()
        set_phone(chat_id(message), name, phone)
        bot.reply_to(message, f"Телефон {name}: {phone}")

    @bot.message_handler(commands=["текст"])
    def cmd_text(message: telebot.types.Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "Формат: /текст Иван")
            return
        name = parts[1].strip()
        LAST_MSG[message.chat.id] = {"name": name, "when": None}
        bot.send_message(
            message.chat.id,
            phone_line(chat_id(message), name) + "\nНажмите язык — пришлю текст для копирования.",
            reply_markup=lang_buttons("feel"),
        )
        bot.send_message(
            message.chat.id,
            "Приглашение на массаж:",
            reply_markup=lang_buttons("invite"),
        )

    @bot.message_handler(commands=["запись"])
    def cmd_appt(message: telebot.types.Message) -> None:
        text = re.sub(r"^/запись(@\S+)?\s+", "", message.text or "", flags=re.I).strip()
        phone_m = re.search(r"(0\d{8,10}|972\d{8,10}|\+972\d{8,10})\s*$", text)
        phone = phone_m.group(1) if phone_m else ""
        if phone_m:
            text = text[: phone_m.start()].strip()
        time_m = re.search(r"(\d{1,2})[:.\-](\d{2})\s*$", text)
        if not time_m:
            bot.reply_to(message, "Напишите время в конце: /запись Иван 18.09 12:00")
            return
        hour, minute = int(time_m.group(1)), int(time_m.group(2))
        if hour > 23 or minute > 59:
            bot.reply_to(message, "Время от 00:00 до 23:59")
            return
        rest = text[: time_m.start()].strip()
        date_m = re.search(r"(\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)\s*$", rest)
        if not date_m:
            bot.reply_to(message, "Напишите дату: /запись Иван 18.09 12:00")
            return
        day = parse_date(date_m.group(1))
        name = rest[: date_m.start()].strip()
        if not day or not name:
            bot.reply_to(message, "Формат: /запись Иван 18.09 12:00")
            return
        when = day.replace(hour=hour, minute=minute)
        add_appointment(chat_id(message), name, when)
        if phone:
            set_phone(chat_id(message), name, phone)
        LAST_MSG[message.chat.id] = {"name": name, "when": when.isoformat()}
        saved = phone or get_phone(chat_id(message), name)
        extra = f"\nТелефон: {saved}" if saved else "\nТелефон пока не записан."
        bot.reply_to(
            message,
            f"Запись: {name}  {when.strftime('%d.%m.%Y %H:%M')}{extra}\n"
            "За день в 18:00 пришлю напоминание и кнопки русского/иврита.",
        )
        if not phone and not get_phone(chat_id(message), name):
            PENDING_PHONE[message.chat.id] = name
            bot.send_message(
                message.chat.id,
                f"Напишите номер телефона для {name}, например 0501234567",
            )

    @bot.message_handler(commands=["записи"])
    def cmd_appts(message: telebot.types.Message) -> None:
        try:
            rows = list_appointments(chat_id(message), upcoming_only=False)
            if not rows:
                bot.reply_to(message, "Записей на визит пока нет.")
                return
            now = datetime.now()
            lines = ["Визиты (новые сверху). Удалить: /снять 3", ""]
            for row in rows:
                when = datetime.strptime(str(row["when_at"])[:16], "%Y-%m-%d %H:%M")
                tag = "будет" if when >= now else "прошла"
                lines.append(
                    f"№{row['id']}  {when.strftime('%d.%m.%Y %H:%M')}  {row['client']}  ({tag})"
                )
            bot.reply_to(message, "\n".join(lines))
        except Exception as exc:
            print("записи error:", exc)
            bot.reply_to(message, f"Не смог показать список: {exc}")

    @bot.message_handler(commands=["снять"])
    def cmd_cancel_appt(message: telebot.types.Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        cid = chat_id(message)
        if len(parts) < 2:
            rows = list_appointments(cid, upcoming_only=False)
            if not rows:
                bot.reply_to(message, "Записей нет.")
                return
            row = rows[0]
        elif parts[1].strip().lstrip("№#").isdigit():
            row = get_appointment(cid, int(parts[1].strip().lstrip("№#")))
            if not row:
                bot.reply_to(message, "Такой записи нет. Сначала /записи")
                return
        else:
            bot.reply_to(message, "Пример: /снять    или   /снять 3")
            return
        when = datetime.strptime(str(row["when_at"])[:16], "%Y-%m-%d %H:%M")
        LAST_MSG[cid] = {"del_appt": int(row["id"]), "name": row["client"], "when": None}
        bot.reply_to(
            message,
            f"Удалить эту запись?\n№{row['id']}  {row['client']}  {when.strftime('%d.%m.%Y %H:%M')}\n"
            "Напишите: да",
        )

    @bot.message_handler(commands=["итог"])
    def cmd_today(message: telebot.types.Message) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        rows = day_report(chat_id(message), today)
        bot.reply_to(message, format_rows(rows, f"Итог за {datetime.now().strftime('%d.%m.%Y')}"))

    @bot.message_handler(commands=["удалить"])
    def undo(message: telebot.types.Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            row = delete_last(chat_id(message))
        else:
            arg = parts[1].strip().lstrip("№#")
            if arg.isdigit():
                public = int(arg)
                internal = public - RECEIPT_SERIES
                if internal <= 0:
                    bot.reply_to(message, "Такого номера чека нет.")
                    return
                row = delete_by_id(chat_id(message), internal)
            else:
                bot.reply_to(
                    message,
                    "Удалить одну запись:\n"
                    "/удалить — последнюю\n"
                    "/удалить 30001 — по номеру чека\n"
                    "Список: /отчет",
                )
                return
        if not row:
            bot.reply_to(message, "Такой записи нет.")
            return
        d = datetime.strptime(row["paid_on"], "%Y-%m-%d").strftime("%d.%m.%Y")
        bot.reply_to(
            message,
            f"Удалил №{public_no(row['id'])}: {d}  {row['client']}  {row['amount']:.2f} ₪",
        )

    @bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("q|"))
    def on_ask_del(call: CallbackQuery) -> None:
        if not call.message:
            return
        try:
            appt_id = int((call.data or "").split("|")[1])
        except (IndexError, ValueError):
            bot.answer_callback_query(call.id, "Ошибка")
            return
        row = get_appointment(call.message.chat.id, appt_id)
        if not row:
            bot.answer_callback_query(call.id, "Записи уже нет")
            return
        when = datetime.strptime(row["when_at"], "%Y-%m-%d %H:%M")
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("Да, удалить", callback_data=f"x|{appt_id}"),
            InlineKeyboardButton("Отмена", callback_data=f"n|{appt_id}"),
        )
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"Точно удалить запись?\n{row['client']}  {when.strftime('%d.%m.%Y %H:%M')}",
            reply_markup=kb,
        )

    @bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("n|"))
    def on_cancel_del(call: CallbackQuery) -> None:
        bot.answer_callback_query(call.id, "Не удалил")
        if call.message:
            try:
                bot.edit_message_text(
                    "Удаление отменено.",
                    call.message.chat.id,
                    call.message.message_id,
                )
            except Exception:
                bot.send_message(call.message.chat.id, "Удаление отменено.")

    @bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("x|"))
    def on_del_appt(call: CallbackQuery) -> None:
        if not call.message:
            return
        try:
            appt_id = int((call.data or "").split("|")[1])
        except (IndexError, ValueError):
            bot.answer_callback_query(call.id, "Не получилось удалить")
            return
        row = delete_appointment(call.message.chat.id, appt_id)
        if not row:
            bot.answer_callback_query(call.id, "Уже удалено")
            return
        when = datetime.strptime(row["when_at"], "%Y-%m-%d %H:%M")
        bot.answer_callback_query(call.id, "Удалено")
        bot.send_message(
            call.message.chat.id,
            f"Удалил запись: {row['client']}  {when.strftime('%d.%m.%Y %H:%M')}",
        )
        left = list_appointments(call.message.chat.id)
        if not left:
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass

    @bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("c|"))
    def on_copy(call: CallbackQuery) -> None:
        parts = (call.data or "").split("|")
        if len(parts) != 3:
            return
        _, kind, lang = parts
        ctx = LAST_MSG.get(call.message.chat.id, {}) if call.message else {}
        name = ctx.get("name") or "клиент"
        when = None
        if ctx.get("when"):
            try:
                when = datetime.fromisoformat(ctx["when"])
            except ValueError:
                when = None
        text = pick_text(kind, name, when, lang)
        bot.send_message(call.message.chat.id, text)
        bot.answer_callback_query(call.id, "Текст ниже. Зажмите и копируйте.")

    @bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith("/"))
    def add(message: telebot.types.Message) -> None:
        raw = (message.text or "").strip()
        wait_del = LAST_MSG.get(message.chat.id, {}).get("del_appt")
        if wait_del and raw.lower() in {"да", "yes", "удалить"}:
            row = delete_appointment(message.chat.id, int(wait_del))
            LAST_MSG.get(message.chat.id, {}).pop("del_appt", None)
            if not row:
                bot.reply_to(message, "Уже удалено.")
                return
            when = datetime.strptime(row["when_at"], "%Y-%m-%d %H:%M")
            bot.reply_to(message, f"Удалил: {row['client']}  {when.strftime('%d.%m.%Y %H:%M')}")
            return
        if wait_del and raw.lower() in {"нет", "отмена"}:
            LAST_MSG.get(message.chat.id, {}).pop("del_appt", None)
            bot.reply_to(message, "Оставил запись.")
            return
        pending = PENDING_PHONE.get(message.chat.id)
        if pending:
            phone = re.sub(r"[\s\-]", "", raw)
            if re.fullmatch(r"(\+?972|0)\d{8,10}", phone):
                set_phone(message.chat.id, pending, phone)
                PENDING_PHONE.pop(message.chat.id, None)
                bot.reply_to(message, f"Телефон {pending}: {phone}")
                return
            if raw.lower() in {"нет", "пропуск", "skip"}:
                PENDING_PHONE.pop(message.chat.id, None)
                bot.reply_to(message, "Ок, без телефона.")
                return
            bot.reply_to(message, f"Это не похоже на номер. Для {pending} напишите 0501234567 или «нет».")
            return
        raw_l = raw.lower()
        if raw_l in {"тест", "test"}:
            bot.reply_to(message, "Проверка прошла. Бот в этом чате отвечает.")
            bot.send_message(
                message.chat.id,
                "Напоминание (тест)\nВчера выслали чек клиенту: Тест\n"
                "Напишите в WhatsApp: как самочувствие после процедуры.",
            )
            return
        parsed = parse_message(message.text or "")
        if not parsed:
            if message.chat.type == "private":
                bot.reply_to(message, "Пишите: Иван 2500  или  15.09 Иван 2500")
            return
        paid_on, client, amount, pay = parsed
        uid = message.from_user.id if message.from_user else 0
        rid = add_payment(chat_id(message), uid, paid_on, client, amount)
        num = public_no(rid)
        d = datetime.strptime(paid_on, "%Y-%m-%d").strftime("%d.%m.%Y")
        path = make_receipt_pdf(num, paid_on, client, amount, pay)
        with path.open("rb") as f:
            bot.send_document(
                message.chat.id,
                f,
                visible_file_name=f"kabala_{num}.pdf",
                caption=f"Записал №{num}: {d}  {client}  {amount:.2f} ₪\nПерешлите PDF клиенту в WhatsApp.",
                reply_to_message_id=message.message_id,
            )

    def reminder_loop() -> None:
        while True:
            try:
                for row in due_feel_reminders():
                    LAST_MSG[row["chat_id"]] = {"name": row["client"], "when": None}
                    text = (
                        f"Напоминание\n"
                        f"Вчера чек клиенту: {row['client']}\n"
                        f"{phone_line(row['chat_id'], row['client'])}\n"
                        "Нажмите язык — текст для WhatsApp."
                    )
                    bot.send_message(row["chat_id"], text, reply_markup=lang_buttons("feel"))
                    mark_reminder(row["chat_id"], "feel", row["client"], str(row["id"]))
                for row in due_invite_reminders():
                    ref = row["last_visit"]
                    if reminder_was_sent(row["chat_id"], "invite", row["client"], ref):
                        continue
                    last = datetime.strptime(row["last_visit"], "%Y-%m-%d").strftime("%d.%m.%Y")
                    LAST_MSG[row["chat_id"]] = {"name": row["client"], "when": None}
                    text = (
                        f"Напоминание\n"
                        f"Клиент {row['client']} не был уже 2 недели.\n"
                        f"Последний визит: {last}\n"
                        f"{phone_line(row['chat_id'], row['client'])}\n"
                        "Нажмите язык — текст для WhatsApp."
                    )
                    bot.send_message(row["chat_id"], text, reply_markup=lang_buttons("invite"))
                    mark_reminder(row["chat_id"], "invite", row["client"], ref)
                now = datetime.now()
                if now.hour == 18:
                    for row in due_appointment_reminders():
                        ref = f"appt-{row['id']}"
                        if reminder_was_sent(row["chat_id"], "appt", row["client"], ref):
                            continue
                        when = datetime.strptime(row["when_at"], "%Y-%m-%d %H:%M")
                        LAST_MSG[row["chat_id"]] = {
                            "name": row["client"],
                            "when": when.isoformat(),
                        }
                        bot.send_message(
                            row["chat_id"],
                            "Напоминание о записи завтра\n"
                            f"{row['client']}  {when.strftime('%d.%m.%Y %H:%M')}\n"
                            f"{phone_line(row['chat_id'], row['client'])}\n"
                            "Нажмите язык — пришлю текст для WhatsApp.",
                            reply_markup=lang_buttons("visit"),
                        )
                        mark_reminder(row["chat_id"], "appt", row["client"], ref)
                if now.hour == 21:
                    today = now.strftime("%Y-%m-%d")
                    for cid in known_chat_ids():
                        if reminder_was_sent(cid, "daily", "-", today):
                            continue
                        rows = day_report(cid, today)
                        bot.send_message(
                            cid,
                            format_rows(rows, f"Итог дня {now.strftime('%d.%m.%Y')}"),
                        )
                        mark_reminder(cid, "daily", "-", today)
            except Exception as exc:
                print("reminder error:", exc)
            time.sleep(1800)

    threading.Thread(target=reminder_loop, daemon=True).start()
    print("Бот кассы запущен")
    bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    main()
