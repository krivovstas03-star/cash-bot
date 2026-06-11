import asyncio
import json
import logging
import os
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ========== НАСТРОЙКИ ==========
TOKEN = "8837823632:AAG9dEOT5HNvTwmv2njWcX_gKu_WrXe0C3I"
SPREADSHEET_NAME = "Касса_Учёт"
SHEET_TRANSACTIONS = "Транзакции"
SHEET_BALANCES = "Остатки"
SHEET_ARTICLES = "Статьи"
SHEET_USERS = "Пользователи"          # Новый лист для ID пользователей
PORT = int(os.getenv("PORT", 8080))

# ========== GOOGLE SHEETS ==========
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
if GOOGLE_CREDS_JSON:
    with open("credentials.json", "w", encoding="utf-8") as f:
        f.write(GOOGLE_CREDS_JSON)

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
gclient = gspread.authorize(creds)

sheet_trans = gclient.open(SPREADSHEET_NAME).worksheet(SHEET_TRANSACTIONS)
sheet_bal = gclient.open(SPREADSHEET_NAME).worksheet(SHEET_BALANCES)
sheet_art = gclient.open(SPREADSHEET_NAME).worksheet(SHEET_ARTICLES)
sheet_users = gclient.open(SPREADSHEET_NAME).worksheet(SHEET_USERS)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
RESPONSIBLE = json.loads(os.getenv("RESPONSIBLE_JSON", "{}"))

CASHIERS = ["АЛЕКСЕЙ", "ЕВГЕНИЙ"]
SOURCES = ["ИП Герасимов", "ИП Уварова", "ИП Смирнов", "ООО Техвижения"]

# ========== ЗАГРУЗКА ДАННЫХ ИЗ ТАБЛИЦ ==========
def load_initial_balances():
    records = sheet_bal.get_all_records()
    return {r["Касса"]: float(r["Начальный остаток"]) for r in records if r["Касса"]}

def load_expense_articles():
    articles = sheet_art.col_values(1)
    result = []
    for a in articles:
        a = a.strip()
        if a and a.lower() != "статья":
            result.append(a)
    return result if result else ["Прочее"]

def load_users():
    """Загружает ID пользователей из листа Пользователи"""
    ids = sheet_users.col_values(1)
    result = set()
    for uid in ids:
        uid = uid.strip()
        if uid and uid.lower() != "user_id":
            try:
                result.add(int(uid))
            except:
                pass
    return result

def save_user_to_sheet(user_id):
    """Сохраняет ID пользователя, если его ещё нет в листе"""
    existing = sheet_users.col_values(1)
    uid_str = str(user_id)
    if uid_str not in existing:
        sheet_users.append_row([user_id], value_input_option="USER_ENTERED")

initial_balances = load_initial_balances()
expense_articles = load_expense_articles()
known_users = load_users()

# ========== КЛАВИАТУРЫ ==========
def make_keyboard(options, prefix, add_back=False, add_custom=False):
    buttons = []
    for i, opt in enumerate(options):
        buttons.append([InlineKeyboardButton(text=opt, callback_data=f"{prefix}:{i}")])
    if add_back:
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    if add_custom:
        buttons.append([InlineKeyboardButton("✏️ Новая статья", callback_data="custom_article")])
    return InlineKeyboardMarkup(buttons)

def authorized(user_id, cashier):
    return user_id == ADMIN_ID or RESPONSIBLE.get(cashier) == user_id

def remember_user(user_id):
    global known_users
    if user_id not in known_users:
        known_users.add(user_id)
        save_user_to_sheet(user_id)

