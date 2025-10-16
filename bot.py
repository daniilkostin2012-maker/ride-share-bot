import os
import logging
import sqlite3
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not TOKEN or not WEBHOOK_URL:
    raise ValueError("TELEGRAM_TOKEN и WEBHOOK_URL должны быть заданы!")

# === База данных ===
def init_db():
    conn = sqlite3.connect('users.db')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def save_role(user_id, role):
    conn = sqlite3.connect('users.db')
    conn.execute('INSERT OR REPLACE INTO users (user_id, role) VALUES (?, ?)', (user_id, role))
    conn.commit()
    conn.close()

# === Обработчики ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Выберите роль: /driver или /passenger")

async def driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_role(update.effective_user.id, "driver")
    await update.message.reply_text("Вы — водитель. 🚗")

async def passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_role(update.effective_user.id, "passenger")
    await update.message.reply_text("Вы — пассажир. 🧑‍💼")

# === Запуск ===
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("driver", driver))
    app.add_handler(CommandHandler("passenger", passenger))

    webhook_full_url = f"{WEBHOOK_URL}/{TOKEN}"
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_full_url,
        url_path=TOKEN
    )
    logging.info(f"Бот запущен. Webhook: {webhook_full_url}")

if __name__ == '__main__':
    main()