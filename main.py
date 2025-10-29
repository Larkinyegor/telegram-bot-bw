import logging
import uuid
import os
import random
import locale
import httpx
import pytz
import sqlite3
import json
from collections import deque
from datetime import datetime, time, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo, \
    InputMediaAnimation
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest

# Загружаем переменные окружения из .env файла (для локального запуска)
load_dotenv()

# Настройка русской локали
try:
    locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
except locale.Error:
    try:
        locale.setlocale(locale.LC_TIME, 'ru_RU')
    except locale.Error:
        logging.warning("Russian locale not found, month/day names might be in English.")

# Включаем логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ВАШИ ДАННЫЕ (из переменных окружения) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ALLOWED_USER_IDS_STR = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = [int(uid.strip()) for uid in ALLOWED_USER_IDS_STR.split(',') if uid.strip()]

TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
RANDOM_MESSAGES_STR = os.getenv("RANDOM_MESSAGES", "")
RANDOM_MESSAGES = [msg.strip() for msg in RANDOM_MESSAGES_STR.split('|') if msg.strip()]

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
CITY_NAME = os.getenv("CITY_NAME")

VK_SERVICE_TOKEN = os.getenv("VK_SERVICE_TOKEN")
VK_API_VERSION = "5.131"
VK_COMMUNITIES_STR = os.getenv("VK_COMMUNITIES", "")
VK_COMMUNITIES = {}
if VK_COMMUNITIES_STR:
    for item in VK_COMMUNITIES_STR.split(','):
        parts = item.split(':')
        if len(parts) == 2:
            VK_COMMUNITIES[parts[0].strip()] = int(parts[1].strip())

DB_NAME = "bot_data.db"
MOSCOW_TZ = pytz.timezone("Europe/Moscow")
# --------------------

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ И КОНСТАНТЫ ---
last_post_time = None
bot_startup_time = None
JOB_KWARGS = {'misfire_grace_time': 30}


# --- ФУНКЦИИ ДЛЯ РАБОТЫ С БД ---

def setup_database():
    """Создает таблицы, если они не существуют."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meme_queue (
            id TEXT PRIMARY KEY,
            post_data TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS special_posts (
            post_type TEXT PRIMARY KEY,
            post_data TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("База данных успешно настроена.")


def save_bot_state(key: str, value: str):
    """Сохраняет значение состояния бота в БД."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def get_bot_state(key: str) -> str | None:
    """Получает значение состояния бота из БД."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def save_or_update_special_post(post_type: str, post_data: dict):
    """Сохраняет или обновляет специальный пост (утро/вечер)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO special_posts (post_type, post_data) VALUES (?, ?)",
                   (post_type, json.dumps(post_data)))
    conn.commit()
    conn.close()


def get_special_post(post_type: str) -> dict | None:
    """Получает специальный пост из БД."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT post_data FROM special_posts WHERE post_type = ?", (post_type,))
    row = cursor.fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def delete_special_post(post_type: str):
    """Удаляет специальный пост из БД."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM special_posts WHERE post_type = ?", (post_type,))
    conn.commit()
    conn.close()


def add_post_to_db(post_data: dict):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO meme_queue (id, post_data) VALUES (?, ?)",
                   (post_data['id'], json.dumps(post_data)))
    conn.commit()
    conn.close()


def get_all_posts_from_db() -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT post_data FROM meme_queue ORDER BY created_at ASC")
    rows = cursor.fetchall()
    conn.close()
    return [json.loads(row[0]) for row in rows]


