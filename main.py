# -*- coding: utf-8 -*-
"""
Telegram-бот: остатки 1С:УНФ, клиенты, долги.
Адаптирован под реальные поля 1С УНФ.
Управление через кнопки + команды через / остаются рабочими.
"""

import os
import json
import logging
import datetime
import asyncio
import difflib

import requests
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

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


# Клавиатура с кнопками — показывается в /start и после каждого ответа.
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Остатки товара", callback_data="ask_stock")],
        [InlineKeyboardButton("👤 Клиент",         callback_data="ask_customer")],
        [InlineKeyboardButton("💰 Задолженность",  callback_data="ask_debt")],
        [InlineKeyboardButton("📊 Отчёт по просрочкам", callback_data="run_report")],
    ])


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
    q_words = query.lower().split()
    t_words = target.lower().split()

    if not q_words:
        return False

    for qw in q_words:
        ok = any(qw in tw for tw in t_words)
        if not ok:
            ok = any(
                difflib.SequenceMatcher(None, qw, tw).ratio() >= 0.8
                for tw in t_words
            )
        if not ok:
            return False

    return True


# ── 6. КОМАНДА /start ───────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update, "/start")
    # На всякий случай сбрасываем режим ожидания ввода.
    context.user_data.pop("waiting_for", None)
    text = (
        "👋 Привет! Я бот склада и клиентов.\n\n"
        "Нажмите кнопку ниже — я попрошу ввести то, что нужно.\n"
        "Можно и командами: /stock название, /customer ИНН, /debt ИНН."
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())


# ── ЛОГИКА ПОИСКА ОСТАТКОВ (общая для команды и для кнопки) ──
async def do_stock(message, query: str):
    """Ищет товар и отправляет ответ в чат `message`."""
    query = query.strip()
    if not query:
        await message.reply_text("Пустой запрос. Напишите название товара.")
        return

    data, error = onec_get("/Stocks")
    if error:
        await message.reply_text(error, reply_markup=main_keyboard())
        return

    # 1С отдаёт объект: { "Название склада": [список товаров], ... }
    if not isinstance(data, dict):
        await message.reply_text("⚠️ Неожиданный формат данных от 1С.",
                                 reply_markup=main_keyboard())
        return

    items = data.get(TARGET_WAREHOUSE)
    if not isinstance(items, list):
        await message.reply_text(
            "⚠️ Нужный склад не найден в данных 1С.\n"
            f"Ожидался: {TARGET_WAREHOUSE}",
            reply_markup=main_keyboard())
        return

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
        await message.reply_text(
            f"📦 Ничего не найдено для '{query}'.\n"
            "Попробуйте написать часть названия, например: лосось 5-6 sup",
            reply_markup=main_keyboard())
        return

    if len(found) == 1:
        name, qty = next(iter(found.items()))
        unit = units[name]
        await message.reply_text(
            f"📦 {name}\nОстаток: {qty} {unit}",
            reply_markup=main_keyboard())
        return

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
    await message.reply_text(answer, reply_markup=main_keyboard())


# ── 7. КОМАНДА /stock ───────────────────────────────────
async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update, "/stock")
    if not context.args:
        # Команда без аргумента — переходим в режим ожидания ввода.
        context.user_data["waiting_for"] = "stock"
        await update.message.reply_text("📦 Введите название товара:")
        return
    await do_stock(update.message, " ".join(context.args))


# ── ЛОГИКА ПО КЛИЕНТУ (общая) ───────────────────────────
async def do_customer(message, inn: str):
    inn = inn.strip()
    if not inn:
        await message.reply_text("Пустой запрос. Введите ИНН.")
        return

    data, error = onec_get(f"/Customers/?INN={inn}")
    if error:
        await message.reply_text(error, reply_markup=main_keyboard())
        return

    if not data:
        await message.reply_text(f"👤 Клиент с ИНН {inn} не найден.",
                                 reply_markup=main_keyboard())
        return

    c = data[0] if isinstance(data, list) else data

    text = (
        "👤 Информация о клиенте:\n\n"
        f"Компания/ФИО: {c.get('Наименование', '?')}\n"
        f"ИНН: {c.get('ИНН', '?')}\n"
        f"Телефон: {c.get('Телефон', '?')}\n"
        f"Email: {c.get('Email', '?')}\n"
        f"Адрес: {c.get('Адрес', '?')}"
    )
    await message.reply_text(text, reply_markup=main_keyboard())