# ========== КОМАНДА /help (красивая) ==========
async def help_cmd(update, context):
    text = (
        "📋 <b>Команды:</b>\n"
        "/start — начать операцию\n"
        "/balance — остатки по кассам\n"
        "/reload — перезагрузить справочники\n"
        "/cancel — отменить операцию\n"
        "/skip — пропустить комментарий\n"
        "/notify — уведомление всем (админ)\n"
        "/help — справка"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ========== ОСТАЛЬНЫЕ КОМАНДЫ ==========
async def start(update, context):
    remember_user(update.message.from_user.id)
    context.user_data.clear()
    kb = make_keyboard(CASHIERS, "cashier")
    await update.message.reply_text("Выберите кассу:", reply_markup=kb)

async def balance_cmd(update, context):
    remember_user(update.message.from_user.id)
    transactions = sheet_trans.get_all_records()
    balances = {}
    for cashier in CASHIERS:
        balances[cashier] = initial_balances.get(cashier, 0)
    for t in transactions:
        c = t.get("Касса", "")
        if c not in CASHIERS:
            continue
        try:
            a = float(t.get("Сумма", 0))
        except:
            continue
        balances[c] = balances.get(c, 0) + (a if t.get("Тип") == "Приход" else -a)
    text = "💰 <b>Остатки:</b>\n"
    for c in CASHIERS:
        text += f"• {c}: {balances.get(c, 0):,.2f} ₽\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def reload_cmd(update, context):
    global expense_articles, initial_balances, known_users
    expense_articles = load_expense_articles()
    initial_balances = load_initial_balances()
    known_users = load_users()
    await update.message.reply_text(
        f"✅ Перезагружено. Статей: {len(expense_articles)}, Пользователей: {len(known_users)}"
    )

async def notify_cmd(update, context):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только администратор.")
        return
    text = update.message.text.replace("/notify ", "", 1)
    if not text or text == "/notify":
        await update.message.reply_text("Напишите: /notify Текст")
        return
    global known_users
    known_users = load_users()  # свежий список из таблицы
    success = 0
    for uid in list(known_users):
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 {text}")
            success += 1
        except:
            known_users.discard(uid)
    await update.message.reply_text(f"✅ Отправлено: {success}")

# ========== ОБРАБОТЧИК КНОПОК ==========
async def button_handler(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data
    remember_user(q.from_user.id)

    if data == "back":
        context.user_data.clear()
        kb = make_keyboard(CASHIERS, "cashier")
        await q.edit_message_text("Выберите кассу:", reply_markup=kb)
        return

    if data.startswith("cashier:"):
        idx = int(data.split(":")[1])
        cashier = CASHIERS[idx]
        if not authorized(q.from_user.id, cashier):
            await q.answer("⛔ Нет доступа", show_alert=True)
            return
        context.user_data["cashier"] = cashier
        context.user_data["responsible_name"] = q.from_user.full_name
        await q.edit_message_text(f"Касса: {cashier}")
        if cashier not in initial_balances:
            context.user_data["waiting"] = "init_balance"
            await q.message.reply_text("Введите начальный остаток:")
        else:
            kb = make_keyboard(["Приход", "Расход"], "optype", add_back=True)
            await q.message.reply_text("Тип операции:", reply_markup=kb)
        return

    if data == "optype:0":  # Приход
        context.user_data["optype"] = "Приход"
        await q.edit_message_text("Тип: Приход")
        kb = make_keyboard(SOURCES, "source", add_back=True)
        await q.message.reply_text("Счёт списания:", reply_markup=kb)
        return
    if data == "optype:1":  # Расход
        context.user_data["optype"] = "Расход"
        await q.edit_message_text("Тип: Расход")
        kb = make_keyboard(expense_articles, "expense", add_back=True, add_custom=True)
        await q.message.reply_text("Статья расхода:", reply_markup=kb)
        return

    if data.startswith("source:"):
        idx = int(data.split(":")[1])
        context.user_data["source"] = SOURCES[idx]
        await q.edit_message_text(f"Счёт: {SOURCES[idx]}")
        context.user_data["waiting"] = "income_sum"
        await q.message.reply_text("Сумма прихода:")
        return

    if data.startswith("expense:"):
        idx = int(data.split(":")[1])
        context.user_data["expense_article"] = expense_articles[idx]
        await q.edit_message_text(f"Статья: {expense_articles[idx]}")
        context.user_data["waiting"] = "expense_sum"
        await q.message.reply_text("Сумма расхода:")
        return

    if data == "custom_article":
        context.user_data["waiting"] = "custom_article"
        await q.message.reply_text("Название новой статьи:")
        return

# ========== ОБРАБОТЧИК ТЕКСТА ==========
async def text_handler(update, context):
    msg = update.message.text.strip()
    waiting = context.user_data.get("waiting", "")

    if waiting == "init_balance":
        try:
            bal = float(msg.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Введите число:")
            return
        cashier = context.user_data["cashier"]
        sheet_bal.append_row([cashier, bal], value_input_option="USER_ENTERED")
        initial_balances[cashier] = bal
        context.user_data.pop("waiting", None)
        await update.message.reply_text(f"✅ Остаток {bal:.2f} сохранён.")
        kb = make_keyboard(["Приход", "Расход"], "optype", add_back=True)
        await update.message.reply_text("Тип операции:", reply_markup=kb)
        return

    if waiting == "income_sum":
        try:
            amount = float(msg.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Введите число:")
            return
        context.user_data["amount"] = amount
        context.user_data["waiting"] = "income_comment"
        await update.message.reply_text("Комментарий (/skip):")
        return

    if waiting == "expense_sum":
        try:
            amount = float(msg.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Введите число:")
            return
        context.user_data["amount"] = amount
        context.user_data["waiting"] = "expense_comment"
        await update.message.reply_text("Комментарий (/skip):")
        return

    if waiting == "custom_article":
        if msg not in expense_articles:
            expense_articles.append(msg)
            sheet_art.append_row([msg], value_input_option="USER_ENTERED")
            await update.message.reply_text(f"✅ Статья «{msg}» добавлена.")
        context.user_data["expense_article"] = msg
        context.user_data["waiting"] = "expense_sum"
        await update.message.reply_text("Сумма расхода:")
        return

    if waiting == "income_comment":
        await finalize(update, context, msg)
        return

    if waiting == "expense_comment":
        await finalize(update, context, msg)
        return

async def skip(update, context):
    waiting = context.user_data.get("waiting", "")
    if waiting in ("income_comment", "expense_comment"):
        await finalize(update, context, "")
    else:
        await update.message.reply_text("Нечего пропускать.")

async def finalize(update, context, comment):
    data = context.user_data
    now = datetime.now()
    sheet_trans.append_row([
        now.strftime("%d.%m.%Y"), now.strftime("%H:%M:%S"),
        data.get("cashier", ""), data.get("responsible_name", ""),
        data.get("optype", ""), data.get("source", ""),
        data.get("expense_article", ""), data.get("amount", 0), comment
    ], value_input_option="USER_ENTERED")
    context.user_data.clear()
    await update.message.reply_text("✅ Записано в таблицу.")

async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено. /start для начала.")

# ========== ЗАПУСК ==========
async def main():
    logging.basicConfig(level=logging.INFO)
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("notify", notify_cmd))
    app.add_handler(CommandHandler("skip", skip))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    print("Бот запущен...")

    from aiohttp import web
    wapp = web.Application()
    wapp.add_routes([web.get("/", lambda r: web.Response(text="OK"))])
    runner = web.AppRunner(wapp)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
