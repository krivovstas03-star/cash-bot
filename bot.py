import asyncio
import json
import logging
import os
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ========== НАСТРОЙКИ ==========
TOKEN = "8837823632:AAG9dEOT5HNvTwmv2njWcX_gKu_WrXe0C3I"
SPREADSHEET_NAME = "Касса_Учёт"
SHEET_TRANSACTIONS = "Транзакции"
SHEET_BALANCES = "Остатки"
SHEET_ARTICLES = "Статьи"
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

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
RESPONSIBLE = json.loads(os.getenv("RESPONSIBLE_JSON", "{}"))

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
    print(f"DEBUG: Загружено статей: {len(result)}")
    return result if result else ["Статья 1 (тест)", "Статья 2 (тест)"]

initial_balances = load_initial_balances()
expense_articles = load_expense_articles()

CHOOSING_CASHIER, ENTERING_INITIAL_BALANCE, CHOOSING_TYPE, CHOOSING_SOURCE, ENTERING_INCOME_SUM, ENTERING_INCOME_COMMENT, CHOOSING_EXPENSE_ARTICLE, ENTERING_EXPENSE_SUM, ENTERING_EXPENSE_COMMENT, ENTERING_CUSTOM_ARTICLE = range(10)

def make_keyboard(options, prefix, add_back=False, add_custom=False):
    buttons = []
    for i, opt in enumerate(options):
        safe_data = f"{prefix}:{i}"
        buttons.append([InlineKeyboardButton(text=opt, callback_data=safe_data)])
    if add_back:
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    if add_custom:
        buttons.append([InlineKeyboardButton("✏️ Новая статья", callback_data="custom_article")])
    return InlineKeyboardMarkup(buttons)

def authorized(user_id, cashier):
    return user_id == ADMIN_ID or RESPONSIBLE.get(cashier) == user_id

async def start(update, context):
    context.user_data.clear()
    kb = make_keyboard(["АЛЕКСЕЙ", "ЕВГЕНИЙ"], "cashier")
    await update.message.reply_text("Выберите кассу:", reply_markup=kb)
    return CHOOSING_CASHIER