def delete_post_from_db(post_id: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM meme_queue WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()


def count_posts_in_db() -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM meme_queue")
    count = cursor.fetchone()[0]
    conn.close()
    return count


# --- ФУНКЦИИ ДЛЯ ИНТЕГРАЦИИ С VK ---
async def fetch_vk_photos(community_id: int, count: int = 10) -> list[str]:
    """Делает запрос к VK API и возвращает список URL последних фотографий."""
    photo_urls = []
    api_url = "https://api.vk.com/method/wall.get"
    params = {
        "owner_id": community_id,
        "count": 40,
        "access_token": VK_SERVICE_TOKEN,
        "v": VK_API_VERSION
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(api_url, params=params)
            response.raise_for_status()
            data = response.json()
        if 'error' in data:
            logger.error(f"VK API Error: {data['error']['error_msg']}")
            return []
        for post in data['response']['items']:
            if 'attachments' not in post:
                continue
            for attachment in post['attachments']:
                if attachment['type'] == 'photo':
                    max_size_photo = max(attachment['photo']['sizes'], key=lambda size: size['width'])
                    photo_urls.append(max_size_photo['url'])
                    if len(photo_urls) >= count:
                        return photo_urls
        return photo_urls
    except Exception as e:
        logger.error(f"Ошибка при запросе к VK API: {e}")
        return []


# --- "УМНЫЙ" ПЛАНИРОВЩИК ---

async def recalculate_and_schedule_all_posts(context: ContextTypes.DEFAULT_TYPE) -> None:
    for job in context.job_queue.jobs():
        if job.name and job.name.startswith("normal_post_job_"):
            job.schedule_removal()
    logger.info("Все старые задачи для обычных постов удалены. Начинается перепланирование.")

    posts_to_schedule = get_all_posts_from_db()
    if not posts_to_schedule:
        logger.info("Очередь пуста, планировать нечего.")
        return

    now = datetime.now(MOSCOW_TZ)
    start_of_day = now.replace(hour=10, minute=0, second=0, microsecond=0)
    one_hour_from_startup = bot_startup_time + timedelta(hours=1)
    one_hour_from_last_post = last_post_time + timedelta(hours=1) if last_post_time else now

    scheduling_start_time = max(now, start_of_day, one_hour_from_startup, one_hour_from_last_post)
    scheduling_end_time = now.replace(hour=23, minute=0, second=0, microsecond=0)

    if scheduling_start_time >= scheduling_end_time:
        logger.info("Временное окно на сегодня закрыто. Посты будут запланированы завтра.")
        return

    num_posts = len(posts_to_schedule)
    available_duration = scheduling_end_time - scheduling_start_time
    interval = available_duration / (num_posts + 1)

    for i, post_data in enumerate(posts_to_schedule):
        post_time = scheduling_start_time + (interval * (i + 1))
        job_name = f"normal_post_job_{post_data['id']}"
        context.job_queue.run_once(
            post_normal_meme, when=post_time, data=post_data, name=job_name,
            job_kwargs=JOB_KWARGS
        )
        logger.info(f"Пост {post_data['id']} запланирован на {post_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И КОМАНДЫ ---

async def get_weather_text() -> str:
    url = f"https://api.openweathermap.org/data/2.5/forecast?q={CITY_NAME}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ru"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        now = datetime.now(MOSCOW_TZ)
        today_date_str = now.strftime("%Y-%m-%d")
        daily_forecasts = [f for f in data['list'] if f['dt_txt'].startswith(today_date_str)]
        if not daily_forecasts:
            return "Не найден прогноз на сегодня."
        current_weather = daily_forecasts[0]
        current_temp = round(current_weather['main']['temp'])
        feels_like = round(current_weather['main']['feels_like'])
        description = current_weather['weather'][0]['description'].capitalize()
        wind_speed = current_weather['wind']['speed']
        temp_min = round(min(f['main']['temp_min'] for f in daily_forecasts))
        temp_max = round(max(f['main']['temp_max'] for f in daily_forecasts))
        return (
            f"🌤️ Погода в г. {CITY_NAME}:\n\n"
            f"🌡️ Сейчас: {current_temp}°C (ощущается как {feels_like}°C), {description}.\n\n"
            f"🌆 В течение дня:\n"
            f"  • Макс: {temp_max}°C\n  • Мин: {temp_min}°C\n  • Ветер: {wind_speed:.1f} м/с."
        )
    except Exception as e:
        logger.error(f"Ошибка при получении или обработке данных о погоде: {e}")
        return "Не удалось загрузить данные о погоде."


async def show_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS: return
    jobs = context.job_queue.jobs()
    if not jobs:
        await update.message.reply_text("Нет запланированных задач.")
        return
    response = "🗓️ Запланированные задачи:\n\n"
    for job in jobs:
        next_run = job.next_run_time.astimezone(MOSCOW_TZ).strftime(
            '%Y-%m-%d %H:%M:%S %Z') if job.next_run_time else "N/A"
        response += (
            f"🔹 **Название:** `{job.name}`\n"
            f"   **Следующий запуск:** {next_run}\n"
            f"   **Статус:** {'Активна' if job.enabled else 'Приостановлена'}\n\n"
        )
    await update.message.reply_text(response, parse_mode='Markdown')


async def rate_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message.reply_to_message:
        await update.message.reply_text("Чтобы оценить сообщение, используйте команду /rate в ответ на него.")
        return
    ratings = ["Говняк", "Сомнительно", "Норм", "Секс", "Слон сука!"]
    weights = [24, 24, 24, 24, 4]
    chosen_rating = random.choices(ratings, weights=weights, k=1)[0]
    response_text = f"Моя оценка: {chosen_rating}"
    await update.message.reply_text(response_text)


async def morning_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS: return
    await send_daily_greeting(context)
    await update.message.reply_text("Утреннее приветствие отправлено в целевой чат.")


async def vk_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS: return
    if update.message and update.message.chat.type in ['group', 'supergroup']:
        await update.message.reply_text("Эта команда доступна только в личных сообщениях с ботом.")
        return
    if not VK_COMMUNITIES:
        await update.message.reply_text("Список VK сообществ пуст. Добавьте их в код.")
        return
    keyboard = []
    for name, community_id in VK_COMMUNITIES.items():
        button = InlineKeyboardButton(name, callback_data=f"vk_post_{community_id}")
        keyboard.append([button])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите сообщество для постинга:", reply_markup=reply_markup)


async def vk_community_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        community_id = int(query.data.split('_')[-1])
    except (ValueError, IndexError):
        await query.edit_message_text("Ошибка: неверный ID сообщества.")
        return
    await query.edit_message_text("⏳ Ищу последние 10 фото, пожалуйста, подождите...")
    photo_urls = await fetch_vk_photos(community_id, count=10)
    if not photo_urls:
        await query.edit_message_text("Не удалось найти фотографии в последних постах этого сообщества.")
        return
    media_group = [InputMediaPhoto(media=url) for url in photo_urls]
    try:
        await context.bot.send_media_group(chat_id=query.message.chat_id, media=media_group)
        await query.delete_message()
        await context.bot.send_message(chat_id=query.message.chat_id, text=f"✅ Вот последние {len(photo_urls)} фото.")
    except Exception as e:
        logger.error(f"Не удалось отправить медиа-группу: {e}")
        await query.edit_message_text(f"Произошла ошибка при отправке: {e}")


# --- ФУНКЦИИ ПРИВЕТСТВИЯ И СООБЩЕНИЙ ---

async def send_daily_greeting(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"Подготовка утреннего приветствия для чата {TARGET_CHAT_ID}")
    now = datetime.now(MOSCOW_TZ)
    month_names = {
        1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
        7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
    }
    weekday_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    date_str = f"{now.day} {month_names[now.month]} {now.year} года"
    day_of_week = weekday_names[now.weekday()]
    day_descriptions = {
        "Понедельник": "терпение начинается.", "Вторник": "терпение усиливается.", "Среда": "терпение максимизируется.",
        "Четверг": "терпение дожимается.", "Пятница": "терпение испаряется", "Суббота": "терпение забывается.",
        "Воскресенье": "терпение вспоминается."
    }
    day_description = day_descriptions.get(day_of_week, "хорошего дня!")
    weather_text = await get_weather_text()
    final_message = (
        f"Доброе утро всем! ☀️\n\n"
        f"Сегодня {date_str}.\n"
        f"День недели: {day_of_week} - {day_description}\n\n"
        f"---\n\n{weather_text}"
    )
    try:
        await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=final_message)
        logger.info("Утреннее приветствие успешно отправлено.")
    except Exception as e:
        logger.error(f"Не удалось отправить утреннее приветствие в чат {TARGET_CHAT_ID}: {e}")


async def send_and_reschedule_random_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"Отправка случайного сообщения в чат {TARGET_CHAT_ID}")
    if not RANDOM_MESSAGES:
        logger.warning("Список случайных сообщений пуст. Отправка отменена.")
        return
    try:
        message_to_send = random.choice(RANDOM_MESSAGES)
        await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=message_to_send)
        logger.info("Случайное сообщение успешно отправлено.")
    except Exception as e:
        logger.error(f"Не удалось отправить случайное сообщение в чат {TARGET_CHAT_ID}: {e}")
    tomorrow = datetime.now(MOSCOW_TZ).date() + timedelta(days=1)
    random_hour = random.randint(10, 22)
    random_minute = random.randint(0, 59)
    random_time = time(hour=random_hour, minute=random_minute)
    next_run_datetime = datetime.combine(tomorrow, random_time).astimezone(MOSCOW_TZ)
    context.job_queue.run_once(send_and_reschedule_random_message, when=next_run_datetime)
    logger.info(f"Следующее случайное сообщение запланировано на {next_run_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')}")


