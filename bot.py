import os
import logging
import sqlite3
import re
import requests
from datetime import datetime, timedelta
from urllib.parse import quote
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardRemove, MenuButtonCommands, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from geopy.distance import geodesic
import polyline

# === Настройка ===
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ORS_API_KEY = os.getenv("ORS_API_KEY")

# Координаты склада: [долгота, широта]
WAREHOUSE_COORDS = [73.17325327166235, 55.001957853274014]  # ЗАМЕНИ НА СВОИ!

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋 Я бот для поиска попутчиков на склад.\n\n"
        "Выберите роль:\n"
        "→ /driver — если вы водитель\n"
        "→ /passenger — если вы пассажир\n\n"
        "После выбора роли используйте:\n"
        "→ /new_ride — создать поездку (водитель)\n"
        "→ /find_ride — найти поездку (пассажир)"
    )

if not TOKEN or not WEBHOOK_URL:
    raise RuntimeError("❌ TELEGRAM_TOKEN и WEBHOOK_URL обязательны!")

# === Состояния (через user_data флаги) ===
STATE_AWAITING = "awaiting"
STATE_ROUTE_CONFIRM = "route_confirm"
STATE_ADD_WAYPOINTS = "add_waypoints"

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
            waypoints TEXT,  -- JSON строка: [[lat,lon], ...]
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            seats INTEGER NOT NULL,
            confirmed BOOLEAN DEFAULT 0,
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
            notified BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            match_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ride_id INTEGER NOT NULL,
            request_id INTEGER NOT NULL,
            driver_approved BOOLEAN DEFAULT 0,
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

def cleanup_old_requests():
    """Удаляет запросы пассажиров, если прошло >2 часов с указанного времени"""
    conn = sqlite3.connect('users.db')
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute('''
        DELETE FROM passenger_requests
        WHERE datetime(date || " " || time, "+2 hours") < ?
    ''', (now,))
    conn.commit()
    conn.close()

# === OpenRouteService ===
def get_route_polyline(start_lat, start_lon, waypoints, api_key, warehouse_coords):
    coords = [[start_lon, start_lat]]
    if waypoints:
        for lat, lon in waypoints:
            coords.append([lon, lat])
    coords.append(warehouse_coords)
    
    url = "https://api.openrouteservice.org/v2/directions/driving-car"
    headers = {'Authorization': api_key, 'Content-Type': 'application/json'}
    body = {"coordinates": coords}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data['routes'][0]['geometry'], data['routes'][0]['summary']['distance']
    except Exception as e:
        logging.error(f"ORS route error: {e}")
    return None, 0

def get_static_map_url(start_lat, start_lon, waypoints, warehouse_coords, api_key):
    coords = [[start_lon, start_lat]]
    if waypoints:
        for lat, lon in waypoints:
            coords.append([lon, lat])
    coords.append(warehouse_coords)
    
    polyline_str = quote(polyline.encode([(lat, lon) for lon, lat in coords]))
    markers = f"{start_lon},{start_lat};{warehouse_coords[0]},{warehouse_coords[1]}"
    url = (
        f"https://api.openrouteservice.org/v1/maps/static?api_key={api_key}"
        f"&size=600x400&format=png&coordinates={polyline_str}"
        f"&markers={markers}&theme=light"
    )
    return url

def is_point_near_route(pass_lat, pass_lon, start_lat, start_lon, waypoints=None, max_dist_m=100):
    polyline_str, _ = get_route_polyline(start_lat, start_lon, waypoints, ORS_API_KEY, WAREHOUSE_COORDS)
    if not polyline_str:
        return False
    try:
        coords = polyline.decode(polyline_str)
        for lat, lon in coords:
            if geodesic((pass_lat, pass_lon), (lat, lon)).meters <= max_dist_m:
                return True
    except Exception as e:
        logging.error(f"Polyline decode error: {e}")
    return False

# === Вспомогательные функции ===
async def set_bot_commands(application):
    commands = [
        BotCommand("start", "Запустить бота"),
        BotCommand("driver", "Я водитель"),
        BotCommand("passenger", "Я пассажир"),
        BotCommand("new_ride", "Создать поездку"),
        BotCommand("find_ride", "Найти поездку"),
        BotCommand("my_rides", "Мои поездки"),
        BotCommand("my_requests", "Мои запросы"),
    ]
    await application.bot.set_my_commands(commands)
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

