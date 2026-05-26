# -*- coding: utf-8 -*-
"""
Telegram-бот: остатки 1С:УНФ, клиенты, долги.
Адаптирован под реальные поля 1С УНФ.
Python 3.11 + python-telegram-bot 20.1
"""

import os
import json
import logging
import datetime
import asyncio

import requests
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ── 1. НАСТРОЙКИ (читаются из файла .env) ──────────────────
load_dotenv()

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
ONEC_BASE_URL     = os.getenv("ONEC_BASE_URL")
ONEC_LOGIN        = os.getenv("ONEC_LOGIN")
ONEC_PASSWORD     = os.getenv("ONEC_PASSWORD")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_CREDS_FILE = os.getenv("GOOGLE_CREDS_FILE", "google_creds.json")

REQUEST_TIMEOUT = 15

# ── 2. ЛОГИРОВАНИЕ ────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bot")

# ── 3. РАБОТА С 1С ────────────────────────────────────────
def onec_get(endpoint: str):
    """GET-запрос к 1С. Возвращает (данные, текст_ошибки)."""
    url = ONEC_BASE_URL.rstrip("/") + endpoint
    try:
        resp = requests.get(
            url,
            auth=(ONEC_LOGIN, ONEC_PASSWORD),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        log.error("1С не ответила вовремя: %s", url)
        return None, "⏱ 1С не отвечает (превышено время ожидания)."
    except requests.exceptions.ConnectionError:
        log.error("Нет связи с 1С: %s", url)
        return None, "🔌 Не удалось подключиться к 1С. Проверьте адрес и сеть."

    if resp.status_code == 401:
        log.error("1С: неверный логин или пароль")
        return None, "🔑 1С отклонила вход (неверный логин или пароль)."
    if resp.status_code == 404:
        log.error("1С: адрес не найден %s", url)
        return None, "❓ Такой адрес в 1С не найден (404)."
    if resp.status_code != 200:
        log.error("1С вернула код %s", resp.status_code)
        return None, f"⚠️ 1С вернула ошибку (код {resp.status_code})."

    try:
        return resp.json(), None
    except json.JSONDecodeError:
        log.error("1С вернула не JSON")
        return None, "⚠️ 1С вернула непонятный ответ."


# ── 4. РАБОТА С GOOGLE SHEETS ────────────────────────────
def get_credit_days(inn: str):
    """Лимит дней отсрочки по ИНН из Google Sheets."""
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open(GOOGLE_SHEET_NAME).sheet1
        rows = sheet.get_all_records()
        for row in rows:
            if str(row.get("INN", "")).strip() == str(inn).strip():
                return int(row.get("Credit_Days", 0)), None
        return None, "Клиент не найден в Google-таблице."
    except FileNotFoundError:
        log.error("Файл ключа Google не найден")
        return None, "Файл ключа Google не найден."
    except Exception as e:  # noqa: BLE001
        log.error("Ошибка Google Sheets: %s", e)
        return None, "Не удалось прочитать Google-таблицу."


# ── 5. ВСПОМОГАТЕЛЬНОЕ ──────────────────────────────────
def log_command(update: Update, name: str):
    user = update.effective_user
    log.info("Команда %s от @%s (id=%s)", name, user.username, user.id)


# ── 6. КОМАНДА /start ───────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update, "/start")
    text = (
        "👋 Привет! Я бот склада и клиентов.\n\n"
        "Вот что я умею:\n"
        "📦 /stock <название> — остатки товара\n"
        "👤 /customer <ИНН> — данные клиента\n"
        "💰 /debt <ИНН> — задолженность клиента\n"
        "📊 /report — отчёт по просрочкам\n"
    )
    await update.message.reply_text(text)


# ── НАЗВАНИЕ НУЖНОГО СКЛАДА ──────────────────────────────
# Бот показывает остатки только по этому складу.
TARGET_WAREHOUSE = "Склад ГП. Основной склад (Химки. Обособленное подразделение)"


# ── ПОИСК ПО СЛОВАМ (вспомогательное) ───────────────────
def words_match(query: str, target: str) -> bool:
    """
    Возвращает True, если КАЖДОЕ слово из query встречается в target.
    Слова можно писать в любом порядке. Терпит опечатку в одной букве.
    Пример: 'лосось 5-6 sup охл' найдёт 'Лосось атл (семга) ПСГ 5-6 SUP охл'.
    """
    import difflib

    q_words = query.lower().split()
    t_words = target.lower().split()

    if not q_words:
        return False

    for qw in q_words:
        # слово подходит, если оно входит в какое-то слово названия...
        ok = any(qw in tw for tw in t_words)
        # ...или очень похоже на него (опечатка в букве)
        if not ok:
            ok = any(
                difflib.SequenceMatcher(None, qw, tw).ratio() >= 0.8
                for tw in t_words
            )
        if not ok:
            return False  # хотя бы одно слово не найдено — товар не подходит

    return True


# ── 7. КОМАНДА /stock ───────────────────────────────────
async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update, "/stock")
    if not context.args:
        await update.message.reply_text("Напишите так: /stock лосось 5-6 sup")
        return

    query = " ".join(context.args).strip()
    data, error = onec_get("/Stocks")
    if error:
        await update.message.reply_text(error)
        return

    # 1С отдаёт объект: { "Название склада": [список товаров], ... }
    if not isinstance(data, dict):
        await update.message.reply_text("⚠️ Неожиданный формат данных от 1С.")
        return

    # Берём товары ТОЛЬКО нужного склада
    items = data.get(TARGET_WAREHOUSE)
    if not isinstance(items, list):
        await update.message.reply_text(
            "⚠️ Нужный склад не найден в данных 1С.\n"
            f"Ожидался: {TARGET_WAREHOUSE}"
        )
        return

    # Ищем совпадения: название -> количество (+ единица)
    found = {}
    units = {}
    for item in items:
        name = str(item.get("Наименование", ""))
        if not name:
            continue
        if words_match(query, name):
            found[name] = item.get("Количество", 0)
            units[name] = item.get("ЕдиницаИзмерения", "шт")

    if not found:
        await update.message.reply_text(
            f"📦 Ничего не найдено для '{query}'.\n"
            "Попробуйте написать часть названия, например: лосось 5-6 sup"
        )
        return

    # Если совпадение РОВНО одно — короткий ответ
    if len(found) == 1:
        name, qty = next(iter(found.items()))
        unit = units[name]
        await update.message.reply_text(
            f"📦 {name}\nОстаток: {qty} {unit}"
        )
        return

    # Несколько совпадений — список
    lines = [f"📦 Найдено товаров: {len(found)}", ""]
    for name, qty in found.items():
        unit = units[name]
        lines.append(f"• {name}")
        lines.append(f"   Остаток: {qty} {unit}")
        lines.append("")
    lines.append("Уточните запрос, если нужен один конкретный товар.")

    answer = "\n".join(lines)
    if len(answer) > 4000:
        answer = answer[:4000] + "\n\n… список обрезан, уточните запрос."
    await update.message.reply_text(answer)