# --- ФУНКЦИИ ДЛЯ ПОСТИНГА ---
async def post_good_morning(context: ContextTypes.DEFAULT_TYPE):
    global last_post_time
    post_data = get_special_post('good_morning')
    if not post_data:
        logger.info("Нет запланированного утреннего поста для публикации.")
        return
    logger.info("Публикую утренний пост из БД...")
    try:
        if post_data['type'] == 'photo':
            await context.bot.send_photo(chat_id=CHANNEL_ID, photo=post_data['file_id'], caption=post_data['caption'])
        elif post_data['type'] == 'video':
            await context.bot.send_video(chat_id=CHANNEL_ID, video=post_data['file_id'], caption=post_data['caption'])
        elif post_data['type'] == 'animation':
            await context.bot.send_animation(chat_id=CHANNEL_ID, animation=post_data['file_id'],
                                             caption=post_data['caption'])
        now = datetime.now(MOSCOW_TZ)
        last_post_time = now
        save_bot_state('last_post_time', now.isoformat())
        delete_special_post('good_morning')
        logger.info("Утренний пост успешно опубликован и удален из БД.")
    except Exception as e:
        logger.error(f"Не удалось опубликовать утренний пост: {e}")


async def post_good_night(context: ContextTypes.DEFAULT_TYPE):
    global last_post_time
    post_data = get_special_post('good_night')
    if not post_data:
        logger.info("Нет запланированного вечернего поста для публикации.")
        return
    logger.info("Публикую вечерний пост из БД...")
    try:
        if post_data['type'] == 'photo':
            await context.bot.send_photo(chat_id=CHANNEL_ID, photo=post_data['file_id'], caption=post_data['caption'])
        elif post_data['type'] == 'video':
            await context.bot.send_video(chat_id=CHANNEL_ID, video=post_data['file_id'], caption=post_data['caption'])
        elif post_data['type'] == 'animation':
            await context.bot.send_animation(chat_id=CHANNEL_ID, animation=post_data['file_id'],
                                             caption=post_data['caption'])
        now = datetime.now(MOSCOW_TZ)
        last_post_time = now
        save_bot_state('last_post_time', now.isoformat())
        delete_special_post('good_night')
        logger.info("Вечерний пост успешно опубликован и удален из БД.")
    except Exception as e:
        logger.error(f"Не удалось опубликовать вечерний пост: {e}")


