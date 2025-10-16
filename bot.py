import os
import logging
import sqlite3
import re
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)
from geopy.distance import geodesic
import polyline

# === Настройка ===
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ORS_API_KEY = os.getenv("ORS_API_KEY")

# Координаты склада: [долгота, широта] — ЗАМЕНИ НА СВОИ!
WAREHOUSE_COORDS = [37.618423, 55.751244]  # [lon, lat]

if not TOKEN or not WEBHOOK_URL:
    raise RuntimeError("❌ TELEGRAM_TOKEN и WEBHOOK_URL обязательны!")

# === Состояния ===
AWAITING_LOCATION = "awaiting_location"
AWAITING_SEATS = "awaiting_seats"
AWAITING_P_LOCATION = "awaiting_p_location"

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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS passenger_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            matched_ride_id INTEGER,
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

def count_active_rides(user_id):
    conn = sqlite3.connect('users.db')
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(*) FROM driver_rides
        WHERE user_id = ? AND datetime(date || ' ' || time, '+2 hours') >= ?
    ''', (user_id, now))
    count = cursor.fetchone()[0]
    conn.close()
    return count

def cleanup_old_rides_and_requests():
    conn = sqlite3.connect('users.db')
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute('DELETE FROM driver_rides WHERE datetime(date || " " || time, "+2 hours") < ?', (now,))
    conn.execute('DELETE FROM passenger_requests WHERE created_at < datetime("now", "-1 day")')
    conn.commit()
    conn.close()

# === OpenRouteService и проверка маршрута ===
def get_route_polyline(start_lat, start_lon, api_key, warehouse_coords):
    if not api_key:
        return None
    url = "https://api.openrouteservice.org/v2/directions/driving-car"
    headers = {'Authorization': api_key, 'Content-Type': 'application/json'}
    body = {"coordinates": [[start_lon, start_lat], warehouse_coords]}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()['routes'][0]['geometry']
    except Exception as e:
        logging.error(f"ORS error: {e}")
    return None

def is_point_near_route(pass_lat, pass_lon, start_lat, start_lon, max_dist_m=100):
    # Если нет ORS — проверяем расстояние до склада (упрощённо)
    warehouse_lat, warehouse_lon = WAREHOUSE_COORDS[1], WAREHOUSE_COORDS[0]
    dist_to_warehouse = geodesic((pass_lat, pass_lon), (warehouse_lat, warehouse_lon)).meters
    if dist_to_warehouse > 10000:  # дальше 10 км — точно не подходит
        return False

    # Пробуем построить маршрут
    polyline_str = get_route_polyline(start_lat, start_lon, ORS_API_KEY, WAREHOUSE_COORDS)
    if not polyline_str:
        # Без маршрута — считаем, что если пассажир в пределах 2 км от линии "дом-склад", то OK
        dist_to_line = geodesic((pass_lat, pass_lon), (start_lat, start_lon)).meters
        return dist_to_line <= 2000

    try:
        coords = polyline.decode(polyline_str)  # [(lat, lon), ...]
        for lat, lon in coords:
            if geodesic((pass_lat, pass_lon), (lat, lon)).meters <= max_dist_m:
                return True
        return False
    except Exception as e:
        logging.error(f"Ошибка декодирования полилинии: {e}")
        return False

# === Вспомогательные функции уведомлений ===
async def notify_driver_of_passenger(context: ContextTypes.DEFAULT_TYPE, driver_id, passenger_user, p_date, p_time, p_lat, p_lon):
    username = passenger_user.username or "недоступен"
    try:
        await context.bot.send_message(
            chat_id=driver_id,
            text=f"👤 Новый пассажир рядом с вашим маршрутом!\n"
                 f"Время: {p_time}, дата: {p_date}\n"
                 f"Координаты: {p_lat:.4f}, {p_lon:.4f}\n"
                 f"Telegram: @{username}"
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить водителя {driver_id}: {e}")

async def notify_passenger_no_ride(context: ContextTypes.DEFAULT_TYPE, user_id):
    try:
        await context.bot.send_message(user_id, "❌ Пока нет подходящих поездок. Как только появится — уведомим!")
    except:
        pass

# === Обработчики команд ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n"
        "Выберите роль:\n"
        "→ /driver — водитель\n"
        "→ /passenger — пассажир"
    )

async def driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_role(update.effective_user.id, "driver")
    await update.message.reply_text("Вы — водитель. 🚗\nСоздайте поездку: /new_ride")

async def passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_role(update.effective_user.id, "passenger")
    await update.message.reply_text("Вы — пассажир. 🧑‍💼\nНайдите поездку: /find_ride")

# === Выбор даты и времени (водитель) ===
async def new_ride(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect('users.db')
    role = conn.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if not role or role[0] != "driver":
        await update.message.reply_text("❌ Сначала выберите /driver")
        return

    if count_active_rides(user_id) >= 2:
        await update.message.reply_text("❌ Максимум 2 активные поездки.")
        return

    today = datetime.now().date()
    buttons = []
    for i in range(7):
        d = today + timedelta(days=i)
        btn = InlineKeyboardButton(d.strftime("%d.%m"), callback_data=f"date_driver_{d.isoformat()}")
        if i % 3 == 0:
            buttons.append([])
        buttons[-1].append(btn)
    await update.message.reply_text("📅 Выберите дату:", reply_markup=InlineKeyboardMarkup(buttons))

async def handle_date_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    date_iso = query.data.split("_", 2)[2]
    context.user_data['date'] = date_iso

    times = ["06:00", "06:30", "07:00", "07:30", "08:00", "08:30", "09:00", "17:00", "17:30", "18:00", "18:30", "19:00"]
    buttons = [[InlineKeyboardButton(t, callback_data=f"time_driver_{t}") for t in times[i:i+3]] for i in range(0, len(times), 3)]
    await query.edit_message_text("⏰ Выберите время:", reply_markup=InlineKeyboardMarkup(buttons))

async def handle_time_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    time_str = query.data.split("_", 2)[2]
    context.user_data['time'] = time_str
    context.user_data[AWAITING_LOCATION] = 'driver'
    await query.edit_message_text("📍 Отправьте геолокацию:")

# === Приём геолокации и мест ===
async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AWAITING_LOCATION not in context.user_data:
        return

    role = context.user_data[AWAITING_LOCATION]
    if role == 'driver':
        context.user_data['lat'] = update.message.location.latitude
        context.user_data['lon'] = update.message.location.longitude
        context.user_data[AWAITING_SEATS] = True
        del context.user_data[AWAITING_LOCATION]
        await update.message.reply_text("🚗 Сколько мест? (1–4):")
    elif role == 'passenger':
        context.user_data['p_lat'] = update.message.location.latitude
        context.user_data['p_lon'] = update.message.location.longitude
        await process_passenger_request(update, context)

async def handle_seats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AWAITING_SEATS not in context.user_data:
        return

    try:
        seats = int(update.message.text.strip())
        if 1 <= seats <= 4:
            user_id = update.effective_user.id
            date_iso = context.user_data['date']
            time_str = context.user_data['time']
            lat = context.user_data['lat']
            lon = context.user_data['lon']

            conn = sqlite3.connect('users.db')
            conn.execute('''
                INSERT INTO driver_rides (user_id, departure_lat, departure_lon, date, time, seats)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, lat, lon, date_iso, time_str, seats))
            ride_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            conn.close()

            await update.message.reply_text(f"✅ Поездка создана!\n{date_iso} в {time_str}")

            # Фоновая проверка пассажиров
            cleanup_old_rides_and_requests()
            conn = sqlite3.connect('users.db')
            passengers = conn.execute('''
                SELECT request_id, user_id, lat, lon, date, time
                FROM passenger_requests
                WHERE date = ? AND matched_ride_id IS NULL
            ''', (date_iso,)).fetchall()
            conn.close()

            for req_id, p_user_id, p_lat, p_lon, p_date, p_time in passengers:
                target = datetime.strptime(f"{date_iso} {time_str}", "%Y-%m-%d %H:%M")
                p_dt = datetime.strptime(f"{p_date} {p_time}", "%Y-%m-%d %H:%M")
                if abs((p_dt - target).total_seconds()) <= 3600:  # ±1 час
                    if is_point_near_route(p_lat, p_lon, lat, lon):
                        # Матчим
                        conn = sqlite3.connect('users.db')
                        conn.execute('UPDATE passenger_requests SET matched_ride_id = ? WHERE request_id = ?', (ride_id, req_id))
                        conn.commit()
                        conn.close()
                        user = await context.bot.get_chat(p_user_id)
                        await notify_driver_of_passenger(context, user_id, user, p_date, p_time, p_lat, p_lon)

            del context.user_data[AWAITING_SEATS]
        else:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите число от 1 до 4.")