# ── 8. КОМАНДА /customer ────────────────────────────────
async def cmd_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update, "/customer")
    if not context.args:
        await update.message.reply_text("Напишите так: /customer 4632570209 41")
        return

    inn = context.args[0].strip()
    data, error = onec_get(f"/Customers/?INN={inn}")
    if error:
        await update.message.reply_text(error)
        return

    if not data:
        await update.message.reply_text(f"👤 Клиент с ИНН {inn} не найден.")
        return

    # data может быть одним объектом или списком
    c = data[0] if isinstance(data, list) else data

    name = c.get("Наименование", "?")
    inn_show = c.get("ИНН", "?")
    phone = c.get("Телефон", "?")
    email = c.get("Email", "?")
    address = c.get("Адрес", "?")

    text = (
        "👤 Информация о клиенте:\n\n"
        f"Компания/ФИО: {name}\n"
        f"ИНН: {inn_show}\n"
        f"Телефон: {phone}\n"
        f"Email: {email}\n"
        f"Адрес: {address}"
    )
    await update.message.reply_text(text)


# ── 9. КОМАНДА /debt ────────────────────────────────────
async def cmd_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update, "/debt")
    if not context.args:
        await update.message.reply_text("Напишите так: /debt 4632570209 41")
        return

    inn = context.args[0].strip()
    data, error = onec_get(f"/Debts/?INN={inn}")
    if error:
        await update.message.reply_text(error)
        return

    if not data:
        await update.message.reply_text(f"💰 Долгов по ИНН {inn} не найдено.")
        return

    d = data[0] if isinstance(data, list) else data

    company = d.get("Наименование", "?")
    amount = d.get("Задолженность", 0)
    last_pay = d.get("ДатаПоследнегоПлатежа", "")

    text = [
        "💰 Задолженность:", "",
        f"Компания: {company}",
        f"ИНН: {inn}",
        f"Сумма долга: {amount} руб",
    ]

    # Считаем дни с последнего платежа
    days_passed = None
    if last_pay:
        try:
            pay_date = datetime.datetime.strptime(last_pay, "%Y-%m-%d").date()
            days_passed = (datetime.date.today() - pay_date).days
            text.append(f"Дата последнего платежа: {last_pay}")
            text.append(f"Прошло дней: {days_passed}")
        except ValueError:
            text.append(f"Дата последнего платежа: {last_pay}")
    else:
        text.append("Дата последнего платежа: нет данных")

    # Лимит отсрочки из Google Sheets
    credit_days, gs_error = get_credit_days(inn)
    if gs_error:
        text.append(f"Лимит отсрочки: {gs_error}")
    else:
        text.append(f"Лимит отсрочки: {credit_days} дней")
        if days_passed is not None:
            overdue = days_passed - credit_days
            if overdue > 0:
                text.append(f"Статус: ⚠️ ПРОСРОЧКА на {overdue} дней!")
            else:
                text.append("Статус: ✅ Без просрочки")

    await update.message.reply_text("\n".join(text))