async def post_normal_meme(context: ContextTypes.DEFAULT_TYPE):
    global last_post_time
    post_data = context.job.data
    logger.info(f"Публикую обычный пост {post_data['id']}.")
    caption = post_data.get('caption')
    try:
        if post_data['type'] == 'photo':
            await context.bot.send_photo(chat_id=CHANNEL_ID, photo=post_data['file_id'], caption=caption)
        elif post_data['type'] == 'video':
            await context.bot.send_video(chat_id=CHANNEL_ID, video=post_data['file_id'], caption=caption)
        elif post_data['type'] == 'animation':
            await context.bot.send_animation(chat_id=CHANNEL_ID, animation=post_data['file_id'], caption=caption)
        delete_post_from_db(post_data['id'])
        now = datetime.now(MOSCOW_TZ)
        last_post_time = now
        save_bot_state('last_post_time', now.isoformat())
        logger.info(f"Обычный пост {post_data['id']} успешно опубликован и удален из БД.")
    except Exception as e:
        logger.error(f"Не удалось опубликовать обычный пост {post_data['id']}: {e}")


# --- ОБРАБОТЧИКИ КОМАНД И КНОПОК ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        logger.warning(f"Попытка несанкционированного доступа от user_id: {user_id}")
        return
    if update.message and update.message.chat.type in ['group', 'supergroup']:
        await update.message.reply_text("Для управления ботом, пожалуйста, напишите мне в личные сообщения.")
        return
    keyboard = [[InlineKeyboardButton("Постинг мемов", callback_data='post_meme')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = 'Привет, Администратор! Выбери действие:'
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == 'post_meme':
        gm_scheduled = get_special_post('good_morning') is not None
        gn_scheduled = get_special_post('good_night') is not None
        gm_text = "Доброе утро! ✅" if gm_scheduled else "Доброе утро!"
        gn_text = "Спокойной ночи! ✅" if gn_scheduled else "Спокойной ночи!"
        keyboard = [
            [InlineKeyboardButton(gm_text, callback_data='good_morning')],
            [InlineKeyboardButton(gn_text, callback_data='good_night')],
            [InlineKeyboardButton("Обычный постинг", callback_data='normal_post')],
            [InlineKeyboardButton("👀 Просмотр очереди", callback_data='view_queue_0')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='start')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text(text="Выбери тип поста:", reply_markup=reply_markup)
        except BadRequest as e:
            if 'no text in the message to edit' in str(e).lower():
                await query.message.delete()
                await query.message.chat.send_message(text="Выбери тип поста:", reply_markup=reply_markup)
            else:
                logger.error(f"Неожиданная ошибка BadRequest при возврате в меню: {e}")
    elif query.data in ['good_morning', 'good_night', 'normal_post']:
        context.user_data['post_type'] = query.data
        text_map = {
            'good_morning': "Отправь медиа для утреннего поста. Текст, который ты добавишь к медиа, будет опубликован над основной подписью.",
            'good_night': "Отправь медиа для вечернего поста. Текст, который ты добавишь к медиа, будет опубликован над основной подписью.",
            'normal_post': "Кидай мемы! Я добавлю их в очередь на публикацию. Если добавишь к медиа текст, я опубликую его вместе с ним."
        }
        back_button_keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data='post_meme')]]
        reply_markup = InlineKeyboardMarkup(back_button_keyboard)
        await query.edit_message_text(text=text_map[query.data], reply_markup=reply_markup)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data: return
    post_type = context.user_data.get('post_type')
    if not post_type: return

    message = update.message
    media_file, file_type = None, None
    if message.photo:
        media_file, file_type = message.photo[-1], 'photo'
    elif message.video:
        media_file, file_type = message.video, 'video'
    elif message.animation:
        media_file, file_type = message.animation, 'animation'
    if not media_file:
        await message.reply_text("Пожалуйста, отправь фото, видео или гифку.")
        return

    post_data = {'type': file_type, 'file_id': media_file.file_id}
    back_to_menu_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data='post_meme')]])
    user_caption = message.caption

    if post_type == 'good_morning':
        bot_greeting = "Доброе утро!"
        post_data['caption'] = f"{user_caption}\n\n{bot_greeting}" if user_caption else bot_greeting
        save_or_update_special_post('good_morning', post_data)
        await message.reply_text("Утренний пост на 10:00 сохранен! 💛", reply_markup=back_to_menu_markup)
        context.user_data.clear()

    elif post_type == 'good_night':
        bot_greeting = "Спокойной ночи!"
        post_data['caption'] = f"{user_caption}\n\n{bot_greeting}" if user_caption else bot_greeting
        save_or_update_special_post('good_night', post_data)
        await message.reply_text("Вечерний пост на 23:00 сохранен! 💛", reply_markup=back_to_menu_markup)
        context.user_data.clear()

    elif post_type == 'normal_post':
        post_data['id'] = str(uuid.uuid4())
        post_data['caption'] = user_caption
        add_post_to_db(post_data)
        await message.reply_text(
            f"Мем добавлен в очередь. Всего в очереди: {count_posts_in_db()}.",
            reply_markup=back_to_menu_markup
        )
        await recalculate_and_schedule_all_posts(context)
        context.user_data.clear()