# === Пассажир: выбор даты/времени ===
async def find_ride(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect('users.db')
    role = conn.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if not role or role[0] != "passenger":
        await update.message.reply_text("❌ Сначала выберите /passenger")
        return

    today = datetime.now().date()
    buttons = []
    for i in range(7):
        d = today + timedelta(days=i)
        btn = InlineKeyboardButton(d.strftime("%d.%m"), callback_data=f"date_passenger_{d.isoformat()}")
        if i % 3 == 0:
            buttons.append([])
        buttons[-1].append(btn)
    await update.message.reply_text("📅 Выберите дату:", reply_markup=InlineKeyboardMarkup(buttons))

async def handle_date_passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    date_iso = query.data.split("_", 2)[2]
    context.user_data['p_date'] = date_iso

    times = ["06:00", "06:30", "07:00", "07:30", "08:00", "08:30", "09:00", "17:00", "17:30", "18:00", "18:30", "19:00"]
    buttons = [[InlineKeyboardButton(t, callback_data=f"time_passenger_{t}") for t in times[i:i+3]] for i in range(0, len(times), 3)]
    await query.edit_message_text("⏰ Выберите время:", reply_markup=InlineKeyboardMarkup(buttons))

async def handle_time_passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    time_str = query.data.split("_", 2)[2]
    context.user_data['p_time'] = time_str
    context.user_data[AWAITING_P_LOCATION] = 'passenger'
    await query.edit_message_text("📍 Отправьте геолокацию:")

async def process_passenger_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    p_date_iso = context.user_data['p_date']
    p_time_str = context.user_data['p_time']
    p_lat = context.user_data['p_lat']
    p_lon = context.user_data['p_lon']

    conn = sqlite3.connect('users.db')
    conn.execute('''
        INSERT INTO passenger_requests (user_id, lat, lon, date, time, matched_ride_id)
        VALUES (?, ?, ?, ?, ?, NULL)
    ''', (user_id, p_lat, p_lon, p_date_iso, p_time_str))
    request_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    # Ищем активные поездки
    target = datetime.strptime(f"{p_date_iso} {p_time_str}", "%Y-%m-%d %H:%M")
    min_t = (target - timedelta(hours=1)).strftime("%H:%M")
    max_t = (target + timedelta(hours=1)).strftime("%H:%M")

    conn = sqlite3.connect('users.db')
    rides = conn.execute('''
        SELECT ride_id, user_id, departure_lat, departure_lon, time
        FROM driver_rides
        WHERE date = ? AND time BETWEEN ? AND ?
    ''', (p_date_iso, min_t, max_t)).fetchall()
    conn.close()

    matched = False
    for ride_id, driver_id, d_lat, d_lon, d_time in rides:
        if is_point_near_route(p_lat, p_lon, d_lat, d_lon):
            conn = sqlite3.connect('users.db')
            conn.execute('UPDATE passenger_requests SET matched_ride_id = ? WHERE request_id = ?', (ride_id, request_id))
            conn.commit()
            conn.close()
            user = await context.bot.get_chat(user_id)
            await notify_driver_of_passenger(context, driver_id, user, p_date_iso, p_time_str, p_lat, p_lon)
            matched = True
            break

    if not matched:
        await notify_passenger_no_ride(context, user_id)

# === Управление поездками и запросами ===
async def my_rides(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect('users.db')
    rides = conn.execute('''
        SELECT ride_id, date, time, seats FROM driver_rides
        WHERE user_id = ? ORDER BY date, time
    ''', (user_id,)).fetchall()
    conn.close()

    if not rides:
        await update.message.reply_text("У вас нет активных поездок.")
        return

    for ride_id, date, time, seats in rides:
        d_str = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y")
        keyboard = [[
            InlineKeyboardButton("Удалить", callback_data=f"del_ride_{ride_id}")
        ]]
        await update.message.reply_text(
            f"Поездка: {d_str} в {time}, мест: {seats}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def my_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect('users.db')
    requests = conn.execute('''
        SELECT request_id, date, time FROM passenger_requests
        WHERE user_id = ? ORDER BY date, time
    ''', (user_id,)).fetchall()
    conn.close()

    if not requests:
        await update.message.reply_text("У вас нет активных запросов.")
        return

    for req_id, date, time in requests:
        d_str = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y")
        keyboard = [[
            InlineKeyboardButton("Удалить", callback_data=f"del_req_{req_id}")
        ]]
        await update.message.reply_text(
            f"Запрос: {d_str} в {time}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    conn = sqlite3.connect('users.db')
    if data.startswith("del_ride_"):
        ride_id = int(data.split("_")[2])
        conn.execute("DELETE FROM driver_rides WHERE ride_id = ?", (ride_id,))
        await query.edit_message_text("✅ Поездка удалена.")
    elif data.startswith("del_req_"):
        req_id = int(data.split("_")[2])
        conn.execute("DELETE FROM passenger_requests WHERE request_id = ?", (req_id,))
        await query.edit_message_text("✅ Запрос удалён.")
    conn.commit()
    conn.close()

# === Основная функция ===
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("driver", driver))
    app.add_handler(CommandHandler("passenger", passenger))
    app.add_handler(CommandHandler("new_ride", new_ride))
    app.add_handler(CommandHandler("find_ride", find_ride))
    app.add_handler(CommandHandler("my_rides", my_rides))
    app.add_handler(CommandHandler("my_requests", my_requests))

    app.add_handler(CallbackQueryHandler(handle_date_driver, pattern="^date_driver_"))
    app.add_handler(CallbackQueryHandler(handle_time_driver, pattern="^time_driver_"))
    app.add_handler(CallbackQueryHandler(handle_date_passenger, pattern="^date_passenger_"))
    app.add_handler(CallbackQueryHandler(handle_time_passenger, pattern="^time_passenger_"))
    app.add_handler(CallbackQueryHandler(handle_delete, pattern="^del_"))

    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_seats))

    full_webhook_url = f"{WEBHOOK_URL}/{TOKEN}"
    logging.info(f"📡 Webhook: {full_webhook_url}")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=full_webhook_url,
        url_path=TOKEN
    )

if __name__ == '__main__':
    main()