# ── 10. КОМАНДА /report ─────────────────────────────────
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update, "/report")
    await update.message.reply_text("⏳ Загружаю отчёт... (может занять минуту)")

    # Берём все ИНН из Google Sheets
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open(GOOGLE_SHEET_NAME).sheet1
        rows = sheet.get_all_records()
    except FileNotFoundError:
        await update.message.reply_text("❌ Файл ключа Google не найден.")
        return
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"❌ Ошибка Google Sheets: {e}")
        return

    if not rows:
        await update.message.reply_text("❌ Google-таблица пуста.")
        return

    overdue_list = []
    for row in rows:
        inn = str(row.get("INN", "")).strip()
        if not inn:
            continue

        # Запрашиваем долг конкретного клиента из 1С
        data, error = onec_get(f"/Debts/?INN={inn}")
        if error:
            continue

        if not data:
            continue

        d = data[0] if isinstance(data, list) else data

        last_pay = d.get("ДатаПоследнегоПлатежа", "")
        amount = d.get("Задолженность", 0)
        company = d.get("Наименование", "?")

        if not last_pay or amount == 0:
            continue

        try:
            pay_date = datetime.datetime.strptime(last_pay, "%Y-%m-%d").date()
        except ValueError:
            continue

        days_passed = (datetime.date.today() - pay_date).days
        credit_days, gs_error = get_credit_days(inn)

        if gs_error:
            continue

        overdue = days_passed - credit_days
        if overdue > 0:
            overdue_list.append(
                f"⚠️ {company}: {amount} руб, просрочка {overdue} дн."
            )

    if not overdue_list:
        await update.message.reply_text("📊 Просрочек нет — все молодцы! ✅")
        return

    await update.message.reply_text(
        "📊 Отчёт по просрочкам:\n\n" + "\n".join(overdue_list)
    )


# ── 11. ЗАПУСК БОТА ──────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        log.error("Нет TELEGRAM_TOKEN. Проверьте файл .env")
        print("ОШИБКА: не заполнен файл .env (нет TELEGRAM_TOKEN).")
        return

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("customer", cmd_customer))
    app.add_handler(CommandHandler("debt", cmd_debt))
    app.add_handler(CommandHandler("report", cmd_report))

    log.info("Бот запущен ✅")
    print("Бот запущен! Открой Telegram и напиши ему /start")

    # Создаём event loop вручную и запускаем бота через него.
    # Так бот работает на любой версии Python, включая 3.14,
    # где app.run_polling() сам по себе падает.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.run_polling()


if __name__ == "__main__":
    main()