# ── 8. КОМАНДА /customer ────────────────────────────────
async def cmd_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update, "/customer")
    if not context.args:
        context.user_data["waiting_for"] = "customer"
        await update.message.reply_text("👤 Введите ИНН клиента:")
        return
    await do_customer(update.message, context.args[0])


# ── ЛОГИКА ПО ДОЛГУ (общая) ─────────────────────────────
async def do_debt(message, inn: str):
    inn = inn.strip()
    if not inn:
        await message.reply_text("Пустой запрос. Введите ИНН.")
        return

    data, error = onec_get(f"/Debts/?INN={inn}")
    if error:
        await message.reply_text(error, reply_markup=main_keyboard())
        return

    if not data:
        await message.reply_text(f"💰 Долгов по ИНН {inn} не найдено.",
                                 reply_markup=main_keyboard())
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

    await message.reply_text("\n".join(text), reply_markup=main_keyboard())


# ── 9. КОМАНДА /debt ────────────────────────────────────
async def cmd_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update, "/debt")
    if not context.args:
        context.user_data["waiting_for"] = "debt"
        await update.message.reply_text("💰 Введите ИНН клиента:")
        return
    await do_debt(update.message, context.args[0])


# ── ЛОГИКА ОТЧЁТА (общая) ───────────────────────────────
async def do_report(message):
    await message.reply_text("⏳ Загружаю отчёт... (может занять минуту)")

    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open(GOOGLE_SHEET_NAME).sheet1
        rows = sheet.get_all_records()
    except FileNotFoundError:
        await message.reply_text("❌ Файл ключа Google не найден.",
                                 reply_markup=main_keyboard())
        return
    except Exception as e:  # noqa: BLE001
        await message.reply_text(f"❌ Ошибка Google Sheets: {e}",
                                 reply_markup=main_keyboard())
        return

    if not rows:
        await message.reply_text("❌ Google-таблица пуста.",
                                 reply_markup=main_keyboard())
        return

    overdue_list = []
    for row in rows:
        inn = str(row.get("INN", "")).strip()
        if not inn:
            continue

        data, error = onec_get(f"/Debts/?INN={inn}")
        if error or not data:
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
        await message.reply_text("📊 Просрочек нет — все молодцы! ✅",
                                 reply_markup=main_keyboard())
        return

    await message.reply_text(
        "📊 Отчёт по просрочкам:\n\n" + "\n".join(overdue_list),
        reply_markup=main_keyboard())


# ── 10. КОМАНДА /report ─────────────────────────────────
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update, "/report")
    context.user_data.pop("waiting_for", None)
    await do_report(update.message)


# ── 11. НАЖАТИЕ НА КНОПКУ ───────────────────────────────
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # убираем "часики" на кнопке
    action = query.data

    if action == "ask_stock":
        context.user_data["waiting_for"] = "stock"
        await query.message.reply_text("📦 Введите название товара:")

    elif action == "ask_customer":
        context.user_data["waiting_for"] = "customer"
        await query.message.reply_text("👤 Введите ИНН клиента:")

    elif action == "ask_debt":
        context.user_data["waiting_for"] = "debt"
        await query.message.reply_text("💰 Введите ИНН клиента:")

    elif action == "run_report":
        context.user_data.pop("waiting_for", None)
        await do_report(query.message)


# ── 12. ОБРАБОТКА ОБЫЧНОГО ТЕКСТА ───────────────────────
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срабатывает, когда пользователь прислал текст без команды."""
    waiting = context.user_data.get("waiting_for")
    user_text = (update.message.text or "").strip()

    if waiting == "stock":
        context.user_data.pop("waiting_for", None)
        await do_stock(update.message, user_text)

    elif waiting == "customer":
        context.user_data.pop("waiting_for", None)
        await do_customer(update.message, user_text)

    elif waiting == "debt":
        context.user_data.pop("waiting_for", None)
        await do_debt(update.message, user_text)

    else:
        # Бот ничего не ждал — показываем кнопки.
        await update.message.reply_text(
            "Выберите, что нужно:", reply_markup=main_keyboard())


# ── 13. ЗАПУСК БОТА ──────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        log.error("Нет TELEGRAM_TOKEN. Проверьте файл .env")
        print("ОШИБКА: не заполнен файл .env (нет TELEGRAM_TOKEN).")
        return

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Команды через /
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("customer", cmd_customer))
    app.add_handler(CommandHandler("debt", cmd_debt))
    app.add_handler(CommandHandler("report", cmd_report))

    # Нажатия на кнопки
    app.add_handler(CallbackQueryHandler(on_button))

    # Любой обычный текст (не команда)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

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
