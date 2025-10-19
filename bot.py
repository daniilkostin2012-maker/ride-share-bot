# bot.py
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)
from geopy.distance import geodesic
import polyline
import requests # Добавлено для API ORS

# --- Настройки ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") # Получить из переменных окружения на Render
ORS_API_KEY = os.environ.get("ORS_API_KEY") # Получить из переменных окружения на Render
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///carpool_bot.db") # Пример для PostgreSQL на Render
YOUR_ADMIN_USER_ID = 6821825839 # ВСТАВЬТЕ СВОЙ USER ID ТУТ

# --- Скрытие предупреждений PTB для CallbackQueryHandler в ConversationHandler ---
from warnings import filterwarnings
from telegram.warnings import PTBUserWarning
filterwarnings(action="ignore", message=r".*CallbackQueryHandler", category=PTBUserWarning)
# ---

# --- Скрытие логов httpx (по желанию, для уменьшения шума)
logging.getLogger("httpx").setLevel(logging.WARNING)
# ---

# --- Фиксированные координаты склада ---
# Адрес: Россия, город Омск, ул. Айвазовского, 33
WAREHOUSE_LAT = 55.001957853274014
WAREHOUSE_LON = 73.17325327166235
WAREHOUSE_POINT = [WAREHOUSE_LAT, WAREHOUSE_LON] # [lat, lon]

