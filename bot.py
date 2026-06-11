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
else:
    raise Exception("Переменная GOOGLE_CREDENTIALS_JSON не найдена!")

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
gclient = gspread.authorize(creds)

sheet_trans = gclient.open(SPREADSHEET_NAME).worksheet(SHEET_TRANSACTIONS)
sheet_bal = gclient.open(SPREADSHEET_NAME).worksheet(SHEET_BALANCES)
sheet_art = gclient.open(SPREADSHEET_NAME).worksheet(SHEET_ARTICLES)

# ========== КОНФИГУРАЦИЯ ПОЛЬЗОВАТЕЛЕЙ ==========
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
RESPONSIBLE = json.loads(os.getenv("RESPONSIBLE_JSON", "{}"))

# ========== ЗАГРУЗКА ДАННЫХ ИЗ ТАБЛИЦ ==========
def load_initial_balances():
    records = sheet_bal.get_all_records()
    return {r["Касса"]: float(r["Начальный остаток"]) for r in records if r["Касса"]}

def load_expense_articles():
    articles = sheet_art.col_values(1)
    if articles and articles[0].strip().lower() == "статья":
        articles = articles[1:]
    return [a.strip() for a in articles if a.strip()]

initial_balances = load_initial_balances()
expense_articles = load_expense_articles()
if not expense_articles:
    expense_articles = ["Прочее"]

# ========== КЛАВИАТУРЫ ==========
def make_keyboard(options: list, callback_prefix: str, add_back: bool = False, add_custom: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=opt, callback_data=f"{callback_prefix}:{opt}")]
        for opt in options
    ]
    if add_back:
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    if add_custom:
        buttons.append([InlineKeyboardButton("✏️ Ввести новую статью", callback_data="custom_article")])
    return InlineKeyboardMarkup(buttons)

def is_authorized_for_cash(user_id: int, cashier: str) -> bool:
    if user_id == ADMIN_ID:
        return True
    responsible_id = RESPONSIBLE.get(cashier)
    return responsible_id is not None and user_id == responsible_id

# ========== СОСТОЯНИЯ ==========
CHOOSING_CASHIER, ENTERING_INITIAL_BALANCE, CHOOSING_TYPE, CHOOSING_SOURCE, ENTERING_INCOME_SUM, ENTERING_INCOME_COMMENT, CHOOSING_EXPENSE_ARTICLE, ENTERING_EXPENSE_SUM, ENTERING_EXPENSE_COMMENT, ENTERING_CUSTOM_ARTICLE = range(10)

# ========== КОМАНДА /help ==========
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
<b>📋 Доступные команды:</b>

/start — начать новую операцию (приход/расход)
/balance — посмотреть текущие остатки по всем кассам
/reload — перезагрузить справочники из Google Таблицы (только для админа)
/cancel — отменить текущую операцию
/skip — пропустить ввод комментария
/help — показать эту справку

