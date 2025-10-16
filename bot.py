import os
import logging
import sqlite3
import re
from datetime import datetime
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters
)

# === Настройка ===
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ORS_API_KEY = os.getenv("ORS_API_KEY")  # Пока не используется, но заготовлено

# Координаты склада: [долгота, широта] — ЗАМЕНИ НА СВОИ!
WAREHOUSE_COORDS = [73.17325327166235, 55.001957853274014]  # Пример: Москва, Красная площадь

if not TOKEN or not WEBHOOK_URL:
    raise RuntimeError("❌ TELEGRAM_TOKEN и WEBHOOK_URL обязательны!")

# === Состояния диалога ===
DATE, TIME, LOCATION, SEATS = range(4)

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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS driver_rides (
            ride_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            departure_lat REAL NOT NULL,
            departure_lon REAL NOT NULL,
            date TEXT NOT NULL,        -- YYYY-MM-DD
            time TEXT NOT NULL,        -- HH:MM
            seats INTEGER NOT NULL,    -- 1–4
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_role(user_id, role):
    conn = sqlite3.connect('users.db')
    conn.execute('INSERT OR REPLACE INTO users (user_id, role) VALUES (?, ?)', (user_id, role))
    conn.commit()
    conn.close()

def save_driver_ride(user_id, lat, lon, date, time, seats):
    conn = sqlite3.connect('users.db')
    conn.execute('''
        INSERT INTO driver_rides (user_id, departure_lat, departure_lon, date, time, seats)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, lat, lon, date, time, seats))
    conn.commit()
    conn.close()

# === Обработчики команд ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n"
        "Я бот для поиска попутчиков на склад.\n"
        "Выберите роль:\n"
        "→ /driver — если вы водитель\n"
        "→ /passenger — если вы пассажир"
    )

async def driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    save_role(user_id, "driver")
    await update.message.reply_text("Вы выбрали роль: водитель. 🚗\n"
                                    "Чтобы создать поездку, используйте /new_ride")

async def passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    save_role(user_id, "passenger")
    await update.message.reply_text("Вы выбрали роль: пассажир. 🧑‍💼")

# === Диалог создания поездки ===
async def new_ride(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()

    if not result or result[0] != "driver":
        await update.message.reply_text("❌ Только водители могут создавать поездки. Сначала выберите /driver.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📅 Введите дату поездки в формате ДД.ММ.ГГГГ (например, 18.10.2025):"
    )
    return DATE

async def receive_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', text):
        context.user_data['date'] = text
        await update.message.reply_text("⏰ Введите время отправления в формате ЧЧ:ММ (например, 08:30):")
        return TIME
    else:
        await update.message.reply_text("❌ Неверный формат. Попробуйте: ДД.ММ.ГГГГ")
        return DATE

async def receive_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if re.match(r'^\d{1,2}:\d{2}$', text):
        h, m = text.split(':')
        time_norm = f"{int(h):02d}:{int(m):02d}"
        context.user_data['time'] = time_norm
        await update.message.reply_text("📍 Отправьте свою геолокацию (нажмите скрепку → Геопозиция):")
        return LOCATION
    else:
        await update.message.reply_text("❌ Неверный формат. Попробуйте: ЧЧ:ММ")
        return TIME

async def receive_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    location = update.message.location
    if location:
        context.user_data['lat'] = location.latitude
        context.user_data['lon'] = location.longitude
        await update.message.reply_text("🚗 Сколько свободных мест в машине? (1–4):")
        return SEATS
    else:
        await update.message.reply_text("❌ Пожалуйста, отправьте геолокацию через Telegram.")
        return LOCATION

async def receive_seats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        seats = int(update.message.text.strip())
        if 1 <= seats <= 4:
            context.user_data['seats'] = seats

            user_id = update.effective_user.id
            date_str = context.user_data['date']
            time_str = context.user_data['time']
            lat = context.user_data['lat']
            lon = context.user_data['lon']

            # Преобразуем дату в YYYY-MM-DD
            date_db = datetime.strptime(date_str, "%d.%m.%Y").strftime("%Y-%m-%d")

            save_driver_ride(user_id, lat, lon, date_db, time_str, seats)

            await update.message.reply_text(
                f"✅ Поездка создана!\n"
                f"Дата: {date_str}\n"
                f"Время: {time_str}\n"
                f"Мест: {seats}\n"
                f"Откуда: {lat:.4f}, {lon:.4f}"
            )
            return ConversationHandler.END
        else:
            await update.message.reply_text("❌ Укажите число от 1 до 4.")
            return SEATS
    except ValueError:
        await update.message.reply_text("❌ Введите число (1–4).")
        return SEATS

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Создание поездки отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# === Основная функция ===
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    # Обычные команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("driver", driver))
    app.add_handler(CommandHandler("passenger", passenger))

    # Диалог для водителя
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("new_ride", new_ride)],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_date)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_time)],
            LOCATION: [MessageHandler(filters.LOCATION, receive_location)],
            SEATS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_seats)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    app.add_handler(conv_handler)

    # Запуск webhook
    full_webhook_url = f"{WEBHOOK_URL}/{TOKEN}"
    logging.info(f"📡 Устанавливаю webhook: {full_webhook_url}")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=full_webhook_url,
        url_path=TOKEN
    )

if __name__ == '__main__':
    main()