# === Водитель: дата → час → минуты → гео → маршрут ===
async def new_ride(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... проверка роли и лимита (аналогично предыдущей версии) ...
    today = datetime.now().date()
    buttons = [[InlineKeyboardButton((today + timedelta(days=i)).strftime("%d.%m"),
                                     callback_data=f"date_{(today + timedelta(days=i)).isoformat()}")]
               for i in range(7)]
    await update.message.reply_text("📅 Выберите дату:", reply_markup=InlineKeyboardMarkup(buttons))

async def handle_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['date'] = query.data.split("_")[1]
    # Часы 0–23
    buttons = [[InlineKeyboardButton(f"{h:02d}", callback_data=f"hour_{h}") for h in range(i, min(i+6, 24))]
               for i in range(0, 24, 6)]
    await query.edit_message_text("🕒 Выберите час:", reply_markup=InlineKeyboardMarkup(buttons))

async def handle_hour(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['hour'] = query.data.split("_")[1]
    hour = int(context.user_data['hour'])
    # Минуты с шагом 5
    minutes = [f"{m:02d}" for m in range(0, 60, 5)]
    buttons = [[InlineKeyboardButton(m, callback_data=f"minute_{m}") for m in minutes[i:i+5]]
               for i in range(0, len(minutes), 5)]
    await query.edit_message_text(f"🕒 {hour}:__ — выберите минуты:", reply_markup=InlineKeyboardMarkup(buttons))

async def handle_minute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['minute'] = query.data.split("_")[1]
    context.user_data[STATE_AWAITING] = 'driver_location'
    await query.edit_message_text("📍 Отправьте точку отправления:")

async def handle_driver_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get(STATE_AWAITING) != 'driver_location':
        return
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    context.user_data.update({'lat': lat, 'lon': lon, 'waypoints': []})
    await show_route_preview(update, context)

async def show_route_preview(update, context):
    lat = context.user_data['lat']
    lon = context.user_data['lon']
    waypoints = context.user_data.get('waypoints', [])
    
    polyline_str, distance = get_route_polyline(lat, lon, waypoints, ORS_API_KEY, WAREHOUSE_COORDS)
    if not polyline_str:
        await update.message.reply_text("❌ Не удалось построить маршрут.")
        return

    map_url = get_static_map_url(lat, lon, waypoints, WAREHOUSE_COORDS, ORS_API_KEY)
    time_str = f"{context.user_data['hour']}:{context.user_data['minute']}"
    date_str = context.user_data['date']

    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить маршрут", callback_data="confirm_route")],
        [InlineKeyboardButton("➕ Добавить точку", callback_data="add_waypoint")]
    ]
    await update.message.reply_photo(
        photo=map_url,
        caption=f"Маршрут построен!\nДата: {date_str}\nВремя: {time_str}\nРасстояние: {distance/1000:.1f} км",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data[STATE_AWAITING] = None
    context.user_data[STATE_ROUTE_CONFIRM] = True

async def handle_route_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_route":
        # Сохраняем поездку
        user_id = query.from_user.id
        date_iso = context.user_data['date']
        time_str = f"{context.user_data['hour']}:{context.user_data['minute']}"
        lat = context.user_data['lat']
        lon = context.user_data['lon']
        waypoints = str(context.user_data.get('waypoints', []))  # JSON-like string

        conn = sqlite3.connect('users.db')
        conn.execute('''
            INSERT INTO driver_rides (user_id, departure_lat, departure_lon, waypoints, date, time, seats, confirmed)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        ''', (user_id, lat, lon, waypoints, date_iso, time_str, 2))  # seats=2 по умолчанию
        conn.commit()
        conn.close()
        await query.edit_message_caption("✅ Поездка создана и подтверждена!")
        # Запуск проверки пассажиров...
    elif query.data == "add_waypoint":
        context.user_data[STATE_ADD_WAYPOINTS] = True
        await query.edit_message_caption("📍 Отправьте дополнительную точку маршрута:")

# === Пассажир: аналогично с выбором времени → гео → сохранение ===
async def find_ride(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... аналогично водителю: дата → час → минуты ...
    pass  # реализуется по аналогии

async def handle_passenger_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Сохраняем запрос
    user_id = update.effective_user.id
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    # ... получаем date/time из user_data ...

    conn = sqlite3.connect('users.db')
    conn.execute('''
        INSERT INTO passenger_requests (user_id, lat, lon, date, time)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, lat, lon, date_iso, time_str))
    req_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    # Ищем активные поездки
    matched = False
    conn = sqlite3.connect('users.db')
    rides = conn.execute('''
        SELECT ride_id, user_id, departure_lat, departure_lon, waypoints
        FROM driver_rides
        WHERE date = ? AND time BETWEEN ? AND ? AND confirmed = 1
    ''', (date_iso, min_t, max_t)).fetchall()
    conn.close()

    for ride_id, driver_id, d_lat, d_lon, wp_str in rides:
        waypoints = eval(wp_str) if wp_str else []
        if is_point_near_route(lat, lon, d_lat, d_lon, waypoints):
            # Создаём матч
            conn = sqlite3.connect('users.db')
            conn.execute('INSERT INTO matches (ride_id, request_id) VALUES (?, ?)', (ride_id, req_id))
            match_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            conn.close()

            # Уведомляем водителя
            passenger = await context.bot.get_chat(user_id)
            keyboard = [[InlineKeyboardButton("✅ Взять", callback_data=f"approve_{match_id}"),
                         InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{match_id}")]]
            await context.bot.send_message(
                driver_id,
                f"👤 Пассажир рядом с вашим маршрутом!\nКоординаты: {lat:.4f}, {lon:.4f}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            matched = True
            break

    if matched:
        await update.message.reply_text("✅ Найдена поездка! Ожидайте подтверждения от водителя.")
    else:
        await update.message.reply_text("❌ Пока нет подходящих поездок.")

# === Обработка согласия водителя ===
async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    match_id = int(query.data.split("_")[1])
    driver_id = query.from_user.id

    conn = sqlite3.connect('users.db')
    conn.execute('UPDATE matches SET driver_approved = 1 WHERE match_id = ?', (match_id,))
    # Получаем данные пассажира
    data = conn.execute('''
        SELECT pr.user_id, dr.user_id
        FROM matches m
        JOIN passenger_requests pr ON m.request_id = pr.request_id
        JOIN driver_rides dr ON m.ride_id = dr.ride_id
        WHERE m.match_id = ?
    ''', (match_id,)).fetchone()
    conn.commit()
    conn.close()

    if data:
        passenger_id, driver_id = data
        # Отправляем контакты
        try:
            passenger = await context.bot.get_chat(passenger_id)
            driver = await context.bot.get_chat(driver_id)
            await context.bot.send_message(driver_id, f"Контакт пассажира: @{passenger.username or 'недоступен'}")
            await context.bot.send_message(passenger_id, f"Контакт водителя: @{driver.username or 'недоступен'}")
            await query.edit_message_text("✅ Контакты отправлены!")
        except Exception as e:
            logging.error(f"Ошибка отправки контактов: {e}")

# === Основная функция ===
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("driver", driver))
    app.add_handler(CommandHandler("passenger", passenger))
    app.add_handler(CommandHandler("new_ride", new_ride))
    app.add_handler(CommandHandler("find_ride", find_ride))
    # ... остальные команды ...

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_date, pattern="^date_"))
    app.add_handler(CallbackQueryHandler(handle_hour, pattern="^hour_"))
    app.add_handler(CallbackQueryHandler(handle_minute, pattern="^minute_"))
    app.add_handler(CallbackQueryHandler(handle_route_action, pattern="^(confirm_route|add_waypoint)$"))
    app.add_handler(CallbackQueryHandler(handle_approval, pattern="^(approve|reject)_"))

    # Геолокация
    app.add_handler(MessageHandler(filters.LOCATION, handle_driver_location))
    app.add_handler(MessageHandler(filters.LOCATION, handle_passenger_location))

    # Установка меню
    import asyncio
    asyncio.run(set_bot_commands(app))

    # Webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
        url_path=TOKEN
    )

if __name__ == '__main__':
    main()