# --- Состояния для ConversationHandler ---
ASK_ROLE, ASK_TRIP_TYPE_DRIVER, ASK_DATE_DRIVER, ASK_HOUR_DRIVER, ASK_MINUTE_DRIVER, ASK_LOCATION_DRIVER, ASK_SEATS, ASK_TRIP_TYPE_PASSENGER, ASK_DATE_PASSENGER, ASK_HOUR_PASSENGER, ASK_MINUTE_PASSENGER, ASK_LOCATION_PASSENGER, ASK_COMMENT_PASSENGER, ASK_SEATS_PASSENGER, CONFIRM_BUG_REPORT = range(15)

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Инициализация базы данных ---
def init_db():
    """Создает таблицы в базе данных, если они не существуют."""
    conn = sqlite3.connect('carpool_bot.db') # Используем локальный файл для примера
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            role TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id INTEGER,
            trip_type TEXT, -- 'to_warehouse', 'from_warehouse'
            start_time TEXT, -- ISO format datetime
            start_point TEXT, -- JSON string [lat, lon]
            end_point TEXT, -- JSON string [lat, lon]
            polyline TEXT, -- Encoded polyline string
            available_seats INTEGER,
            FOREIGN KEY(driver_id) REFERENCES users(user_id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            passenger_id INTEGER,
            trip_type TEXT, -- 'to_warehouse', 'from_warehouse'
            request_time TEXT, -- ISO format datetime
            pickup_point TEXT, -- JSON string [lat, lon]
            pickup_comment TEXT,
            required_seats INTEGER,
            FOREIGN KEY(passenger_id) REFERENCES users(user_id)
        )
    ''')
    conn.commit()
    conn.close()

# --- Вспомогательные функции ---
def encode_coords_to_polyline(coords_list):
    """Кодирует список координат в строку polyline."""
    # coords_list: [[lat1, lon1], [lat2, lon2], ...]
    return polyline.encode(coords_list)

def decode_polyline_to_coords(polyline_str):
    """Декодирует строку polyline в список координат."""
    # polyline_str: "u{~vFvyys@fZp|@~@vA"
    return polyline.decode(polyline_str)

def get_ors_route(start_coords, end_coords):
    """Получает маршрут по дорогам через OpenRouteService (ORS) API."""
    # ORS API Documentation: https://openrouteservice.org/dev/#/api-docs/v2/directions/{profile}/post
    # Profile: driving-car, cycling-regular, foot-walking, etc.
    url = "https://api.openrouteservice.org/v2/directions/driving-car"

    headers = {
        "Authorization": f"Bearer {ORS_API_KEY}",
        "Content-Type": "application/json",
    }

    # Формат тела запроса для ORS: "coordinates": [[lon, lat], [lon, lat], ...]
    payload = {
        "coordinates": [
            [start_coords[1], start_coords[0]],  # [lon, lat]
            [end_coords[1], end_coords[0]]       # [lon, lat]
        ],
        "format": "json", # ИСПРАВЛЕНО: Запрашиваем формат JSON, который возвращает 'routes'
        # "instructions": False, # Опционально: отключить инструкции для уменьшения ответа
        # "geometry_simplify": True # Опционально: упростить геометрию
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status() # Вызовет исключение для кодов ошибок HTTP (4xx, 5xx)
        data = response.json()

        # ИСПРАВЛЕНО: Проверяем наличие 'routes' и первого маршрута
        if 'routes' in data and len(data['routes']) > 0:
            first_route = data['routes'][0]
            # ИСПРАВЛЕНО: Получаем геометрию из 'geometry' первого маршрута
            polyline_str = first_route.get('geometry')
            if polyline_str:
                # polyline.decode ожидает формат polyline6, что соответствует формату JSON
                coords = polyline.decode(polyline_str) # coords = [[lat, lon], [lat, lon], ...]
                # polyline_str уже закодирована правильно, возвращаем её
                return polyline_str
            else:
                logger.error(f"ORS API returned route but no geometry: {data}")
                return None
        else:
            logger.error(f"ORS API returned no routes: {data}")
            return None
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error from ORS API: {e}")
        logger.error(f"Response content: {response.text}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error to ORS API: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error getting ORS route: {e}")
        return None # Возвращаем None в случае ошибки


def is_point_near_polyline(point, polyline_coords, tolerance_m=300):
    """Проверяет, находится ли точка в пределах tolerance_m метров от линии маршрута."""
    point_tuple = (point[0], point[1])
    for i in range(len(polyline_coords) - 1):
        start = (polyline_coords[i][0], polyline_coords[i][1])
        end = (polyline_coords[i+1][0], polyline_coords[i+1][1])
        # Приблизительный расчет расстояния от точки до отрезка
        dist_to_segment = geodesic(point_tuple, start).meters
        if dist_to_segment <= tolerance_m:
            return True
        dist_to_segment = geodesic(point_tuple, end).meters
        if dist_to_segment <= tolerance_m:
            return True
        # Более точный расчет (упрощенный, можно улучшить)
        dist_to_segment = geodesic(point_tuple, ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)).meters
        if dist_to_segment <= tolerance_m:
            return True
    return False

# --- Основные обработчики ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет приветственное сообщение и предлагает выбрать роль."""
    keyboard = [
        [InlineKeyboardButton("Я водитель", callback_data='role_driver')],
        [InlineKeyboardButton("Я пассажир", callback_data='role_passenger')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Привет! Выберите вашу роль:', reply_markup=reply_markup)

# --- Новые команды ---
async def driver_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню управления для водителя."""
    user_id = update.effective_user.id
    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()

    if result and result[0] == 'driver':
        keyboard = [
            [InlineKeyboardButton("Создать поездку", callback_data='create_trip_driver')],
            [InlineKeyboardButton("Управление маршрутами", callback_data='manage_trips_driver')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Меню водителя:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("Пожалуйста, сначала выберите роль 'водитель' с помощью /start.")

async def passenger_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню управления для пассажира."""
    user_id = update.effective_user.id
    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()

    if result and result[0] == 'passenger':
        keyboard = [
            [InlineKeyboardButton("Создать запрос", callback_data='create_request_passenger')],
            [InlineKeyboardButton("Управление запросами", callback_data='manage_requests_passenger')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Меню пассажира:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("Пожалуйста, сначала выберите роль 'пассажир' с помощью /start.")

# --- Старые обработчики (без изменений, кроме добавления команды в ConversationHandler) ---
async def handle_role_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор роли пользователем."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    role = query.data.split('_')[1]

    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, role) VALUES (?, ?)", (user_id, role))
    conn.commit()
    conn.close()

    await query.edit_message_text(f"Вы выбрали роль: {role.capitalize()}")
    context.user_data['role'] = role

    if role == 'driver':
        keyboard = [
            [InlineKeyboardButton("Создать поездку", callback_data='create_trip_driver')],
            [InlineKeyboardButton("Управление маршрутами", callback_data='manage_trips_driver')]
        ]
    else: # passenger
        keyboard = [
            [InlineKeyboardButton("Создать запрос", callback_data='create_request_passenger')],
            [InlineKeyboardButton("Управление запросами", callback_data='manage_requests_passenger')]
        ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def create_trip_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает процесс создания поездки для водителя."""
    keyboard = [
        [InlineKeyboardButton("До склада", callback_data='type_to_warehouse')],
        [InlineKeyboardButton("От склада", callback_data='type_from_warehouse')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.message.reply_text("Выберите тип поездки:", reply_markup=reply_markup)
    return ASK_TRIP_TYPE_DRIVER

async def ask_date_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает дату поездки у водителя с помощью кнопок (компактно)."""
    context.user_data['trip_type'] = update.callback_query.data.split('_')[1]

    keyboard = []
    today = datetime.today().date()
    row = []
    for i in range(7):
        day = today + timedelta(days=i)
        row.append(InlineKeyboardButton(day.strftime('%d.%m'), callback_data=f'date_{day.strftime("%Y-%m-%d")}') )
        if len(row) == 3: # 3 кнопки в строке
            keyboard.append(row)
            row = []
    if row: # Добавить оставшиеся кнопки, если есть
        keyboard.append(row)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.message.reply_text("Выберите дату поездки:", reply_markup=reply_markup)
    return ASK_DATE_DRIVER

async def ask_hour_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает час поездки у водителя с помощью кнопок (компактно)."""
    selected_date = update.callback_query.data.split('_')[1]
    context.user_data['selected_date'] = selected_date

    keyboard = []
    row = []
    for hour in range(24):
        row.append(InlineKeyboardButton(f'{hour:02d}:00', callback_data=f'hour_{hour:02d}') )
        if len(row) == 4: # 4 кнопки в строке
            keyboard.append(row)
            row = []
    if row: # Добавить оставшиеся кнопки, если есть
        keyboard.append(row)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.message.reply_text("Выберите час поездки:", reply_markup=reply_markup)
    return ASK_HOUR_DRIVER

async def ask_minute_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает минуты поездки у водителя с помощью кнопок (компактно)."""
    selected_hour = update.callback_query.data.split('_')[1]
    context.user_data['selected_hour'] = selected_hour

    keyboard = []
    row = []
    for minute in range(0, 60, 5): # Каждые 5 минут
        row.append(InlineKeyboardButton(f'{selected_hour}:{minute:02d}', callback_data=f'min_{minute:02d}') )
        if len(row) == 4: # 4 кнопки в строке
            keyboard.append(row)
            row = []
    if row: # Добавить оставшиеся кнопки, если есть
        keyboard.append(row)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.message.reply_text("Выберите минуты поездки:", reply_markup=reply_markup)
    return ASK_MINUTE_DRIVER

async def ask_location_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает геопозицию у водителя."""
    selected_minute = update.callback_query.data.split('_')[1]
    selected_time_str = f"{context.user_data['selected_date']} {context.user_data['selected_hour']}:{selected_minute}"
    context.user_data['trip_time'] = selected_time_str

    trip_type = context.user_data['trip_type']
    if trip_type == 'to_warehouse':
        # ИСПРАВЛЕНО: Правильное сообщение для "до склада"
        await update.callback_query.message.reply_text("Отправьте геопозицию вашего дома (точка отправления):")
    else: # from_warehouse
        await update.callback_query.message.reply_text("Отправьте геопозицию места назначения (точка прибытия):")
    return ASK_LOCATION_DRIVER

async def ask_seats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает количество мест у водителя."""
    location = update.message.location
    context.user_data['location'] = [location.latitude, location.longitude]
    await update.message.reply_text("Сколько мест доступно в вашей машине (1-7)?")
    return ASK_SEATS

async def save_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет поездку водителя в базу данных."""
    try:
        seats = int(update.message.text)
        if not 1 <= seats <= 7:
            await update.message.reply_text("Количество мест должно быть от 1 до 7.")
            return ASK_SEATS
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число.")
        return ASK_SEATS

    context.user_data['seats'] = seats

    user_id = update.effective_user.id
    trip_type = context.user_data['trip_type']
    trip_time = context.user_data['trip_time']
    start_point = context.user_data['location'] # [lat, lon]
    # Используем фиксированные координаты склада
    end_point = WAREHOUSE_POINT if trip_type == 'to_warehouse' else start_point
    start_point = start_point if trip_type == 'to_warehouse' else WAREHOUSE_POINT

    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()

    # Проверка лимита поездок
    c.execute("SELECT COUNT(*) FROM trips WHERE driver_id = ?", (user_id,))
    trip_count = c.fetchone()[0]
    if trip_count >= 5:
        conn.close()
        await update.message.reply_text("Вы достигли лимита в 5 активных поездок.")
        return ConversationHandler.END

    # Построение маршрута по дорогам через ORS
    polyline_str = get_ors_route(start_point, end_point)
    if not polyline_str:
        # ИСПРАВЛЕНО: Вывод сообщения пользователю и логирование ошибки
        logger.error(f"Failed to get ORS route for user {user_id} at {datetime.now()}. Check ORS_API_KEY and service status.")
        # Спрашиваем пользователя, хочет ли он отправить багрепорт
        keyboard = [
            [InlineKeyboardButton("Да", callback_data='bug_report_yes')],
            [InlineKeyboardButton("Нет", callback_data='bug_report_no')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Не удалось построить маршрут. Отправить багрепорт?", reply_markup=reply_markup)
        return CONFIRM_BUG_REPORT # Переход к новому состоянию

    # ХОРОШИЙ СПОСОБ (используем json):
    import json
    start_point_str = json.dumps(start_point)
    end_point_str = json.dumps(end_point)

    c.execute("""
        INSERT INTO trips (driver_id, trip_type, start_time, start_point, end_point, polyline, available_seats)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, trip_type, trip_time, start_point_str, end_point_str, polyline_str, seats))
    trip_id = c.lastrowid
    conn.commit()
    conn.close()

    await update.message.reply_text(f"Поездка создана! Тип: {trip_type}, Время: {trip_time}, Мест: {seats}/7")

    # Проверка совпадений с запросами пассажиров
    await check_matches_for_new_trip(update, context, trip_id)

    return ConversationHandler.END

async def check_matches_for_new_trip(update, context, trip_id):
    """Проверяет совпадения новой поездки водителя с существующими запросами пассажиров."""
    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()
    # ИСПРАВЛЕНО: Явно указываем столбцы, соответствующие переменным ниже
    c.execute("SELECT driver_id, trip_type, start_time, start_point, end_point, polyline, available_seats FROM trips WHERE id = ?", (trip_id,))
    trip = c.fetchone()
    if not trip:
        conn.close()
        return

    # ИСПРАВЛЕНО: Теперь ожидается 7 значений, соответствующих SELECT выше
    driver_id, trip_type, trip_time_str, start_point_str, end_point_str, polyline_str, available_seats = trip
    trip_time = datetime.strptime(trip_time_str, '%Y-%m-%d %H:%M')
    polyline_coords = decode_polyline_to_coords(polyline_str)

    # Найти запросы в нужное время и тип (сначала проверяем время)
    c.execute("""
        SELECT * FROM requests
        WHERE trip_type = ? AND request_time BETWEEN ? AND ?
    """, (trip_type, (trip_time - timedelta(hours=1)).isoformat(' '), (trip_time + timedelta(hours=1)).isoformat(' ')))
    matching_requests = c.fetchall()

    for req in matching_requests:
        req_id, passenger_id, req_type, req_time_str, pickup_point_str, _, req_seats = req
        # ХОРОШИЙ СПОСОБ (используем json):
        import json
        try:
            pickup_point = json.loads(pickup_point_str)
            if not isinstance(pickup_point, list) or len(pickup_point) != 2:
                 raise ValueError("Неверный формат точки")
        except (json.JSONDecodeError, ValueError):
            logger.error(f"Ошибка парсинга pickup_point для req_id {req_id}: {pickup_point_str}")
            continue # Пропустить некорректную запись

        # Теперь проверяем дистанцию, так как время совпадает
        if req_seats <= available_seats and is_point_near_polyline(pickup_point, polyline_coords):
            # Найти имя пассажира
            passenger_name = (await context.bot.get_chat(passenger_id)).first_name
            # Отправить уведомление водителю
            keyboard = [
                [InlineKeyboardButton("Принять", callback_data=f'accept_{req_id}_{trip_id}'),
                 InlineKeyboardButton("Отклонить", callback_data=f'reject_{req_id}')],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=driver_id,
                text=f"Новый пассажир!\nИмя: {passenger_name}\nВремя запроса: {req_time_str}\nТочка посадки: {pickup_point} (проверьте комментарий)\nМест нужно: {req_seats}\nБрать пассажира?",
                reply_markup=reply_markup
            )
            # Уведомить пассажира о совпадении
            await context.bot.send_message(
                chat_id=passenger_id,
                text="Найден водитель! Ожидаем подтверждения..."
            )

    conn.close()

# --- Обработчики для пассажира ---
async def create_request_passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает процесс создания запроса для пассажира."""
    keyboard = [
        [InlineKeyboardButton("До склада", callback_data='type_to_warehouse')],
        [InlineKeyboardButton("От склада", callback_data='type_from_warehouse')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.message.reply_text("Выберите тип поездки:", reply_markup=reply_markup)
    context.user_data['creating_request'] = True # Флаг для определения контекста callback'а
    return ASK_TRIP_TYPE_PASSENGER

async def ask_date_passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает дату запроса у пассажира с помощью кнопок (компактно)."""
    context.user_data['trip_type'] = update.callback_query.data.split('_')[1]

    keyboard = []
    today = datetime.today().date()
    row = []
    for i in range(7):
        day = today + timedelta(days=i)
        row.append(InlineKeyboardButton(day.strftime('%d.%m'), callback_data=f'date_{day.strftime("%Y-%m-%d")}') )
        if len(row) == 3: # 3 кнопки в строке
            keyboard.append(row)
            row = []
    if row: # Добавить оставшиеся кнопки, если есть
        keyboard.append(row)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.message.reply_text("Выберите дату поездки:", reply_markup=reply_markup)
    return ASK_DATE_PASSENGER

async def ask_hour_passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает час запроса у пассажира с помощью кнопок (компактно)."""
    selected_date = update.callback_query.data.split('_')[1]
    context.user_data['selected_date'] = selected_date

    keyboard = []
    row = []
    for hour in range(24):
        row.append(InlineKeyboardButton(f'{hour:02d}:00', callback_data=f'hour_{hour:02d}') )
        if len(row) == 4: # 4 кнопки в строке
            keyboard.append(row)
            row = []
    if row: # Добавить оставшиеся кнопки, если есть
        keyboard.append(row)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.message.reply_text("Выберите час поездки:", reply_markup=reply_markup)
    return ASK_HOUR_PASSENGER

async def ask_minute_passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает минуты запроса у пассажира с помощью кнопок (компактно)."""
    selected_hour = update.callback_query.data.split('_')[1]
    context.user_data['selected_hour'] = selected_hour

    keyboard = []
    row = []
    for minute in range(0, 60, 5): # Каждые 5 минут
        row.append(InlineKeyboardButton(f'{selected_hour}:{minute:02d}', callback_data=f'min_{minute:02d}') )
        if len(row) == 4: # 4 кнопки в строке
            keyboard.append(row)
            row = []
    if row: # Добавить оставшиеся кнопки, если есть
        keyboard.append(row)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.message.reply_text("Выберите минуты поездки:", reply_markup=reply_markup)
    return ASK_MINUTE_PASSENGER

async def ask_location_passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает геопозицию у пассажира."""
    selected_minute = update.callback_query.data.split('_')[1]
    selected_time_str = f"{context.user_data['selected_date']} {context.user_data['selected_hour']}:{selected_minute}"
    context.user_data['request_time'] = selected_time_str

    await update.callback_query.message.reply_text("Отправьте геопозицию вашей точки посадки:")
    return ASK_LOCATION_PASSENGER

async def ask_comment_passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает комментарий к точке посадки."""
    location = update.message.location
    context.user_data['location'] = [location.latitude, location.longitude]
    await update.message.reply_text("Введите короткое название или комментарий к точке посадки (например, 'Остановка Малунцева'):")
    return ASK_COMMENT_PASSENGER

async def ask_seats_passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает количество мест, необходимых пассажиру."""
    context.user_data['comment'] = update.message.text
    await update.message.reply_text("Сколько мест вам нужно (1-7)?")
    return ASK_SEATS_PASSENGER

async def save_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет запрос пассажира в базу данных."""
    try:
        seats = int(update.message.text)
        if not 1 <= seats <= 4: # Логично ограничить запрос не больше 4 мест
            await update.message.reply_text("Количество мест для запроса должно быть от 1 до 4.")
            return ASK_SEATS_PASSENGER
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число.")
        return ASK_SEATS_PASSENGER

    context.user_data['required_seats'] = seats

    user_id = update.effective_user.id
    trip_type = context.user_data['trip_type']
    request_time = context.user_data['request_time']
    pickup_point = context.user_data['location']
    comment = context.user_data['comment']
    req_seats = context.user_data['required_seats']

    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()

    # Проверка лимита запросов
    c.execute("SELECT COUNT(*) FROM requests WHERE passenger_id = ?", (user_id,))
    req_count = c.fetchone()[0]
    if req_count >= 5:
        conn.close()
        await update.message.reply_text("Вы достигли лимита в 5 активных запросов.")
        return ConversationHandler.END

    # ХОРОШИЙ СПОСОБ (используем json):
    import json
    pickup_point_str = json.dumps(pickup_point)

    c.execute("""
        INSERT INTO requests (passenger_id, trip_type, request_time, pickup_point, pickup_comment, required_seats)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, trip_type, request_time, pickup_point_str, comment, req_seats))
    req_id = c.lastrowid
    conn.commit()
    conn.close()

    await update.message.reply_text(f"Запрос создан! Тип: {trip_type}, Время: {request_time}, Мест нужно: {req_seats}")

    # Проверка совпадений с поездками водителей
    await check_matches_for_new_request(update, context, req_id)

    return ConversationHandler.END

async def check_matches_for_new_request(update, context, req_id):
    """Проверяет совпадения нового запроса пассажира с существующими поездками водителей."""
    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()
    c.execute("SELECT * FROM requests WHERE id = ?", (req_id,))
    request = c.fetchone()
    if not request:
        conn.close()
        return

    req_id, passenger_id, req_type, req_time_str, pickup_point_str, comment, req_seats = request
    req_time = datetime.strptime(req_time_str, '%Y-%m-%d %H:%M')
    # ХОРОШИЙ СПОСОБ (используем json):
    import json
    try:
        pickup_point = json.loads(pickup_point_str)
        if not isinstance(pickup_point, list) or len(pickup_point) != 2:
             raise ValueError("Неверный формат точки")
    except (json.JSONDecodeError, ValueError):
        logger.error(f"Ошибка парсинга pickup_point для req_id {req_id}: {pickup_point_str}")
        conn.close()
        return # Пропустить некорректную запись

    # Найти поездки в нужное время и тип (сначала проверяем время)
    # ИСПРАВЛЕНО: Явно указываем столбцы, соответствующие переменным ниже (БЕЗ trip_id)
    c.execute("""
        SELECT driver_id, trip_type, start_time, start_point, end_point, polyline, available_seats FROM trips
        WHERE trip_type = ? AND start_time BETWEEN ? AND ?
    """, (req_type, (req_time - timedelta(hours=1)).isoformat(' '), (req_time + timedelta(hours=1)).isoformat(' ')))
    matching_trips = c.fetchall()

    found_match = False
    for trip in matching_trips:
        # ИСПРАВЛЕНО: Теперь ожидается 7 значений, соответствующих SELECT выше (trip_id НЕТ)
        driver_id, trip_type, trip_time_str, start_point_str, end_point_str, polyline_str, available_seats = trip
        # Теперь проверяем дистанцию, так как время совпадает
        if req_seats <= available_seats and is_point_near_polyline(pickup_point, decode_polyline_to_coords(polyline_str)):
            # Найти имя пассажира
            passenger_name = (await context.bot.get_chat(passenger_id)).first_name
            # Отправить уведомление водителю
            keyboard = [
                [InlineKeyboardButton("Принять", callback_data=f'accept_{req_id}_{trip_id}'), # trip_id берется из внешней области видимости функции
                 InlineKeyboardButton("Отклонить", callback_data=f'reject_{req_id}')],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=driver_id,
                text=f"Новый пассажир!\nИмя: {passenger_name}\nВремя запроса: {req_time_str}\nТочка посадки: {comment}\nМест нужно: {req_seats}\nБрать пассажира?",
                reply_markup=reply_markup
            )
            found_match = True

    if found_match:
         await context.bot.send_message(
             chat_id=passenger_id,
             text="Найден водитель! Ожидаем подтверждения..."
         )
    else:
        await context.bot.send_message(
             chat_id=passenger_id,
             text="Совпадений с поездками водителей пока нет. Ваш запрос активен."
         )

    conn.close()

# --- Обработчики подтверждения/отказа ---
async def handle_accept_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает принятие или отказ водителем пассажира."""
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    action = data[0]
    req_id = int(data[1])

    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()
    c.execute("SELECT passenger_id, required_seats FROM requests WHERE id = ?", (req_id,))
    request_details = c.fetchone()
    if not request_details:
        await query.edit_message_text("Запрос не найден.")
        conn.close()
        return

    passenger_id, req_seats = request_details

    if action == 'accept':
        trip_id = int(data[2])
        c.execute("SELECT available_seats FROM trips WHERE id = ?", (trip_id,))
        available_seats = c.fetchone()[0]

        if req_seats <= available_seats:
            # Отправить контакт пассажира водителю
            await context.bot.send_contact(
                chat_id=query.from_user.id,
                phone_number=(await context.bot.get_chat(passenger_id)).username or "Нет username", # Telegram может не предоставить номер
                first_name=(await context.bot.get_chat(passenger_id)).first_name
            )
            await query.edit_message_text("Вы приняли пассажира!")

            # Задать вопрос о договоренности
            keyboard = [
                [InlineKeyboardButton("Да", callback_data=f'confirm_deal_{req_id}_{trip_id}_yes'),
                 InlineKeyboardButton("Нет", callback_data=f'confirm_deal_{req_id}_{trip_id}_no')],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="Договорились с пассажиром?",
                reply_markup=reply_markup
            )
        else:
            await query.edit_message_text("Недостаточно мест для этого пассажира.")
    elif action == 'reject':
        await query.edit_message_text("Вы отказались от пассажира.")
        await context.bot.send_message(
            chat_id=passenger_id,
            text="Вас отказались везти."
        )
        # Удаляем запрос, если он не нужен после отказа
        # c.execute("DELETE FROM requests WHERE id = ?", (req_id,))

    conn.commit()
    conn.close()

async def handle_deal_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает подтверждение договоренности и обновляет количество мест."""
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    if data[0] != 'confirm' or data[-1] not in ['yes', 'no']:
        return

    req_id = int(data[2])
    trip_id = int(data[3])
    confirmed = data[-1] == 'yes'

    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()

    if confirmed:
        # Получить количество мест, которые нужно отнять
        c.execute("SELECT required_seats FROM requests WHERE id = ?", (req_id,))
        seats_to_reduce = c.fetchone()[0]

        # Обновить количество доступных мест
        c.execute("""
            UPDATE trips
            SET available_seats = available_seats - ?
            WHERE id = ?
        """, (seats_to_reduce, trip_id))
        await query.edit_message_text("Договорились! Места обновлены.")
    else:
        await query.edit_message_text("Договориться не удалось.")

    conn.commit()
    conn.close()

# --- Управление маршрутами/запросами ---
async def manage_trips_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает водителю его активные поездки."""
    user_id = update.effective_user.id
    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()
    c.execute("SELECT id, trip_type, start_time, available_seats FROM trips WHERE driver_id = ?", (user_id,))
    trips = c.fetchall()
    conn.close()

    if not trips:
        await update.callback_query.message.reply_text("У вас нет активных поездок.")
        return

    message_text = "Ваши активные поездки:\n"
    keyboard = []
    for trip in trips:
        t_id, t_type, t_time, seats = trip
        message_text += f"- ID: {t_id}, Тип: {t_type}, Время: {t_time}, Мест: {seats}\n"
        keyboard.append([InlineKeyboardButton(f"Удалить поездку {t_id}", callback_data=f'delete_trip_{t_id}')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.message.reply_text(message_text, reply_markup=reply_markup)

async def manage_requests_passenger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает пассажиру его активные запросы."""
    user_id = update.effective_user.id
    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()
    c.execute("SELECT id, trip_type, request_time, pickup_comment, required_seats FROM requests WHERE passenger_id = ?", (user_id,))
    requests = c.fetchall()
    conn.close()

    if not requests:
        await update.callback_query.message.reply_text("У вас нет активных запросов.")
        return

    message_text = "Ваши активные запросы:\n"
    keyboard = []
    for req in requests:
        r_id, r_type, r_time, comment, seats = req
        message_text += f"- ID: {r_id}, Тип: {r_type}, Время: {r_time}, Коммент: {comment}, Мест: {seats}\n"
        keyboard.append([InlineKeyboardButton(f"Удалить запрос {r_id}", callback_data=f'delete_request_{r_id}')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.message.reply_text(message_text, reply_markup=reply_markup)

async def delete_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет поездку водителя."""
    query = update.callback_query
    trip_id = int(query.data.split('_')[2])
    user_id = query.from_user.id

    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()
    c.execute("DELETE FROM trips WHERE id = ? AND driver_id = ?", (trip_id, user_id))
    if c.rowcount > 0:
        conn.commit()
        await query.answer("Поездка удалена.")
    else:
        await query.answer("Поездка не найдена или вы не являетесь её владельцем.", show_alert=True)
    conn.close()

async def delete_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет запрос пассажира."""
    query = update.callback_query
    req_id = int(query.data.split('_')[2])
    user_id = query.from_user.id

    conn = sqlite3.connect('carpool_bot.db')
    c = conn.cursor()
    c.execute("DELETE FROM requests WHERE id = ? AND passenger_id = ?", (req_id, user_id))
    if c.rowcount > 0:
        conn.commit()
        await query.answer("Запрос удален.")
    else:
        await query.answer("Запрос не найден или вы не являетесь его владельцем.", show_alert=True)
    conn.close()

# --- Обработчик багрепорта ---
async def handle_bug_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ответ на вопрос о багрепорте."""
    query = update.callback_query
    await query.answer()
    choice = query.data.split('_')[2]

    if choice == 'yes':
        user_id = query.from_user.id
        user_name = query.from_user.full_name
        # Отправить сообщение разработчику
        await context.bot.send_message(
            chat_id=YOUR_ADMIN_USER_ID,
            text=f"Багрепорт от пользователя {user_name} (ID: {user_id}) - Не удалось построить маршрут. Проверьте API ключ ORS и логи."
        )
        await query.edit_message_text("Багрепорт отправлен. Спасибо!")
    else: # choice == 'no'
        await query.edit_message_text("Багрепорт не отправлен.")

    return ConversationHandler.END


# --- Основная функция ---
def main():
    """Запускает бота с вебхуками."""
    init_db()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Conversation для водителя
    conv_handler_driver = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_trip_driver, pattern='^create_trip_driver$')],
        states={
            ASK_TRIP_TYPE_DRIVER: [CallbackQueryHandler(ask_date_driver)],
            ASK_DATE_DRIVER: [CallbackQueryHandler(ask_hour_driver)],
            ASK_HOUR_DRIVER: [CallbackQueryHandler(ask_minute_driver)],
            ASK_MINUTE_DRIVER: [CallbackQueryHandler(ask_location_driver)],
            ASK_LOCATION_DRIVER: [MessageHandler(filters.LOCATION, ask_seats)],
            ASK_SEATS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_trip)],
            CONFIRM_BUG_REPORT: [CallbackQueryHandler(handle_bug_report)], # Добавлено новое состояние
        },
        fallbacks=[],
    )

    # Conversation для пассажира
    conv_handler_passenger = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_request_passenger, pattern='^create_request_passenger$')],
        states={
            ASK_TRIP_TYPE_PASSENGER: [CallbackQueryHandler(ask_date_passenger)],
            ASK_DATE_PASSENGER: [CallbackQueryHandler(ask_hour_passenger)],
            ASK_HOUR_PASSENGER: [CallbackQueryHandler(ask_minute_passenger)],
            ASK_MINUTE_PASSENGER: [CallbackQueryHandler(ask_location_passenger)],
            ASK_LOCATION_PASSENGER: [MessageHandler(filters.LOCATION, ask_comment_passenger)],
            ASK_COMMENT_PASSENGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_seats_passenger)],
            ASK_SEATS_PASSENGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_request)],
        },
        fallbacks=[],
    )

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("driver_menu", driver_menu))
    application.add_handler(CommandHandler("passenger_menu", passenger_menu))
    # application.add_handler(CommandHandler("switch_role", start)) # Если вдруг решите добавить позже

    application.add_handler(CallbackQueryHandler(handle_role_choice, pattern='^role_'))
    application.add_handler(conv_handler_driver)
    application.add_handler(conv_handler_passenger)
    application.add_handler(CallbackQueryHandler(manage_trips_driver, pattern='^manage_trips_driver$'))
    application.add_handler(CallbackQueryHandler(manage_requests_passenger, pattern='^manage_requests_passenger$'))
    application.add_handler(CallbackQueryHandler(handle_accept_reject, pattern='^(accept|reject)_'))
    application.add_handler(CallbackQueryHandler(handle_deal_confirmation, pattern='^confirm_deal_'))
    application.add_handler(CallbackQueryHandler(delete_trip, pattern='^delete_trip_'))
    application.add_handler(CallbackQueryHandler(delete_request, pattern='^delete_request_'))

    # Запуск через вебхуки
    port = int(os.environ.get('PORT', 10000))  # Render предоставляет PORT
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=f'https://{os.environ.get("RENDER_EXTERNAL_HOSTNAME")}/{TELEGRAM_BOT_TOKEN}' # URL вашего Render сервиса
    )

if __name__ == '__main__':
    main()