<b>💡 Как пользоваться:</b>
1. Отправьте /start
2. Выберите кассу
3. Выберите тип операции: Приход или Расход
4. Следуйте инструкциям бота
5. Для возврата используйте кнопку 🔙 Назад
"""
    await update.message.reply_text(text, parse_mode="HTML")

# ========== ОБРАБОТЧИКИ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = make_keyboard(["АЛЕКСЕЙ", "ЕВГЕНИЙ"], "cashier")
    await update.message.reply_text(
        "👋 Добро пожаловать в бот учёта кассы!\n\n"
        "Выберите кассу:",
        reply_markup=keyboard
    )
    return CHOOSING_CASHIER

async def back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    keyboard = make_keyboard(["АЛЕКСЕЙ", "ЕВГЕНИЙ"], "cashier")
    await query.edit_message_text("Выберите кассу:", reply_markup=keyboard)
    return CHOOSING_CASHIER

async def choose_cashier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cashier = query.data.split(":", 1)[1]
    user_id = query.from_user.id

    if not is_authorized_for_cash(user_id, cashier):
        await query.answer("⛔ У вас нет доступа к этой кассе.", show_alert=True)
        return CHOOSING_CASHIER

    context.user_data["cashier"] = cashier
    context.user_data["responsible_name"] = query.from_user.full_name
    await query.edit_message_text(f"Касса: {cashier}")

    if cashier not in initial_balances:
        await query.message.reply_text(
            "Для этой кассы ещё не задан начальный остаток.\n"
            "Введите сумму начального остатка (число, например 15000):"
        )
        return ENTERING_INITIAL_BALANCE
    else:
        keyboard = make_keyboard(["Приход", "Расход"], "optype", add_back=True)
        await query.message.reply_text("Выберите тип операции:", reply_markup=keyboard)
        return CHOOSING_TYPE

async def enter_initial_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введите корректное число (или /cancel):")
        return ENTERING_INITIAL_BALANCE

    cashier = context.user_data["cashier"]
    await asyncio.to_thread(
        sheet_bal.append_row,
        [cashier, balance],
        value_input_option="USER_ENTERED"
    )
    initial_balances[cashier] = balance

    await update.message.reply_text(f"✅ Начальный остаток {balance:.2f} для кассы {cashier} сохранён.")
    keyboard = make_keyboard(["Приход", "Расход"], "optype", add_back=True)
    await update.message.reply_text("Выберите тип операции:", reply_markup=keyboard)
    return CHOOSING_TYPE

async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "back":
        return await back_to_start(update, context)
    
    optype = query.data.split(":", 1)[1]
    context.user_data["optype"] = optype
    await query.edit_message_text(f"Тип: {optype}")

    if optype == "Приход":
        sources = ["ИП Герасимов", "ИП Уварова", "ИП Смирнов", "ООО Техвижения"]
        keyboard = make_keyboard(sources, "source", add_back=True)
        await query.message.reply_text("С какого счёта сняты деньги?", reply_markup=keyboard)
        return CHOOSING_SOURCE
    else:
        keyboard = make_keyboard(expense_articles, "expense", add_back=True, add_custom=True)
        await query.message.reply_text("Выберите статью расхода:", reply_markup=keyboard)
        return CHOOSING_EXPENSE_ARTICLE

async def choose_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "back":
        keyboard = make_keyboard(["Приход", "Расход"], "optype", add_back=True)
        await query.edit_message_text("Выберите тип операции:", reply_markup=keyboard)
        return CHOOSING_TYPE
    
    source = query.data.split(":", 1)[1]
    context.user_data["source"] = source
    await query.edit_message_text(f"Счёт: {source}")
    await query.message.reply_text("Введите сумму прихода (или /cancel):")
    return ENTERING_INCOME_SUM

async def start_custom_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Введите название новой статьи (или /cancel):")
    return ENTERING_CUSTOM_ARTICLE

async def choose_expense_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "back":
        keyboard = make_keyboard(["Приход", "Расход"], "optype", add_back=True)
        await query.edit_message_text("Выберите тип операции:", reply_markup=keyboard)
        return CHOOSING_TYPE
    
    if query.data == "custom_article":
        await query.message.reply_text("Введите название новой статьи (или /cancel):")
        return ENTERING_CUSTOM_ARTICLE
    
    article = query.data.split(":", 1)[1]
    context.user_data["expense_article"] = article
    await query.edit_message_text(f"Статья: {article}")
    await query.message.reply_text("Введите сумму расхода (или /cancel):")
    return ENTERING_EXPENSE_SUM

async def custom_expense_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_article = update.message.text.strip()
    if not new_article:
        await update.message.reply_text("Введите непустую статью (или /cancel):")
        return ENTERING_CUSTOM_ARTICLE

    if new_article not in expense_articles:
        expense_articles.append(new_article)
        await asyncio.to_thread(
            sheet_art.append_row,
            [new_article],
            value_input_option="USER_ENTERED"
        )
        await update.message.reply_text(f"✅ Новая статья «{new_article}» добавлена в справочник.")
    else:
        await update.message.reply_text(f"Статья «{new_article}» уже существует.")

    context.user_data["expense_article"] = new_article
    await update.message.reply_text("Введите сумму расхода (или /cancel):")
    return ENTERING_EXPENSE_SUM

async def enter_income_sum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введите корректное число (или /cancel):")
        return ENTERING_INCOME_SUM

    context.user_data["amount"] = amount
    await update.message.reply_text("Введите комментарий (или /skip, /cancel):")
    return ENTERING_INCOME_COMMENT

async def enter_expense_sum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введите корректное число (или /cancel):")
        return ENTERING_EXPENSE_SUM

    context.user_data["amount"] = amount
    await update.message.reply_text("Введите комментарий (или /skip, /cancel):")
    return ENTERING_EXPENSE_COMMENT

async def skip_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await finalize_transaction(update, context, "")
    return ConversationHandler.END

async def enter_income_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    await finalize_transaction(update, context, comment)
    return ConversationHandler.END

async def enter_expense_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    await finalize_transaction(update, context, comment)
    return ConversationHandler.END

async def finalize_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE, comment: str):
    data = context.user_data
    now = datetime.now()
    date_str = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M:%S")
    responsible = data.get("responsible_name", "Неизвестно")
    row = [
        date_str,
        time_str,
        data["cashier"],
        responsible,
        data["optype"],
        data.get("source", ""),
        data.get("expense_article", ""),
        data["amount"],
        comment
    ]
    await asyncio.to_thread(
        sheet_trans.append_row,
        row,
        value_input_option="USER_ENTERED"
    )
    await update.message.reply_text("✅ Операция записана в таблицу.")
    context.user_data.clear()

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Операция отменена.\nНажмите /start чтобы начать заново.")
    return ConversationHandler.END

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    transactions = sheet_trans.get_all_records()
    balances = {}
    for cashier, init_bal in initial_balances.items():
        balances[cashier] = init_bal

    for t in transactions:
        cashier = t.get("Касса", "")
        if not cashier:
            continue
        optype = t.get("Тип", "")
        try:
            amount = float(t.get("Сумма", 0))
        except (ValueError, TypeError):
            continue
        if optype == "Приход":
            balances[cashier] = balances.get(cashier, 0) + amount
        elif optype == "Расход":
            balances[cashier] = balances.get(cashier, 0) - amount

    if not balances:
        await update.message.reply_text("Нет данных по кассам.")
        return

    text = "💰 <b>Текущие остатки по кассам:</b>\n"
    for cashier in ["АЛЕКСЕЙ", "ЕВГЕНИЙ"]:
        bal = balances.get(cashier, 0)
        text += f"• {cashier}: {bal:,.2f} ₽\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только администратор может перезагрузить справочники.")
        return
    global expense_articles, initial_balances
    expense_articles = load_expense_articles()
    initial_balances = load_initial_balances()
    await update.message.reply_text("✅ Справочники статей и начальных остатков перезагружены из таблицы.")

# ========== ЗАПУСК ==========
async def main():
    logging.basicConfig(level=logging.INFO)
    
    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_CASHIER: [
                CallbackQueryHandler(choose_cashier, pattern="^cashier:"),
            ],
            ENTERING_INITIAL_BALANCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_initial_balance),
            ],
            CHOOSING_TYPE: [
                CallbackQueryHandler(choose_type, pattern="^optype:"),
                CallbackQueryHandler(back_to_start, pattern="^back$"),
            ],
            CHOOSING_SOURCE: [
                CallbackQueryHandler(choose_source, pattern="^source:"),
                CallbackQueryHandler(choose_type, pattern="^back$"),
            ],
            ENTERING_INCOME_SUM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_income_sum),
            ],
            ENTERING_INCOME_COMMENT: [
                CommandHandler("skip", skip_comment),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_income_comment),
            ],
            CHOOSING_EXPENSE_ARTICLE: [
                CallbackQueryHandler(choose_expense_article, pattern="^expense:"),
                CallbackQueryHandler(choose_type, pattern="^back$"),
                CallbackQueryHandler(start_custom_article, pattern="^custom_article$"),
            ],
            ENTERING_CUSTOM_ARTICLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, custom_expense_article),
            ],
            ENTERING_EXPENSE_SUM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_expense_sum),
            ],
            ENTERING_EXPENSE_COMMENT: [
                CommandHandler("skip", skip_comment),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_expense_comment),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("balance", cmd_balance))
    application.add_handler(CommandHandler("reload", cmd_reload))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("start", start))

    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    print("Бот запущен на Render...")
    
    from aiohttp import web
    
    async def health(request):
        return web.Response(text="OK")
    
    app = web.Application()
    app.add_routes([web.get("/", health)])
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