# --- ФУНКЦИИ ДЛЯ ПРОСМОТРА И УДАЛЕНИЯ ---
async def show_queue_item(update: Update, context: ContextTypes.DEFAULT_TYPE, index: int = None) -> None:
    query = update.callback_query
    await query.answer()
    current_index = index
    if current_index is None:
        try:
            current_index = int(query.data.split('_')[-1])
        except (ValueError, IndexError):
            await query.edit_message_text("Ошибка: неверный индекс.")
            return
    posts_in_queue = get_all_posts_from_db()
    if not posts_in_queue:
        try:
            await query.message.delete()
        except BadRequest:
            pass
        await query.message.chat.send_message(
            "Очередь обычных постов пуста.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data='post_meme')]])
        )
        return
    current_index = max(0, min(current_index, len(posts_in_queue) - 1))
    post_data = posts_in_queue[current_index]
    post_id = post_data['id']
    keyboard = []
    nav_buttons = []
    if current_index > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f'view_queue_{current_index - 1}'))
    nav_buttons.append(InlineKeyboardButton("❌ Удалить", callback_data=f'delete_{post_id}_{current_index}'))
    if current_index < len(posts_in_queue) - 1:
        nav_buttons.append(InlineKeyboardButton("➡️ Вперед", callback_data=f'view_queue_{current_index + 1}'))
    keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton("⬅️ Назад в меню", callback_data='post_meme')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    caption = f"Пост {current_index + 1} из {len(posts_in_queue)}"
    user_caption = post_data.get('caption')
    if user_caption:
        caption += f"\n\n---\n{user_caption}"
    media_type = post_data['type']
    file_id = post_data['file_id']
    media_map = {
        'photo': InputMediaPhoto(media=file_id, caption=caption),
        'video': InputMediaVideo(media=file_id, caption=caption),
        'animation': InputMediaAnimation(media=file_id, caption=caption)
    }
    try:
        await query.edit_message_media(media=media_map[media_type], reply_markup=reply_markup)
    except BadRequest as e:
        if 'message is not modified' in str(e).lower():
            pass
        else:
            await query.delete_message()
            if media_type == 'photo':
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=file_id, caption=caption,
                                             reply_markup=reply_markup)
            elif media_type == 'video':
                await context.bot.send_video(chat_id=query.message.chat_id, video=file_id, caption=caption,
                                             reply_markup=reply_markup)
            elif media_type == 'animation':
                await context.bot.send_animation(chat_id=query.message.chat_id, animation=file_id, caption=caption,
                                                 reply_markup=reply_markup)


