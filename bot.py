import os
import logging
import sqlite3
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# === Настройка ===
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Например: https://ride-share-bot.onrender.com

if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан!")
if not WEBHOOK_URL:
    raise ValueError("WEBHOOK_URL не задан!")

# === База данных ===
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def save_role(user_id: int, role: str):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO users (user_id, role) VALUES (?, ?)', (user_id, role))
    conn.commit()
    conn.close()

# === Telegram-бот ===
app = Flask(__name__)
telegram_app = Application.builder().token(TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n"
        "Выберите роль:\n"
        "→ /driver — водитель\n"
        "→ /passenger — пассажир"
    )

async def driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_role(update.effective_user.id, "driver")
    await update.message.reply_text("Вы — водитель. 🚗")

async def passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_role(update.effective_user.id, "passenger")
    await update.message.reply_text("Вы — пассажир. 🧑‍💼")

# Регистрация обработчиков
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("driver", driver))
telegram_app.add_handler(CommandHandler("passenger", passenger))

# === Webhook и маршруты ===
@app.before_first_request
def setup_webhook():
    webhook_full_url = f"{WEBHOOK_URL}/{TOKEN}"
    telegram_app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_full_url,
        url_path=TOKEN
    )
    logging.info(f"Webhook установлен на: {webhook_full_url}")

@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    telegram_app.update_queue.put_nowait(
        telegram_app.update_processor.read_update(request.get_json())
    )
    return 'OK', 200

@app.route('/')
def health_check():
    return '✅ Бот работает! (Webhook активен)', 200

# === Запуск ===
if __name__ == '__main__':
    init_db()
    app.run(host="0.0.0.0", port=PORT)