async def back_start(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    kb = make_keyboard(["АЛЕКСЕЙ", "ЕВГЕНИЙ"], "cashier")
    await q.edit_message_text("Выберите кассу:", reply_markup=kb)
    return CHOOSING_CASHIER

async def pick_cashier(update, context):
    q = update.callback_query
    await q.answer()
    cashier = q.data.split(":", 1)[1]
    if not authorized(q.from_user.id, cashier):
        await q.answer("⛔ Нет доступа", show_alert=True)
        return CHOOSING_CASHIER
    context.user_data.update(cashier=cashier, responsible_name=q.from_user.full_name)
    await q.edit_message_text(f"Касса: {cashier}")
    if cashier not in initial_balances:
        await q.message.reply_text("Введите начальный остаток:")
        return ENTERING_INITIAL_BALANCE
    kb = make_keyboard(["Приход", "Расход"], "optype", add_back=True)
    await q.message.reply_text("Тип операции:", reply_markup=kb)
    return CHOOSING_TYPE

async def init_balance(update, context):
    try:
        bal = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введите число:")
        return ENTERING_INITIAL_BALANCE
    cashier = context.user_data["cashier"]
    sheet_bal.append_row([cashier, bal], value_input_option="USER_ENTERED")
    initial_balances[cashier] = bal
    await update.message.reply_text(f"✅ Остаток {bal:.2f} сохранён.")
    kb = make_keyboard(["Приход", "Расход"], "optype", add_back=True)
    await update.message.reply_text("Тип операции:", reply_markup=kb)
    return CHOOSING_TYPE

async def pick_type(update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "back":
        return await back_start(update, context)
    optype = q.data.split(":", 1)[1]
    context.user_data["optype"] = optype
    await q.edit_message_text(f"Тип: {optype}")
    if optype == "Приход":
        sources = ["ИП Герасимов", "ИП Уварова", "ИП Смирнов", "ООО Техвижения"]
        kb = make_keyboard(sources, "source", add_back=True)
        await q.message.reply_text("Счёт списания:", reply_markup=kb)
        return CHOOSING_SOURCE
    else:
        kb = make_keyboard(expense_articles, "expense", add_back=True, add_custom=True)
        await q.message.reply_text("Статья расхода:", reply_markup=kb)
        return CHOOSING_EXPENSE_ARTICLE

async def pick_source(update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "back":
        kb = make_keyboard(["Приход", "Расход"], "optype", add_back=True)
        await q.edit_message_text("Тип операции:", reply_markup=kb)
        return CHOOSING_TYPE
    idx = int(q.data.split(":", 1)[1])
    sources = ["ИП Герасимов", "ИП Уварова", "ИП Смирнов", "ООО Техвижения"]
    context.user_data["source"] = sources[idx]
    await q.edit_message_text(f"Счёт: {sources[idx]}")
    await q.message.reply_text("Сумма прихода:")
    return ENTERING_INCOME_SUM

async def pick_article(update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "back":
        kb = make_keyboard(["Приход", "Расход"], "optype", add_back=True)
        await q.edit_message_text("Тип операции:", reply_markup=kb)
        return CHOOSING_TYPE
    if q.data == "custom_article":
        await q.message.reply_text("Название новой статьи:")
        return ENTERING_CUSTOM_ARTICLE
    idx = int(q.data.split(":", 1)[1])
    context.user_data["expense_article"] = expense_articles[idx]
    await q.edit_message_text(f"Статья: {expense_articles[idx]}")
    await q.message.reply_text("Сумма расхода:")
    return ENTERING_EXPENSE_SUM

async def custom_article(update, context):
    new_a = update.message.text.strip()
    if not new_a:
        await update.message.reply_text("Введите название:")
        return ENTERING_CUSTOM_ARTICLE
    if new_a not in expense_articles:
        expense_articles.append(new_a)
        sheet_art.append_row([new_a], value_input_option="USER_ENTERED")
        await update.message.reply_text(f"✅ Статья «{new_a}» добавлена.")
    context.user_data["expense_article"] = new_a
    await update.message.reply_text("Сумма расхода:")
    return ENTERING_EXPENSE_SUM

async def sum_income(update, context):
    try:
        context.user_data["amount"] = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Число:")
        return ENTERING_INCOME_SUM
    await update.message.reply_text("Комментарий (/skip):")
    return ENTERING_INCOME_COMMENT

async def sum_expense(update, context):
    try:
        context.user_data["amount"] = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Число:")
        return ENTERING_EXPENSE_SUM
    await update.message.reply_text("Комментарий (/skip):")
    return ENTERING_EXPENSE_COMMENT

async def skip(update, context):
    await finalize(update, context, "")
    return ConversationHandler.END

async def comment_income(update, context):
    await finalize(update, context, update.message.text.strip())
    return ConversationHandler.END

async def comment_expense(update, context):
    await finalize(update, context, update.message.text.strip())
    return ConversationHandler.END

async def finalize(update, context, comment):
    data = context.user_data
    now = datetime.now()
    sheet_trans.append_row([
        now.strftime("%d.%m.%Y"), now.strftime("%H:%M:%S"),
        data["cashier"], data.get("responsible_name", ""),
        data["optype"], data.get("source", ""),
        data.get("expense_article", ""), data["amount"], comment
    ], value_input_option="USER_ENTERED")
    await update.message.reply_text("✅ Записано в таблицу.")
    context.user_data.clear()

async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено. /start для начала.")
    return ConversationHandler.END

async def balance_cmd(update, context):
    transactions = sheet_trans.get_all_records()
    balances = dict(initial_balances)
    for t in transactions:
        c = t.get("Касса", "")
        try:
            a = float(t.get("Сумма", 0))
        except:
            continue
        balances[c] = balances.get(c, 0) + (a if t.get("Тип") == "Приход" else -a)
    text = "💰 <b>Остатки:</b>\n" + "\n".join(f"• {k}: {v:,.2f} ₽" for k, v in balances.items())
    await update.message.reply_text(text, parse_mode="HTML")

async def reload_cmd(update, context):
    global expense_articles, initial_balances
    expense_articles = load_expense_articles()
    initial_balances = load_initial_balances()
    await update.message.reply_text(f"✅ Перезагружено. Статей: {len(expense_articles)}")

async def help_cmd(update, context):
    await update.message.reply_text("/start /balance /reload /cancel /skip /help")

async def main():
    logging.basicConfig(level=logging.INFO)
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_CASHIER: [CallbackQueryHandler(pick_cashier, pattern="^cashier:")],
            ENTERING_INITIAL_BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, init_balance)],
            CHOOSING_TYPE: [CallbackQueryHandler(pick_type, pattern="^optype:"), CallbackQueryHandler(back_start, pattern="^back$")],
            CHOOSING_SOURCE: [CallbackQueryHandler(pick_source, pattern="^source:"), CallbackQueryHandler(pick_type, pattern="^back$")],
            ENTERING_INCOME_SUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, sum_income)],
            ENTERING_INCOME_COMMENT: [CommandHandler("skip", skip), MessageHandler(filters.TEXT & ~filters.COMMAND, comment_income)],
            CHOOSING_EXPENSE_ARTICLE: [
                CallbackQueryHandler(pick_article, pattern="^expense:"),
                CallbackQueryHandler(pick_type, pattern="^back$"),
                CallbackQueryHandler(custom_article, pattern="^custom_article$"),
            ],
            ENTERING_CUSTOM_ARTICLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_article)],
            ENTERING_EXPENSE_SUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, sum_expense)],
            ENTERING_EXPENSE_COMMENT: [CommandHandler("skip", skip), MessageHandler(filters.TEXT & ~filters.COMMAND, comment_expense)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

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