async def delete_queue_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, post_id_to_delete, index_str = query.data.split('_')
        index = int(index_str)
    except ValueError:
        await query.answer("Ошибка при удалении.", show_alert=True)
        return
    delete_post_from_db(post_id_to_delete)
    logger.info(f"Пост с ID {post_id_to_delete} удален из очереди.")
    await recalculate_and_schedule_all_posts(context)
    await show_queue_item(update, context, index=index)


def main() -> None:
    setup_database()
    global bot_startup_time, last_post_time
    bot_startup_time = datetime.now(MOSCOW_TZ)
    last_post_time_str = get_bot_state('last_post_time')
    if last_post_time_str:
        last_post_time = datetime.fromisoformat(last_post_time_str)
        logger.info(f"Восстановлено время последнего поста: {last_post_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    logger.info(f"Бот запущен в {bot_startup_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    application = Application.builder().token(BOT_TOKEN).build()
    application.job_queue.run_once(recalculate_and_schedule_all_posts, when=2, name="initial_recalculator")

    application.job_queue.run_daily(
        post_good_morning, time=time(hour=10, minute=0, tzinfo=MOSCOW_TZ),
        name='good_morning_job', job_kwargs=JOB_KWARGS
    )
    application.job_queue.run_daily(
        post_good_night, time=time(hour=23, minute=0, tzinfo=MOSCOW_TZ),
        name='good_night_job', job_kwargs=JOB_KWARGS
    )
    application.job_queue.run_daily(
        send_daily_greeting, time=time(hour=10, minute=0, tzinfo=MOSCOW_TZ),
        name='daily_greeting_job', job_kwargs=JOB_KWARGS
    )
    application.job_queue.run_daily(
        recalculate_and_schedule_all_posts, time=time(hour=0, minute=1, tzinfo=MOSCOW_TZ),
        name='daily_recalculator', job_kwargs=JOB_KWARGS
    )

    now_in_tz = datetime.now(MOSCOW_TZ)
    random_hour = random.randint(10, 22)
    random_minute = random.randint(0, 59)
    today_random_time = now_in_tz.replace(hour=random_hour, minute=random_minute, second=0, microsecond=0)
    first_run_datetime = today_random_time if today_random_time > now_in_tz else today_random_time + timedelta(days=1)
    application.job_queue.run_once(
        send_and_reschedule_random_message, when=first_run_datetime, job_kwargs=JOB_KWARGS
    )
    logger.info(f"Первое случайное сообщение запланировано на {first_run_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("rate", rate_message))
    application.add_handler(CommandHandler("jobs", show_jobs))
    application.add_handler(CommandHandler("morning", morning_command))
    application.add_handler(CommandHandler("vk", vk_command))
    application.add_handler(CallbackQueryHandler(start, pattern='^start$'))
    application.add_handler(CallbackQueryHandler(button, pattern='^(post_meme|good_morning|good_night|normal_post)$'))
    application.add_handler(CallbackQueryHandler(show_queue_item, pattern='^view_queue_'))
    application.add_handler(CallbackQueryHandler(delete_queue_item, pattern='^delete_'))
    application.add_handler(CallbackQueryHandler(vk_community_selected, pattern='^vk_post_'))
    application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION, handle_media))

    application.run_polling()


if __name__ == '__main__':
    main()