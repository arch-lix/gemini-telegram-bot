import asyncio
import logging
import re
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, \
    CallbackQuery, FSInputFile
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import aiohttp
import requests
import json
import os
from datetime import datetime, timedelta
import subprocess
import signal
from PIL import Image
import pytesseract
import io

# Настройка пути к Tesseract
if os.name == 'nt':  # Windows
    # Проверяем стандартные пути установки Tesseract
    possible_paths = [
        r'C:\Program Files\Tesseract-OCR\tesseract.exe',
        r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
        r'C:\Tesseract-OCR\tesseract.exe'
    ]
    for path in possible_paths:
        if os.path.exists(path):
            pytesseract.pytesseract.tesseract_cmd = path
            break
else:
    # На Linux (Railway, Heroku и т.д.) Tesseract устанавливается через apt
    # и доступен в PATH, поэтому явно указывать путь не нужно
    pass

# Настройки
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8157269355:AAFOCDNdApPolAeBBjbY1An-OfYIokLvfKc")
API_KEY = os.getenv("API_KEY", "openai")  # API ключ для доступа к AI (базовый ключ: openai)
API_URL = "http://api.onlysq.ru/ai/v2"
DEFAULT_MODEL = "gpt-4o-mini"
AVAILABLE_MODELS = {
    "gpt-4o-mini": {"name": "⚡️ GPT-4o Mini", "cost": 1, "desc": "Быстрая и эффективная модель от OpenAI"},
    "gemini-3-pro": {"name": "⭐️ Gemini 3 Pro", "cost": 1, "desc": "Флагманская рассуждающая модель от Google"},
    "gemini-3-pro-preview": {"name": "👽 Gemini 3 Pro Preview", "cost": 1, "desc": "Быстрая preview версия Gemini 3 Pro"},
    "deepseek-v3": {"name": "🐼 DeepSeek V3", "cost": 1, "desc": "Текстовая модель от китайского разработчика"},
    "grok-3": {"name": "🚀 Grok 3", "cost": 1, "desc": "Продвинутая модель от xAI"},
    "sonar-deep-research": {"name": "🔍 Sonar Deep Research", "cost": 1, "desc": "Модель для глубокого анализа"}
}
DB_FILE = "chat_history.json"
DATABASE_FILE = "database.json"  # Объединенная база пользователей и ботов
SETTINGS_FILE = "bot_settings.json"
BOTS_DIR = "user_bots"
MAX_MESSAGE_LENGTH = 4000
ADMIN_ID = 8087962709

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Создаем папку для ботов
os.makedirs(BOTS_DIR, exist_ok=True)

# Храним процессы запущенных ботов
running_bots = {}


# === FSM STATES ===
class BotCreation(StatesGroup):
    waiting_for_token = State()
    waiting_for_prompt = State()


class BotEdit(StatesGroup):
    waiting_for_changes = State()


class AdminStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_model_selection = State()
    waiting_for_tokens_amount = State()
    waiting_for_model_limit = State()


# === КЛАВИАТУРЫ ===
def get_main_keyboard():
    """Главная клавиатура"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🤖 Создать бота"), KeyboardButton(text="📋 Мои боты")],
            [KeyboardButton(text="�  Чат с AI"), KeyboardButton(text="🎯 Выбрать модель")],

        ],
        resize_keyboard=True
    )
    return keyboard


def get_bot_management_keyboard(bot_id: str, is_running: bool):
    """Клавиатура управления ботом"""
    buttons = []

    if is_running:
        buttons.append([InlineKeyboardButton(text="⏹️ Остановить бота", callback_data=f"stop_{bot_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="▶️ Запустить бота", callback_data=f"start_{bot_id}")])

    buttons.append([
        InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_{bot_id}"),
        InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"delete_{bot_id}")
    ])
    buttons.append([
        InlineKeyboardButton(text="📦 Зависимости", callback_data=f"deps_{bot_id}"),
        InlineKeyboardButton(text="💾 Скачать код", callback_data=f"download_{bot_id}")
    ])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_bots")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# === РАБОТА С JSON БАЗОЙ ЧАТОВ ===
def load_db():
    """Загрузить JSON базу"""
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_db(data):
    """Сохранить в JSON"""
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_message(user_id: int, role: str, content: str):
    """Сохранить сообщение"""
    db = load_db()
    user_id_str = str(user_id)

    if user_id_str not in db:
        db[user_id_str] = []

    db[user_id_str].append({
        "role": role,
        "content": content,
        "timestamp": datetime.now().isoformat()
    })

    save_db(db)


def get_history(user_id: int, limit: int = 20) -> list:
    """Получить историю"""
    db = load_db()
    user_id_str = str(user_id)

    if user_id_str not in db:
        return []

    messages = db[user_id_str][-limit:]
    return [{"role": msg["role"], "content": msg["content"]} for msg in messages]


def clear_history(user_id: int):
    """Очистить историю"""
    db = load_db()
    user_id_str = str(user_id)

    if user_id_str in db:
        db[user_id_str] = []
        save_db(db)



# === РАБОТА С ЕДИНОЙ БАЗОЙ ДАННЫХ ===
def load_database():
    """Загрузить базу данных"""
    if os.path.exists(DATABASE_FILE):
        try:
            with open(DATABASE_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                # Проверяем, что файл не пустой
                if not content:
                    return {"users": {}}
                data = json.loads(content)
                # Если старый формат - конвертируем
                if "users" not in data:
                    return {"users": data}
                return data
        except (json.JSONDecodeError, ValueError) as e:
            logging.error(f"Ошибка чтения базы данных: {e}. Создаю новую базу.")
            return {"users": {}}
    return {"users": {}}


def save_database(data):
    """Сохранить базу данных с резервным копированием"""
    # Создаем резервную копию перед сохранением
    if os.path.exists(DATABASE_FILE):
        import shutil
        backup_file = DATABASE_FILE + '.backup'
        try:
            shutil.copy2(DATABASE_FILE, backup_file)
        except Exception as e:
            logging.error(f"Ошибка создания резервной копии: {e}")
    
    # Сохраняем новые данные
    try:
        with open(DATABASE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения базы данных: {e}")
        # Восстанавливаем из резервной копии
        if os.path.exists(backup_file):
            shutil.copy2(backup_file, DATABASE_FILE)
            logging.info("База данных восстановлена из резервной копии")


def get_user_bots(user_id: int) -> list:
    """Получить ботов пользователя"""
    db = load_database()
    user_id_str = str(user_id)
    if user_id_str in db["users"]:
        return db["users"][user_id_str].get("bots", [])
    return []


def add_bot(user_id: int, bot_token: str, prompt: str, bot_id: str, model: str):
    """Добавить бота"""
    db = load_database()
    user_id_str = str(user_id)
    if user_id_str not in db["users"]:
        db["users"][user_id_str] = {"bots": []}
    if "bots" not in db["users"][user_id_str]:
        db["users"][user_id_str]["bots"] = []
    
    db["users"][user_id_str]["bots"].append({
        "bot_id": bot_id,
        "token": bot_token,
        "prompt": prompt,
        "model": model,
        "created_at": datetime.now().isoformat(),
        "is_running": False
    })
    save_database(db)


def update_bot_status(user_id: int, bot_id: str, is_running: bool):
    """Обновить статус бота"""
    db = load_database()
    user_id_str = str(user_id)
    if user_id_str in db["users"] and "bots" in db["users"][user_id_str]:
        for bot in db["users"][user_id_str]["bots"]:
            if bot["bot_id"] == bot_id:
                bot["is_running"] = is_running
                save_database(db)
                break


def delete_bot_from_db(user_id: int, bot_id: str):
    """Удалить бота"""
    db = load_database()
    user_id_str = str(user_id)
    if user_id_str in db["users"] and "bots" in db["users"][user_id_str]:
        db["users"][user_id_str]["bots"] = [b for b in db["users"][user_id_str]["bots"] if b["bot_id"] != bot_id]
        save_database(db)


def get_bot_data(user_id: int, bot_id: str):
    """Получить данные бота"""
    bots = get_user_bots(user_id)
    for bot in bots:
        if bot["bot_id"] == bot_id:
            return bot
    return None


def update_bot_prompt(user_id: int, bot_id: str, new_prompt: str):
    """Обновить промпт бота"""
    db = load_database()
    user_id_str = str(user_id)
    if user_id_str in db["users"] and "bots" in db["users"][user_id_str]:
        for bot in db["users"][user_id_str]["bots"]:
            if bot["bot_id"] == bot_id:
                bot["prompt"] = new_prompt
                save_database(db)
                break


# === РАБОТА С ПОЛЬЗОВАТЕЛЯМИ ===
def get_user_data(user_id: int, username: str = None):
    """Получить данные пользователя"""
    db = load_database()
    user_id_str = str(user_id)
    needs_save = False
    
    if user_id_str not in db["users"]:
        # Создаем токены для каждой модели
        model_tokens = {}
        for model_id in AVAILABLE_MODELS.keys():
            model_tokens[model_id] = get_model_limit(model_id)
        
        db["users"][user_id_str] = {
            "username": username or "unknown",
            "model_tokens": model_tokens,  # Токены для каждой модели
            "total_requests": 0,
            "last_reset": datetime.now().isoformat(),
            "registration_date": datetime.now().isoformat(),
            "selected_model": DEFAULT_MODEL,
            "bots": []
        }
        needs_save = True
    
    # Миграция старых данных
    if "requests_left" in db["users"][user_id_str] and "model_tokens" not in db["users"][user_id_str]:
        # Конвертируем старый формат в новый
        old_balance = db["users"][user_id_str].pop("requests_left", 0)
        model_tokens = {}
        for model_id in AVAILABLE_MODELS.keys():
            model_tokens[model_id] = old_balance  # Переносим старый баланс на все модели
        db["users"][user_id_str]["model_tokens"] = model_tokens
        needs_save = True
    
    # Добавляем недостающие модели (только если их нет)
    if "model_tokens" in db["users"][user_id_str]:
        for model_id in AVAILABLE_MODELS.keys():
            if model_id not in db["users"][user_id_str]["model_tokens"]:
                db["users"][user_id_str]["model_tokens"][model_id] = get_model_limit(model_id)
                needs_save = True
    
    if "selected_model" not in db["users"][user_id_str]:
        db["users"][user_id_str]["selected_model"] = DEFAULT_MODEL
        needs_save = True
        
    if "bots" not in db["users"][user_id_str]:
        db["users"][user_id_str]["bots"] = []
        needs_save = True
    
    # Сохраняем только если были изменения
    if needs_save:
        save_database(db)
    
    return db["users"][user_id_str]


def set_user_model(user_id: int, model: str):
    """Установить модель пользователя"""
    db = load_database()
    user_id_str = str(user_id)
    if user_id_str in db["users"]:
        db["users"][user_id_str]["selected_model"] = model
        save_database(db)


def check_and_reset_limits():
    """Проверить и сбросить лимиты для всех моделей"""
    db = load_database()
    now = datetime.now()
    updated = False
    
    for user_id, user_data in db["users"].items():
        last_reset = datetime.fromisoformat(user_data["last_reset"])
        hours_passed = (now - last_reset).total_seconds() / 3600
        
        if hours_passed >= 24:
            # Сбрасываем токены для каждой модели
            if "model_tokens" not in user_data:
                user_data["model_tokens"] = {}
            
            for model_id in AVAILABLE_MODELS.keys():
                user_data["model_tokens"][model_id] = get_model_limit(model_id)
            
            user_data["last_reset"] = now.isoformat()
            updated = True
    
    if updated:
        save_database(db)


def use_request(user_id: int, model_id: str = None) -> bool:
    """Использовать запрос для конкретной модели"""
    check_and_reset_limits()
    db = load_database()
    user_id_str = str(user_id)
    
    if user_id_str in db["users"]:
        user_data = db["users"][user_id_str]
        
        # Если не указана модель, используем выбранную пользователем
        if model_id is None:
            model_id = user_data.get("selected_model", DEFAULT_MODEL)
        
        # Проверяем наличие токенов для модели
        if "model_tokens" in user_data and model_id in user_data["model_tokens"]:
            if user_data["model_tokens"][model_id] > 0:
                user_data["model_tokens"][model_id] -= 1
                user_data["total_requests"] += 1
                save_database(db)
                return True
    return False


def add_requests(user_id: int, amount: int, model_id: str = None):
    """Добавить запросы для конкретной модели"""
    db = load_database()
    user_id_str = str(user_id)
    if user_id_str in db["users"]:
        user_data = db["users"][user_id_str]
        
        # Если не указана модель, добавляем ко всем моделям
        if model_id is None:
            if "model_tokens" not in user_data:
                user_data["model_tokens"] = {}
            for mid in AVAILABLE_MODELS.keys():
                if mid not in user_data["model_tokens"]:
                    user_data["model_tokens"][mid] = 0
                user_data["model_tokens"][mid] += amount
        else:
            # Добавляем только к указанной модели
            if "model_tokens" not in user_data:
                user_data["model_tokens"] = {}
            if model_id not in user_data["model_tokens"]:
                user_data["model_tokens"][model_id] = 0
            user_data["model_tokens"][model_id] += amount
        
        save_database(db)


def get_user_model_balance(user_id: int, model_id: str) -> int:
    """Получить баланс токенов для конкретной модели"""
    db = load_database()
    user_id_str = str(user_id)
    if user_id_str in db["users"]:
        user_data = db["users"][user_id_str]
        if "model_tokens" in user_data and model_id in user_data["model_tokens"]:
            return user_data["model_tokens"][model_id]
    return 0


def get_all_users():
    """Получить всех пользователей"""
    db = load_database()
    return db.get("users", {})


def get_bot_stats():
    """Статистика бота"""
    users = get_all_users()
    
    # Подсчитываем активных пользователей (у кого есть хотя бы 1 токен)
    active_count = 0
    for user_data in users.values():
        model_tokens = user_data.get("model_tokens", {})
        if any(tokens > 0 for tokens in model_tokens.values()):
            active_count += 1
    
    return {
        "total_users": len(users),
        "total_requests": sum(u["total_requests"] for u in users.values()),
        "active_users": active_count
    }


# === РАБОТА С НАСТРОЙКАМИ БОТА ===
def load_settings():
    """Загрузить настройки бота"""
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        "bot_creation_enabled": True,
        "model_limits": {
            "gemini-3-pro": 30,
            "gemini-3-pro-preview": 20,
            "deepseek-v3": 15,
            "grok-3": 15,
            "sonar-deep-research": 10
        }
    }


def save_settings(settings):
    """Сохранить настройки бота"""
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def get_model_limit(model: str):
    """Получить лимит для конкретной модели"""
    settings = load_settings()
    model_limits = settings.get("model_limits", {})
    return model_limits.get(model, 30)


def set_model_limit(model: str, limit: int):
    """Установить лимит для конкретной модели"""
    settings = load_settings()
    if "model_limits" not in settings:
        settings["model_limits"] = {}
    settings["model_limits"][model] = limit
    save_settings(settings)


def is_bot_creation_enabled():
    """Проверить, включено ли создание ботов"""
    settings = load_settings()
    return settings.get("bot_creation_enabled", True)


def set_bot_creation_enabled(enabled: bool):
    """Включить/отключить создание ботов"""
    settings = load_settings()
    settings["bot_creation_enabled"] = enabled
    save_settings(settings)


# === РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ ===
def split_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list:
    """Разбить длинное сообщение на части"""
    if len(text) <= max_length:
        return [text]

    parts = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break

        split_pos = text.rfind('\n', 0, max_length)
        if split_pos == -1:
            split_pos = text.rfind(' ', 0, max_length)
        if split_pos == -1:
            split_pos = max_length

        parts.append(text[:split_pos])
        text = text[split_pos:].lstrip()

    return parts


async def send_as_file(message: Message, text: str, caption: str = "📄 Ответ в файле"):
    """Отправить текст как файл"""
    filename = f"response_{message.from_user.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(text)
    
    # Отправляем файл
    file = FSInputFile(filename)
    await message.answer_document(file, caption=caption)
    
    # Удаляем временный файл
    os.remove(filename)


def format_ai_response(text: str) -> str:
    """Форматировать ответ AI для красивого отображения в Telegram"""
    
    # Сохраняем блоки кода
    code_blocks = []
    def save_code(match):
        code_blocks.append(match.group(0))
        return f"___CODE_BLOCK_{len(code_blocks)-1}___"
    
    # Временно заменяем блоки кода (```код```)
    text = re.sub(r'```[\s\S]*?```', save_code, text)
    
    # Убираем ВСЕ математические обертки $$ и $ полностью
    text = re.sub(r'\$\$([^\$]+)\$\$', r'\1', text)
    text = re.sub(r'\$([^\$]+)\$', r'\1', text)
    text = text.replace('$$', '').replace('$', '')
    
    # Обрабатываем \frac{числитель}{знаменатель} -> (числитель/знаменатель)
    text = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'(\1/\2)', text)
    
    # Обрабатываем \sqrt{число} -> √(число)
    text = re.sub(r'\\sqrt\{([^}]+)\}', r'√(\1)', text)
    
    # Обрабатываем степени: ^{число} или ^число -> используем Unicode
    def convert_superscript(match):
        num = match.group(1) if match.lastindex else match.group(0)[1]
        superscripts = {'0':'⁰','1':'¹','2':'²','3':'³','4':'⁴','5':'⁵','6':'⁶','7':'⁷','8':'⁸','9':'⁹','+':'⁺','-':'⁻','=':'⁼','(':'⁽',')':'⁾'}
        return ''.join(superscripts.get(c, c) for c in str(num))
    
    text = re.sub(r'\^\{([^}]+)\}', convert_superscript, text)
    text = re.sub(r'\^([0-9])', convert_superscript, text)
    
    # Обрабатываем индексы: _{число} -> используем Unicode
    def convert_subscript(match):
        num = match.group(1)
        subscripts = {'0':'₀','1':'₁','2':'₂','3':'₃','4':'₄','5':'₅','6':'₆','7':'₇','8':'₈','9':'₉','+':'₊','-':'₋','=':'₌','(':'₍',')':'₎'}
        return ''.join(subscripts.get(c, c) for c in str(num))
    
    text = re.sub(r'_\{([^}]+)\}', convert_subscript, text)
    
    # Полная замена LaTeX команд на понятные символы
    latex_replacements = {
        # Операции (ВАЖНО: делаем первыми)
        r'\\times': ' * ', r'\\cdot': ' * ', r'\\div': ' / ', r'\\pm': ' ± ',
        r'\\ldots': '...', r'\\dots': '...',
        
        # Сравнения
        r'\\leq': '≤', r'\\geq': '≥', r'\\neq': '≠', r'\\approx': '≈', r'\\equiv': '≡',
        
        # Стрелки
        r'\\rightarrow': '→', r'\\leftarrow': '←', r'\\to': '→',
        
        # Греческие буквы
        r'\\alpha': 'α', r'\\beta': 'β', r'\\gamma': 'γ', r'\\delta': 'δ',
        r'\\theta': 'θ', r'\\pi': 'π', r'\\sigma': 'σ', r'\\omega': 'ω',
        
        # Тригонометрия
        r'\\sin': 'sin', r'\\cos': 'cos', r'\\tan': 'tan', r'\\cot': 'cot',
        
        # Геометрия
        r'\\angle': '∠', r'\\circ': '°', r'\\degree': '°', r'\\triangle': '△',
        
        # Скобки
        r'\\left\(': '(', r'\\right\)': ')', r'\\left\[': '[', r'\\right\]': ']',
        r'\\left\{': '{', r'\\right\}': '}',
        r'\\left': '', r'\\right': '',
        
        # Текст
        r'\\text\{([^}]+)\}': r'\1',
    }
    
    # Применяем все замены
    for pattern, replacement in latex_replacements.items():
        text = re.sub(pattern, replacement, text)
    
    # Убираем ВСЕ оставшиеся LaTeX команды (начинающиеся с \)
    text = re.sub(r'\\[a-zA-Z_]+', '', text)
    
    # Убираем оставшиеся обратные слеши
    text = text.replace('\\', '')
    
    # Убираем фигурные скобки {} (оставшиеся после обработки)
    text = text.replace('{', '').replace('}', '')
    
    # Форматируем заголовки
    text = re.sub(r'^###\s*(.+)$', r'\n📌 \1\n', text, flags=re.MULTILINE)
    text = re.sub(r'^##\s*(.+)$', r'\n📍 \1\n', text, flags=re.MULTILINE)
    text = re.sub(r'^#\s*(.+)$', r'\n📢 \1\n', text, flags=re.MULTILINE)
    
    # Убираем жирный текст (двойные звездочки)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    
    # Форматируем списки
    text = re.sub(r'^[-*]\s+(.+)$', r'  • \1', text, flags=re.MULTILINE)
    
    # Возвращаем блоки кода
    for i, code_block in enumerate(code_blocks):
        text = text.replace(f"___CODE_BLOCK_{i}___", code_block)
    
    # Убираем лишние пустые строки
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()


def escape_markdown(text: str) -> str:
    """Экранировать специальные символы Markdown для безопасной отправки"""
    # Сохраняем блоки кода
    code_blocks = []
    def save_code(match):
        code_blocks.append(match.group(0))
        return f"___CODE_BLOCK_{len(code_blocks)-1}___"
    
    text = re.sub(r'```[\s\S]*?```', save_code, text)
    text = re.sub(r'`[^`]+`', save_code, text)
    
    # Сохраняем жирный текст
    bold_blocks = []
    def save_bold(match):
        bold_blocks.append(match.group(0))
        return f"___BOLD_BLOCK_{len(bold_blocks)-1}___"
    
    text = re.sub(r'\*[^*]+\*', save_bold, text)
    
    # Экранируем оставшиеся специальные символы
    special_chars = ['*', '_', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    
    # Возвращаем жирный текст
    for i, bold_block in enumerate(bold_blocks):
        text = text.replace(f"___BOLD_BLOCK_{i}___", bold_block)
    
    # Возвращаем блоки кода
    for i, code_block in enumerate(code_blocks):
        text = text.replace(f"___CODE_BLOCK_{i}___", code_block)
    
    return text


async def send_long_message(message: Message, text: str, force_file: bool = False):
    """Отправить длинное сообщение (разбивая на части или отправляя файлом)"""
    # Если пользователь явно попросил файл или сообщение очень длинное
    if force_file or len(text) > 10000:
        await send_as_file(message, text, "📄 Ответ слишком длинный, отправляю файлом" if not force_file else "📄 Ответ в файле")
    else:
        # Разбиваем на части как обычно
        parts = split_message(text)

        for i, part in enumerate(parts):
            if i > 0:
                await asyncio.sleep(0.5)
            try:
                # Проверяем, есть ли блоки кода в тексте
                if '```' in part:
                    # Если есть блоки кода, используем HTML форматирование
                    html_part = part
                    
                    # Функция для экранирования HTML внутри кода
                    def escape_html_in_code(match):
                        code_content = match.group(2) if match.lastindex >= 2 else match.group(1)
                        # Экранируем HTML символы
                        code_content = code_content.replace('&', '&amp;')
                        code_content = code_content.replace('<', '&lt;')
                        code_content = code_content.replace('>', '&gt;')
                        
                        if match.lastindex >= 2:
                            # Блок с языком
                            return f'<pre><code class="language-{match.group(1)}">{code_content}</code></pre>'
                        else:
                            # Блок без языка
                            return f'<pre>{code_content}</pre>'
                    
                    # Обрабатываем блоки кода с языком
                    html_part = re.sub(
                        r'```(\w+)\n([\s\S]*?)```',
                        escape_html_in_code,
                        html_part
                    )
                    
                    # Обрабатываем блоки кода без языка
                    html_part = re.sub(
                        r'```\n?([\s\S]*?)```',
                        escape_html_in_code,
                        html_part
                    )
                    
                    # Обрабатываем инлайн код `код`
                    def escape_inline_code(match):
                        code = match.group(1)
                        code = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                        return f'<code>{code}</code>'
                    
                    html_part = re.sub(r'`([^`]+)`', escape_inline_code, html_part)
                    
                    await message.answer(html_part, parse_mode='HTML')
                else:
                    # Если нет блоков кода, отправляем как обычный текст
                    await message.answer(part)
            except Exception as e:
                logging.error(f"Ошибка отправки сообщения с HTML: {e}")
                try:
                    # Пробуем без форматирования
                    await message.answer(part)
                except Exception as e2:
                    logging.error(f"Ошибка отправки без форматирования: {e2}")
                    try:
                        # В крайнем случае отправляем как текстовый файл
                        await send_as_file(message, part, "📄 Не удалось отправить сообщение, отправляю файлом")
                    except Exception as e3:
                        logging.error(f"Ошибка отправки файла: {e3}")
                except Exception as e2:
                    logging.error(f"Ошибка отправки без форматирования: {e2}")
                    # В крайнем случае отправляем как текстовый файл
                    await send_as_file(message, part, "📄 Не удалось отправить сообщение, отправляю файлом")


# === РАБОТА С AI ===
def sync_api_request(url: str, data: dict, headers: dict) -> dict:
    """Синхронный запрос к API используя requests (как в документации)"""
    try:
        # Логируем что отправляем
        logging.info(f"Sending request to: {url}")
        logging.info(f"Headers: {headers}")
        logging.info(f"Data: {data}")
        
        # Добавляем Content-Type явно
        headers_with_content_type = {**headers, "Content-Type": "application/json"}
        
        # Используем json= для автоматической сериализации
        response = requests.post(url, json=data, headers=headers_with_content_type, timeout=60)
        
        logging.info(f"Received status: {response.status_code}")
        logging.info(f"Response headers: {dict(response.headers)}")
        
        return {
            "status": response.status_code,
            "text": response.text,
            "json": response.json() if response.status_code == 200 else None
        }
    except requests.exceptions.Timeout:
        logging.error("API request timeout (60s)")
        return {
            "status": 0,
            "text": "⏱️ Запрос превысил время ожидания (60 сек). Попробуйте другую модель или повторите позже.",
            "json": None
        }
    except Exception as e:
        logging.error(f"Sync API request error: {e}")
        return {
            "status": 0,
            "text": str(e),
            "json": None
        }


async def get_ai_response(user_id: int, user_message: str) -> str:
    """Получить ответ от AI с историей"""
    
    # Получаем выбранную модель пользователя
    user_data = get_user_data(user_id)
    selected_model = user_data.get("selected_model", DEFAULT_MODEL)

    history = get_history(user_id, limit=20)
    history.append({
        "role": "user",
        "content": user_message
    })

    send = {
        "model": selected_model,
        "request": {
            "messages": history
        }
    }

    # Логирование для отладки
    logging.info(f"API_KEY: {API_KEY}")
    logging.info(f"Model: {selected_model}")

    try:
        # Используем requests в отдельном потоке (как в документации API)
        headers = {"Authorization": f"Bearer {API_KEY}"}
        
        # Выполняем синхронный запрос в executor
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, sync_api_request, API_URL, send, headers)
        
        logging.info(f"Response status: {result['status']}")
        logging.info(f"Response body: {result['text'][:500]}")
        
        if result['status'] == 200 and result['json']:
            data = result['json']
            ai_reply = data['choices'][0]['message']['content']

            save_message(user_id, "user", user_message)
            save_message(user_id, "assistant", ai_reply)

            return ai_reply
        elif result['status'] == 0:
            # Таймаут или ошибка соединения
            return result['text']  # Уже содержит понятное сообщение об ошибке
        else:
            return f"❌ Ошибка API: {result['status']}\n\nПопробуйте другую модель или повторите позже."
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        return f"❌ Ошибка соединения: {str(e)}"


async def generate_bot_code(prompt: str, bot_token: str, user_id: int, selected_model: str) -> str:
    """Сгенерировать код бота через AI"""
    
    # Получаем данные пользователя
    username = f"user_{user_id}"
    user_data = get_user_data(user_id, username)
    
    # Проверяем токены для выбранной модели
    model_tokens = user_data.get("model_tokens", {})
    current_balance = model_tokens.get(selected_model, 0)
    
    if current_balance <= 0:
        return None
    
    # Используем токен для генерации
    if not use_request(user_id, selected_model):
        return None

    system_prompt = f"""Создай код Telegram бота на Python с использованием aiogram 3.x.
Требования:
1. Бот должен соответствовать следующему описанию: {prompt}
2. Используй aiogram 3.x
3. Токен бота: {bot_token}
4. Код должен быть полным и готовым к запуску
5. Добавь базовый функционал и команду /start
6. Используй async/await
7. Верни ТОЛЬКО код Python без объяснений, без markdown разметки
8. Код должен начинаться с import и заканчиваться asyncio.run(main())"""

    send = {
        "model": selected_model,
        "request": {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Создай бота: {prompt}"}
            ]
        }
    }

    try:
        headers = {"Authorization": f"Bearer {API_KEY}"}
        
        # Используем requests в отдельном потоке
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, sync_api_request, API_URL, send, headers)
        
        if result['status'] == 200 and result['json']:
            data = result['json']
            code = data['choices'][0]['message']['content']

            # Очистка кода от markdown
            code = code.replace('```python', '').replace('```', '').strip()

            return code
        else:
            return None
    except Exception as e:
        logging.error(f"Ошибка генерации кода: {e}")
        return None


# === УПРАВЛЕНИЕ БОТАМИ ===
def start_bot_process(bot_id: str, user_id: int):
    """Запустить процесс бота"""
    bot_file = os.path.join(BOTS_DIR, f"bot_{user_id}_{bot_id}.py")

    if not os.path.exists(bot_file):
        return False

    try:
        # Для Windows используем python вместо python3 и без preexec_fn
        if os.name == 'nt':  # Windows
            process = subprocess.Popen(
                ["python", bot_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:  # Linux/Unix
            process = subprocess.Popen(
                ["python3", bot_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid
            )
        running_bots[bot_id] = process
        update_bot_status(user_id, bot_id, True)
        return True
    except Exception as e:
        logging.error(f"Ошибка запуска бота: {e}")
        return False


def stop_bot_process(bot_id: str, user_id: int):
    """Остановить процесс бота"""
    if bot_id in running_bots:
        try:
            process = running_bots[bot_id]
            if os.name == 'nt':  # Windows
                process.terminate()
            else:  # Linux/Unix
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            del running_bots[bot_id]
            update_bot_status(user_id, bot_id, False)
            return True
        except Exception as e:
            logging.error(f"Ошибка остановки бота: {e}")
            return False
    return False


# === КОМАНДЫ БОТА ===
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    # Сбрасываем любые активные состояния
    await state.clear()
    
    # Регистрируем пользователя
    username = message.from_user.username or f"user_{message.from_user.id}"
    user_data = get_user_data(message.from_user.id, username)
    
    await message.answer(
        "👋 *Привет!*\n\n"
        "Этот бот даёт вам доступ к лучшим AI-моделям для работы с текстом.\n\n"
        "🤖 *Доступные модели:*\n"
        "• Gemini 3 Pro\n"
        "• Gemini 3 Flash\n"
        "• DeepSeek V3\n"
        "• Grok 3\n"
        "• Sonar Deep Research\n\n"
        "✨ *Чатбот умеет:*\n"
        "• Писать и переводить тексты 📝\n"
        "• Работать с документами 🗂\n"
        "• Писать и править код ⌨️\n"
        "• Решать математические задачи 🧮\n"
        "• Распознавать текст с фото 🖌\n"
        "• Создавать статьи, эссе, рефераты 🎓\n"
        "• Анализировать и улучшать тексты ✍️\n\n"
        "📝 *ТЕКСТ:* просто напишите вопрос (выбор модели в /model)\n\n"
        "➡️ *РАБОТА С РЕПОСТАМИ:* перешлите сообщение боту для анализа, переписывания, создания статей\n\n"
        "👨‍👩‍👧‍👦 *РАБОТА В ГРУППАХ:* добавьте бота в группу и используйте /ask + ваш запрос\n\n"
        "🎨 *ДОПОЛНИТЕЛЬНО:*\n"
        "• Создание Telegram ботов 🤖\n"
        "• Управление вашими ботами 📋\n"
        "• /account посмотреть свой аккаунт👤\n\n"
        "Выберите действие:",
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )


@dp.message(F.text.contains("Создать бота"))
async def create_bot_start(message: Message, state: FSMContext):
    # Проверяем, включено ли создание ботов
    if not is_bot_creation_enabled():
        await message.answer(
            "⚠️ Создание ботов временно отключено администратором.\n\n"
            "Попробуйте позже или обратитесь в поддержку.",
            reply_markup=get_main_keyboard()
        )
        return
    
    await state.set_state(BotCreation.waiting_for_token)
    await message.answer(
        "🔑 Отправьте API токен вашего бота\n\n"
        "Получить токен можно у @BotFather"
    )


@dp.message(F.text.contains("Мои боты"))
async def show_my_bots_button(message: Message, state: FSMContext):
    # Сбрасываем любые активные состояния
    await state.clear()
    
    logging.info(f"User {message.from_user.id} requested bot list")
    bots = get_user_bots(message.from_user.id)
    logging.info(f"Found {len(bots)} bots for user {message.from_user.id}")

    if not bots:
        await message.answer(
            "У вас пока нет ботов.\n"
            "Создайте первого бота!",
            reply_markup=get_main_keyboard()
        )
        return

    text = "🤖 Ваши боты:\n\n"
    buttons = []

    for i, bot_data in enumerate(bots, 1):
        status = "🟢 Работает" if bot_data.get("is_running", False) else "🔴 Остановлен"
        prompt_short = bot_data['prompt'][:50] + "..." if len(bot_data['prompt']) > 50 else bot_data['prompt']
        
        # Получаем название модели
        bot_model = bot_data.get("model", DEFAULT_MODEL)
        model_name = AVAILABLE_MODELS.get(bot_model, {}).get("name", bot_model)
        
        text += f"{i}. {status}\n🎯 {model_name}\n📝 {prompt_short}\n\n"

        buttons.append([InlineKeyboardButton(
            text=f"Бот #{i}",
            callback_data=f"manage_{bot_data['bot_id']}"
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, reply_markup=keyboard)


@dp.message(F.text.contains("Чат с AI"))
async def ai_chat_mode_button(message: Message, state: FSMContext):
    # Сбрасываем любые активные состояния
    await state.clear()
    
    # Регистрируем пользователя
    username = message.from_user.username or f"user_{message.from_user.id}"
    user_data = get_user_data(message.from_user.id, username)
    selected_model = user_data.get("selected_model", DEFAULT_MODEL)
    model_name = AVAILABLE_MODELS[selected_model]["name"] if selected_model in AVAILABLE_MODELS else selected_model
    
    # Получаем токены для текущей модели
    model_tokens = user_data.get("model_tokens", {})
    current_balance = model_tokens.get(selected_model, 0)
    
    await message.answer(
        f"👋 Привет! Я AI-ассистент.\n\n"
        f"🤖 Текущая модель: *{model_name}*\n\n"
        "Ты можешь задать любой вопрос или попросить помочь что-то решить. "
        "Я могу помочь с программированием, написанием текстов, объяснением сложных тем, "
        "переводом, решением задач и многим другим!\n\n"
        "Команды:\n"
        "/model - выбрать модель AI\n"
        "/account - проверить баланс\n"
        "/clear - очистить историю\n"
        "/history - показать историю\n\n"
        f"📊 Токенов для текущей модели: {current_balance}",
        parse_mode='Markdown'
    )




@dp.message(F.text.contains("Выбрать модель"))
async def select_model_button(message: Message):
    """Кнопка выбора модели"""
    await cmd_model(message)


@dp.message(BotCreation.waiting_for_token)
async def process_token(message: Message, state: FSMContext):
    # Если пользователь нажал кнопку меню - сбрасываем состояние и вызываем соответствующий обработчик
    if message.text and ("Создать бота" in message.text or "Мои боты" in message.text or "Чат с AI" in message.text or "Выбрать модель" in message.text):
        await state.clear()
        
        # Вызываем соответствующий обработчик
        if "Чат с AI" in message.text:
            await ai_chat_mode_button(message, state)
        elif "Выбрать модель" in message.text:
            await select_model_button(message)
        elif "Мои боты" in message.text:
            await show_my_bots_button(message, state)
        elif "Создать бота" in message.text:
            await create_bot_start(message, state)
        return
    
    token = message.text.strip()

    # Улучшенная валидация токена Telegram
    # Формат: XXXXXXXXX:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
    # Первая часть - bot ID (цифры), вторая часть - секретный ключ (буквы, цифры, дефисы, подчеркивания)
    if not token or ':' not in token:
        await message.answer("❌ Неверный формат токена. Попробуйте снова:")
        return
    
    parts = token.split(':')
    if len(parts) != 2:
        await message.answer("❌ Неверный формат токена. Попробуйте снова:")
        return
    
    bot_id_part, secret_part = parts
    
    # Проверяем первую часть (должна быть числом)
    if not bot_id_part.isdigit():
        await message.answer("❌ Неверный формат токена. Попробуйте снова:")
        return
    
    # Проверяем длину второй части (должна быть достаточно длинной)
    if len(secret_part) < 30:
        await message.answer("❌ Неверный формат токена. Попробуйте снова:")
        return

    await state.update_data(token=token)
    await state.set_state(BotCreation.waiting_for_prompt)
    await message.answer(
        "📝 Отлично! Теперь опишите, что должен делать ваш бот.\n\n"
        "Например:\n"
        "- Простой эхо-бот\n"
        "- Бот для заметок\n"
        "- Бот-калькулятор\n"
        "- Бот с кнопками для голосования"
    )


@dp.message(BotCreation.waiting_for_prompt)
async def process_prompt(message: Message, state: FSMContext):
    # Если пользователь нажал кнопку меню - сбрасываем состояние и вызываем соответствующий обработчик
    if message.text and ("Создать бота" in message.text or "Мои боты" in message.text or "Чат с AI" in message.text or "Выбрать модель" in message.text):
        await state.clear()
        
        # Вызываем соответствующий обработчик
        if "Чат с AI" in message.text:
            await ai_chat_mode_button(message, state)
        elif "Выбрать модель" in message.text:
            await select_model_button(message)
        elif "Мои боты" in message.text:
            await show_my_bots_button(message, state)
        elif "Создать бота" in message.text:
            await create_bot_start(message, state)
        return
    
    prompt = message.text
    data = await state.get_data()
    token = data['token']
    
    # Получаем выбранную модель пользователя
    username = message.from_user.username or f"user_{message.from_user.id}"
    user_data = get_user_data(message.from_user.id, username)
    selected_model = user_data.get("selected_model", DEFAULT_MODEL)
    
    # Проверяем токены для выбранной модели
    model_tokens = user_data.get("model_tokens", {})
    current_balance = model_tokens.get(selected_model, 0)
    
    if current_balance <= 0:
        model_name = AVAILABLE_MODELS.get(selected_model, {}).get("name", selected_model)
        await message.answer(
            f"❌ Недостаточно токенов для модели {model_name}\n\n"
            f"📊 Текущий баланс: {current_balance} токенов\n\n"
            "Используйте /model чтобы выбрать другую модель или обратитесь к администратору для пополнения баланса."
        )
        await state.clear()
        return

    status_msg = await message.answer("⏳ Создаю бота... Это может занять минуту.")

    # Генерируем код бота
    bot_code = await generate_bot_code(prompt, token, message.from_user.id, selected_model)

    if not bot_code:
        await status_msg.edit_text(
            "❌ Ошибка при генерации кода бота\n\n"
            "Возможные причины:\n"
            "• Недостаточно токенов для выбранной модели\n"
            "• Проблема с API\n\n"
            "Попробуйте позже или выберите другую модель через /model"
        )
        await state.clear()
        return

    # Создаем уникальный ID бота
    bot_id = f"{message.from_user.id}_{datetime.now().timestamp()}"
    bot_file = os.path.join(BOTS_DIR, f"bot_{message.from_user.id}_{bot_id}.py")

    # Сохраняем код бота
    with open(bot_file, 'w', encoding='utf-8') as f:
        f.write(bot_code)

    # Устанавливаем зависимости
    await status_msg.edit_text("📦 Устанавливаю зависимости...")

    try:
        subprocess.run(
            ["pip", "install", "-q", "aiogram", "aiohttp"],
            check=True,
            capture_output=True
        )
    except:
        pass  # Зависимости уже установлены

    # Сохраняем в базу
    add_bot(message.from_user.id, token, prompt, bot_id, selected_model)

    await status_msg.edit_text(
        "✅ Бот успешно создан!\n\n"
        "Ваш бот готов к запуску.\n"
        "Используйте кнопку 'Мои боты' для управления."
    )

    # Отправляем клавиатуру отдельным сообщением
    await message.answer("Выберите действие:", reply_markup=get_main_keyboard())

    await state.clear()


@dp.callback_query(F.data.startswith("manage_"))
async def manage_bot(callback: CallbackQuery):
    bot_id = callback.data.split("_", 1)[1]
    bot_data = get_bot_data(callback.from_user.id, bot_id)

    if not bot_data:
        await callback.answer("Бот не найден")
        return

    is_running = bot_data.get("is_running", False)
    status = "🟢 Работает" if is_running else "🔴 Остановлен"
    
    # Получаем название модели
    bot_model = bot_data.get("model", DEFAULT_MODEL)
    model_name = AVAILABLE_MODELS.get(bot_model, {}).get("name", bot_model)

    text = f"🤖 Управление ботом\n\n"
    text += f"Статус: {status}\n"
    text += f"🎯 Модель: {model_name}\n"
    text += f"📝 Описание: {bot_data['prompt']}\n"
    text += f"📅 Создан: {bot_data['created_at'][:10]}"

    await callback.message.edit_text(
        text,
        reply_markup=get_bot_management_keyboard(bot_id, is_running)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("start_"))
async def start_bot(callback: CallbackQuery):
    bot_id = callback.data.split("_", 1)[1]

    if start_bot_process(bot_id, callback.from_user.id):
        await callback.answer("✅ Бот запущен!")

        # Обновляем сообщение
        bot_data = get_bot_data(callback.from_user.id, bot_id)
        text = f"🤖 Управление ботом\n\n"
        text += f"Статус: 🟢 Работает\n"
        text += f"📝 Описание: {bot_data['prompt']}\n"
        text += f"📅 Создан: {bot_data['created_at'][:10]}"

        await callback.message.edit_text(
            text,
            reply_markup=get_bot_management_keyboard(bot_id, True)
        )
    else:
        await callback.answer("❌ Ошибка запуска бота")


@dp.callback_query(F.data.startswith("stop_"))
async def stop_bot(callback: CallbackQuery):
    bot_id = callback.data.split("_", 1)[1]

    if stop_bot_process(bot_id, callback.from_user.id):
        await callback.answer("⏹️ Бот остановлен!")

        # Обновляем сообщение
        bot_data = get_bot_data(callback.from_user.id, bot_id)
        text = f"🤖 Управление ботом\n\n"
        text += f"Статус: 🔴 Остановлен\n"
        text += f"📝 Описание: {bot_data['prompt']}\n"
        text += f"📅 Создан: {bot_data['created_at'][:10]}"

        await callback.message.edit_text(
            text,
            reply_markup=get_bot_management_keyboard(bot_id, False)
        )
    else:
        await callback.answer("❌ Ошибка остановки бота")


@dp.callback_query(F.data.startswith("edit_"))
async def edit_bot_start(callback: CallbackQuery, state: FSMContext):
    bot_id = callback.data.split("_", 1)[1]
    
    # Получаем данные бота
    bot_data = get_bot_data(callback.from_user.id, bot_id)
    
    if not bot_data:
        await callback.answer("❌ Бот не найден")
        return
    
    # Получаем модель бота
    bot_model = bot_data.get("model", DEFAULT_MODEL)
    
    # Проверяем токены для модели
    username = callback.from_user.username or f"user_{callback.from_user.id}"
    user_data = get_user_data(callback.from_user.id, username)
    model_tokens = user_data.get("model_tokens", {})
    current_balance = model_tokens.get(bot_model, 0)
    
    if current_balance <= 0:
        model_name = AVAILABLE_MODELS.get(bot_model, {}).get("name", bot_model)
        await callback.message.answer(
            f"❌ Недостаточно токенов для редактирования\n\n"
            f"🎯 Модель бота: {model_name}\n"
            f"📊 Текущий баланс: {current_balance} токенов\n\n"
            "Для редактирования бота нужны токены модели, которая использовалась при его создании.\n"
            "Обратитесь к администратору для пополнения баланса."
        )
        await callback.answer()
        return

    await state.update_data(bot_id=bot_id)
    await state.set_state(BotEdit.waiting_for_changes)

    await callback.message.answer(
        "✏️ Опишите, какие изменения нужно внести в бота:"
    )
    await callback.answer()


@dp.message(BotEdit.waiting_for_changes)
async def process_bot_edit(message: Message, state: FSMContext):
    # Если пользователь нажал кнопку меню - сбрасываем состояние и вызываем соответствующий обработчик
    if message.text and ("Создать бота" in message.text or "Мои боты" in message.text or "Чат с AI" in message.text or "Выбрать модель" in message.text):
        await state.clear()
        
        # Вызываем соответствующий обработчик
        if "Чат с AI" in message.text:
            await ai_chat_mode_button(message, state)
        elif "Выбрать модель" in message.text:
            await select_model_button(message)
        elif "Мои боты" in message.text:
            await show_my_bots_button(message, state)
        elif "Создать бота" in message.text:
            await create_bot_start(message, state)
        return
    
    data = await state.get_data()
    bot_id = data['bot_id']
    changes = message.text

    bot_data = get_bot_data(message.from_user.id, bot_id)

    if not bot_data:
        await message.answer("❌ Бот не найден")
        await state.clear()
        return
    
    # Получаем модель бота
    bot_model = bot_data.get("model", DEFAULT_MODEL)
    
    # Получаем данные пользователя и проверяем токены
    username = message.from_user.username or f"user_{message.from_user.id}"
    user_data = get_user_data(message.from_user.id, username)
    model_tokens = user_data.get("model_tokens", {})
    current_balance = model_tokens.get(bot_model, 0)
    
    if current_balance <= 0:
        model_name = AVAILABLE_MODELS.get(bot_model, {}).get("name", bot_model)
        await message.answer(
            f"❌ Недостаточно токенов для модели {model_name}\n\n"
            f"📊 Текущий баланс: {current_balance} токенов\n\n"
            "Этот бот использует модель, для которой у вас закончились токены.\n"
            "Обратитесь к администратору для пополнения баланса."
        )
        await state.clear()
        return

    # Останавливаем бота если он запущен
    if bot_data.get("is_running", False):
        stop_bot_process(bot_id, message.from_user.id)

    status_msg = await message.answer("⏳ Пересоздаю бота с новыми правками...")

    # Новый промпт с изменениями
    new_prompt = f"{bot_data['prompt']}\n\nДополнительные изменения: {changes}"

    # Генерируем новый код с проверкой токенов
    bot_code = await generate_bot_code(new_prompt, bot_data['token'], message.from_user.id, bot_model)

    if not bot_code:
        await status_msg.edit_text(
            "❌ Ошибка при генерации кода\n\n"
            "Возможные причины:\n"
            "• Недостаточно токенов для выбранной модели\n"
            "• Проблема с API\n\n"
            "Попробуйте позже."
        )
        await state.clear()
        return

    # Перезаписываем файл бота
    bot_file = os.path.join(BOTS_DIR, f"bot_{message.from_user.id}_{bot_id}.py")
    with open(bot_file, 'w', encoding='utf-8') as f:
        f.write(bot_code)

    # Обновляем промпт в базе
    update_bot_prompt(message.from_user.id, bot_id, new_prompt)

    await status_msg.edit_text(
        "✅ Бот успешно обновлен!\n\n"
        "Изменения применены. Запустите бота заново."
    )

    # Отправляем клавиатуру отдельным сообщением
    await message.answer("Выберите действие:", reply_markup=get_main_keyboard())

    await state.clear()


@dp.callback_query(F.data.startswith("delete_"))
async def delete_bot(callback: CallbackQuery):
    bot_id = callback.data.split("_", 1)[1]

    # Останавливаем бота если запущен
    bot_data = get_bot_data(callback.from_user.id, bot_id)
    if bot_data and bot_data.get("is_running", False):
        stop_bot_process(bot_id, callback.from_user.id)

    # Удаляем файл
    bot_file = os.path.join(BOTS_DIR, f"bot_{callback.from_user.id}_{bot_id}.py")
    if os.path.exists(bot_file):
        os.remove(bot_file)

    # Удаляем из базы
    delete_bot_from_db(callback.from_user.id, bot_id)

    await callback.message.edit_text(
        "🗑️ Бот успешно удален!",
        reply_markup=None
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("deps_"))
async def show_bot_dependencies(callback: CallbackQuery):
    """Показать зависимости бота"""
    bot_id = callback.data.split("_", 1)[1]
    
    bot_data = get_bot_data(callback.from_user.id, bot_id)
    
    if not bot_data:
        await callback.answer("❌ Бот не найден")
        return
    
    # Читаем код бота
    bot_file = os.path.join(BOTS_DIR, f"bot_{callback.from_user.id}_{bot_id}.py")
    
    if not os.path.exists(bot_file):
        await callback.answer("❌ Файл бота не найден")
        return
    
    with open(bot_file, 'r', encoding='utf-8') as f:
        code = f.read()
    
    # Ищем импорты в коде
    import_lines = [line.strip() for line in code.split('\n') if line.strip().startswith('import ') or line.strip().startswith('from ')]
    
    # Базовые зависимости
    dependencies = []
    dependencies.append("aiogram>=3.0.0")
    dependencies.append("aiohttp")
    
    # Дополнительные зависимости на основе импортов
    additional_deps = []
    if any('requests' in line for line in import_lines):
        additional_deps.append("requests")
    if any('pillow' in line.lower() or 'pil' in line for line in import_lines):
        additional_deps.append("pillow")
    if any('numpy' in line for line in import_lines):
        additional_deps.append("numpy")
    if any('pandas' in line for line in import_lines):
        additional_deps.append("pandas")
    
    # Встроенные модули
    builtin = []
    if any('sqlite3' in line for line in import_lines):
        builtin.append("sqlite3")
    if any('json' in line for line in import_lines):
        builtin.append("json")
    if any('datetime' in line for line in import_lines):
        builtin.append("datetime")
    if any('os' in line for line in import_lines):
        builtin.append("os")
    
    text = "📦 Зависимости бота:\n\n"
    text += "🔹 Обязательные:\n"
    for dep in dependencies:
        text += f"  • {dep}\n"
    
    if additional_deps:
        text += "\n🔸 Дополнительные:\n"
        for dep in additional_deps:
            text += f"  • {dep}\n"
    
    if builtin:
        text += "\n✅ Встроенные (не требуют установки):\n"
        for dep in builtin:
            text += f"  • {dep}\n"
    
    # Команда для установки
    all_deps = dependencies + additional_deps
    text += f"\n💻 Команда для установки:\n`pip install {' '.join(all_deps)}`"
    
    await callback.message.answer(text, parse_mode='Markdown')
    await callback.answer()


@dp.callback_query(F.data.startswith("download_"))
async def download_bot_code(callback: CallbackQuery):
    """Скачать код бота"""
    bot_id = callback.data.split("_", 1)[1]
    
    bot_data = get_bot_data(callback.from_user.id, bot_id)
    
    if not bot_data:
        await callback.answer("❌ Бот не найден")
        return
    
    # Читаем файл бота
    bot_file = os.path.join(BOTS_DIR, f"bot_{callback.from_user.id}_{bot_id}.py")
    
    if not os.path.exists(bot_file):
        await callback.answer("❌ Файл бота не найден")
        return
    
    # Отправляем файл
    try:
        file = FSInputFile(bot_file, filename=f"bot_{bot_id}.py")
        
        await callback.message.answer_document(
            file,
            caption=f"💾 Код вашего бота\n\n📝 {bot_data['prompt'][:100]}"
        )
        await callback.answer("✅ Файл отправлен")
    except Exception as e:
        logging.error(f"Ошибка отправки файла: {e}")
        await callback.answer("❌ Ошибка отправки файла")


@dp.callback_query(F.data == "back_to_bots")
async def back_to_bots(callback: CallbackQuery):
    bots = get_user_bots(callback.from_user.id)

    text = "🤖 Ваши боты:\n\n"
    buttons = []

    for i, bot_data in enumerate(bots, 1):
        status = "🟢 Работает" if bot_data.get("is_running", False) else "🔴 Остановлен"
        prompt_short = bot_data['prompt'][:50] + "..." if len(bot_data['prompt']) > 50 else bot_data['prompt']
        
        # Получаем название модели
        bot_model = bot_data.get("model", DEFAULT_MODEL)
        model_name = AVAILABLE_MODELS.get(bot_model, {}).get("name", bot_model)
        
        text += f"{i}. {status}\n🎯 {model_name}\n📝 {prompt_short}\n\n"

        buttons.append([InlineKeyboardButton(
            text=f"Бот #{i}",
            callback_data=f"manage_{bot_data['bot_id']}"
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


async def cmd_clear(message: Message):
    clear_history(message.from_user.id)
    await message.answer("🗑️ История очищена!")


@dp.message(F.text == "/account")
async def cmd_account(message: Message):
    """Показать информацию об аккаунте"""
    username = message.from_user.username or f"user_{message.from_user.id}"
    user_data = get_user_data(message.from_user.id, username)
    
    user_id = message.from_user.id
    total_requests = user_data["total_requests"]
    last_reset = datetime.fromisoformat(user_data["last_reset"])
    next_reset = last_reset + timedelta(hours=24)
    time_left = next_reset - datetime.now()
    hours = int(time_left.total_seconds() // 3600)
    minutes = int((time_left.total_seconds() % 3600) // 60)
    selected_model = user_data.get("selected_model", DEFAULT_MODEL)
    model_name = AVAILABLE_MODELS[selected_model]["name"] if selected_model in AVAILABLE_MODELS else selected_model
    
    # Получаем токены для каждой модели
    model_tokens = user_data.get("model_tokens", {})
    
    # Получаем количество ботов
    user_bots = user_data.get("bots", [])
    bots_count = len(user_bots)
    
    # Формируем красивый вывод
    text = (
        f"👤 *ID Пользователя:* `{user_id}`\n"
        f"⭐️ *Тип подписки:* 🆓 Free\n"
        f"📅 *Действует до:* -\n"
        f"💳 *Метод оплаты:* -\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"🤖 *Токены по моделям:*\n"
        f"  • {AVAILABLE_MODELS['gpt-4o-mini']['name']}: {model_tokens.get('gpt-4o-mini', 0)}\n"
        f"  • {AVAILABLE_MODELS['gemini-3-pro']['name']}: {model_tokens.get('gemini-3-pro', 0)}\n"
        f"  • {AVAILABLE_MODELS['gemini-3-pro-preview']['name']}: {model_tokens.get('gemini-3-pro-preview', 0)}\n"
        f"  • {AVAILABLE_MODELS['deepseek-v3']['name']}: {model_tokens.get('deepseek-v3', 0)}\n"
        f"  • {AVAILABLE_MODELS['grok-3']['name']}: {model_tokens.get('grok-3', 0)}\n"
        f"  • {AVAILABLE_MODELS['sonar-deep-research']['name']}: {model_tokens.get('sonar-deep-research', 0)}\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"📊 *Всего использовано:* {total_requests}\n"
        f"🤖 *Созданных ботов:* {bots_count}\n"
        f"⏰ *Лимит обновится через:* {hours} ч. {minutes} мин.\n"
        f"\n"
        f"✅ *Текущая модель:* {model_name}"
    )
        
    # Создаем кнопки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨‍💼 Техподдержка", url="https://t.me/nxtalent")]
    ])
    
    await message.answer(text, reply_markup=keyboard, parse_mode='Markdown')
@dp.message(F.text == "/model")
async def cmd_model(message: Message):
    """Выбрать модель AI"""
    username = message.from_user.username or f"user_{message.from_user.id}"
    user_data = get_user_data(message.from_user.id, username)
    current_model = user_data.get("selected_model", DEFAULT_MODEL)
    # Получаем токены для текущей модели
    model_tokens = user_data.get("model_tokens", {})
    current_balance = model_tokens.get(current_model, 0)
    
    # Создаем кнопки для выбора модели
    buttons = []
    for model_id, model_info in AVAILABLE_MODELS.items():
        model_name = model_info["name"]
        model_cost = model_info.get("cost", 1)
        
        # Добавляем замок если недостаточно токенов
        lock = "🔒 " if current_balance < model_cost else ""
        emoji = "✅ " if model_id == current_model else lock
        
        buttons.append([InlineKeyboardButton(
            text=f"{emoji}{model_name}",
            callback_data=f"model_{model_id}"
        )])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await message.answer(
        "🤖 *Выберите модель AI:*\n\n"
        "*⭐️ Gemini 3 Pro* - Флагманская модель от Google DeepMind для сложных задач.\n\n"
        "*👽 Gemini 3 Flash* - Быстрая модель от Google для чата и текстов.\n\n"
        "*🐼 DeepSeek V3* - Мощная модель для кода и технических задач.\n\n"
        "*🚀 Grok 3* - Модель от xAI с доступом к актуальным данным.\n\n"
        "*🔍 Sonar Deep Research* - Для глубокого анализа и исследований.\n\n"
        "*⚡️ GPT-4o Mini* - Быстрая и эффективная модель от OpenAI.\n\n"
        "⚠️ *Примечание:* Модели могут представляться под другими именами - это нормально для прокси-API.\n\n"
        "Модели с 🔒 недоступны (не хватает токенов).",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )


@dp.callback_query(F.data.startswith("model_"))
async def select_model(callback: CallbackQuery):
    """Обработка выбора модели"""
    model_id = callback.data.replace("model_", "")
    
    if model_id not in AVAILABLE_MODELS:
        await callback.answer("❌ Неизвестная модель", show_alert=True)
        return
    
    # Проверяем доступность модели
    username = callback.from_user.username or f"user_{callback.from_user.id}"
    user_data = get_user_data(callback.from_user.id, username)
    model_cost = AVAILABLE_MODELS[model_id].get("cost", 1)
    
    # Получаем токены для выбираемой модели
    model_tokens = user_data.get("model_tokens", {})
    model_balance = model_tokens.get(model_id, 0)
    
    if model_balance < model_cost:
        await callback.answer(
            f"🔒 Недостаточно токенов для этой модели\n\n"
            f"Требуется: {model_cost} токенов\n"
            f"У вас: {model_balance} токенов",
            show_alert=True
        )
        return
    
    # Проверяем, не выбрана ли уже эта модель
    if user_data.get("selected_model") == model_id:
        await callback.answer(f"ℹ️ Эта модель уже выбрана", show_alert=False)
        return
    
    set_user_model(callback.from_user.id, model_id)
    model_name = AVAILABLE_MODELS[model_id]["name"]
    model_desc = AVAILABLE_MODELS[model_id]["desc"]
    
    await callback.answer(f"✅ Выбрана модель {model_name}", show_alert=False)
    
    # Обновляем кнопки
    current_model = model_id
    model_tokens = user_data.get("model_tokens", {})
    
    buttons = []
    for mid, minfo in AVAILABLE_MODELS.items():
        mname = minfo["name"]
        mcost = minfo.get("cost", 1)
        lock = "🔒 " if model_tokens.get(mid, 0) < mcost else ""
        emoji = "✅ " if mid == current_model else lock
        
        buttons.append([InlineKeyboardButton(
            text=f"{emoji}{mname}",
            callback_data=f"model_{mid}"
        )])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    # Обновляем сообщение с новой информацией о выбранной модели
    try:
        await callback.message.edit_text(
            "🤖 *Выберите модель AI:*\n\n"
            "*⭐️ Gemini 3 Pro* - Флагманская модель от Google DeepMind для сложных задач.\n\n"
            "*👽 Gemini 3 Flash* - Быстрая модель от Google для чата и текстов.\n\n"
            "*🐼 DeepSeek V3* - Мощная модель для кода и технических задач.\n\n"
            "*🚀 Grok 3* - Модель от xAI с доступом к актуальным данным.\n\n"
            "*🔍 Sonar Deep Research* - Для глубокого анализа и исследований.\n\n"
            "Модели с 🔒 недоступны (не хватает токенов).\n\n"
            f"✅ *Текущая модель:* {model_name}",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
    except:
        # Если не удалось обновить текст, просто обновляем кнопки
        await callback.message.edit_reply_markup(reply_markup=keyboard)


@dp.message(F.text == "/history")
async def cmd_history(message: Message):
    history = get_history(message.from_user.id, limit=10)

    if not history:
        await message.answer("📭 История пуста")
        return

    text = "📚 Последние 10 сообщений:\n\n"
    for msg in history:
        role = "👤" if msg["role"] == "user" else "🤖"
        content = msg["content"][:50] + "..." if len(msg["content"]) > 50 else msg["content"]
        text += f"{role} {content}\n\n"

    await message.answer(text)


@dp.message(F.text.startswith("/ask "))
async def cmd_ask(message: Message):
    """Команда для работы в группах"""
    # Извлекаем текст после /ask
    query = message.text[5:].strip()
    
    if not query:
        await message.answer("Использование: /ask ваш вопрос")
        return
    
    # Регистрируем пользователя
    username = message.from_user.username or f"user_{message.from_user.id}"
    user_data = get_user_data(message.from_user.id, username)
    
    # Проверяем лимит
    user_data = get_user_data(message.from_user.id, username)
    selected_model = user_data.get("selected_model", DEFAULT_MODEL)
    if not use_request(message.from_user.id, selected_model):
        await message.answer(f"❌ Лимит запросов для модели {AVAILABLE_MODELS[selected_model]['name']} исчерпан. Используйте /balance для проверки.")
        return
    
    thinking_msg = await message.answer("💭 Думаю...")
    
    ai_response = await get_ai_response(message.from_user.id, query)
    ai_response = format_ai_response(ai_response)
    
    await thinking_msg.delete()
    await send_long_message(message, ai_response)


@dp.message(F.forward_date)
async def handle_forward(message: Message):
    """Обработка пересланных сообщений"""
    username = message.from_user.username or f"user_{message.from_user.id}"
    user_data = get_user_data(message.from_user.id, username)
    
    # Извлекаем текст из пересланного сообщения
    forwarded_text = message.text or message.caption or ""
    
    if not forwarded_text:
        await message.answer("❌ Не могу обработать это сообщение. Перешлите текстовое сообщение.")
        return
    
    # Создаем кнопки для выбора действия
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Переписать", callback_data=f"fwd_rewrite")],
        [InlineKeyboardButton(text="📊 Анализ", callback_data=f"fwd_analyze")],
        [InlineKeyboardButton(text="📰 Создать статью", callback_data=f"fwd_article")],
        [InlineKeyboardButton(text="✍️ Улучшить текст", callback_data=f"fwd_improve")]
    ])
    
    # Сохраняем текст во временное хранилище (можно использовать state или базу)
    # Для простоты сохраним в user_data
    db = load_database_users()
    user_id_str = str(message.from_user.id)
    if user_id_str in db:
        db[user_id_str]["last_forwarded"] = forwarded_text
        save_database_users(db)
    
    await message.answer(
        f"📨 Получено сообщение ({len(forwarded_text)} символов)\n\n"
        f"Выберите действие:",
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("fwd_"))
async def process_forward_action(callback: CallbackQuery):
    """Обработка действий с пересланным сообщением"""
    action = callback.data.replace("fwd_", "")
    
    # Получаем сохраненный текст
    db = load_database_users()
    user_id_str = str(callback.from_user.id)
    
    if user_id_str not in db or "last_forwarded" not in db[user_id_str]:
        await callback.answer("❌ Текст не найден. Перешлите сообщение заново.")
        return
    
    forwarded_text = db[user_id_str]["last_forwarded"]
    
    # Проверяем лимит
    username = callback.from_user.username or f"user_{callback.from_user.id}"
    user_data = get_user_data(callback.from_user.id, username)
    selected_model = user_data.get("selected_model", DEFAULT_MODEL)
    if not use_request(callback.from_user.id, selected_model):
        await callback.answer("❌ Лимит запросов исчерпан")
        return
    
    # Формируем запрос в зависимости от действия
    prompts = {
        "rewrite": f"Перепиши следующий текст, сохраняя смысл но изменяя формулировки:\n\n{forwarded_text}",
        "analyze": f"Проанализируй следующий текст (тема, тон, ключевые моменты, выводы):\n\n{forwarded_text}",
        "article": f"Создай полноценную статью на основе этого текста:\n\n{forwarded_text}",
        "improve": f"Улучши этот текст (грамматика, стиль, структура):\n\n{forwarded_text}"
    }
    
    await callback.message.edit_text("💭 Обрабатываю...")
    
    ai_response = await get_ai_response(callback.from_user.id, prompts[action])
    ai_response = format_ai_response(ai_response)
    
    await callback.message.delete()
    await send_long_message(callback.message, ai_response)
    await callback.answer()


@dp.message(F.text == "/admin")
async def cmd_admin(message: Message):
    """Админ-панель"""
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У вас нет доступа к админ-панели")
        return
    
    stats = get_bot_stats()
    
    # Проверяем статус создания ботов
    bot_creation_status = "✅ Включено" if is_bot_creation_enabled() else "❌ Отключено"
    bot_creation_button_text = "🔴 Отключить создание ботов" if is_bot_creation_enabled() else "🟢 Включить создание ботов"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Все пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="➕ Выдать токены", callback_data="admin_add_tokens")],
        [InlineKeyboardButton(text="⚙️ Лимиты моделей", callback_data="admin_change_limit")],
        [InlineKeyboardButton(text=bot_creation_button_text, callback_data="admin_toggle_bot_creation")],
        [InlineKeyboardButton(text="🌐 Проверить API", callback_data="admin_check_api")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="💾 Экспорт БД", callback_data="admin_export_db")]
    ])
    
    text = (
        "� Админ-панель\n\n"
        f"👥 Всего пользователей: {stats['total_users']}\n"
        f"✅ Активных: {stats['active_users']}\n"
        f"📨 Всего запросов: {stats['total_requests']}\n"
        f"🤖 Создание ботов: {bot_creation_status}"
    )
    
    await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data == "admin_check_api")
async def admin_check_api(callback: CallbackQuery):
    """Проверить статус API"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    await callback.message.edit_text("🔄 Проверяю API...")
    
    try:
        # Пробуем отправить тестовый запрос к API
        test_data = {
            "model": "gpt-4o-mini",
            "request": {
                "messages": [
                    {"role": "user", "content": "test"}
                ]
            }
        }
        
        logging.info(f"Testing API with key: {API_KEY}")
        
        headers = {"Authorization": f"Bearer {API_KEY}"}
        
        # Используем requests в отдельном потоке
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, sync_api_request, API_URL, test_data, headers)
        
        status_code = result['status']
        response_text = result['text']
        
        logging.info(f"API Response status: {status_code}")
        logging.info(f"API Response body: {response_text[:200]}")
        
        if status_code == 200:
            status_text = "✅ API работает нормально"
            status_emoji = "🟢"
        elif status_code == 401:
            status_text = f"⚠️ Ошибка авторизации (401)\n{response_text[:100]}"
            status_emoji = "🟡"
        elif status_code == 403:
            status_text = f"⚠️ Доступ запрещен (403)\n{response_text[:100]}"
            status_emoji = "🟡"
        elif status_code == 429:
            status_text = "⚠️ Превышен лимит запросов (429)"
            status_emoji = "🟡"
        elif status_code >= 500:
            status_text = f"❌ Ошибка сервера ({status_code})"
            status_emoji = "🔴"
        else:
            status_text = f"⚠️ Неизвестный статус ({status_code})\n{response_text[:100]}"
            status_emoji = "🟡"
        
        response_time = "< 1 сек"
                
    except asyncio.TimeoutError:
        status_text = "❌ Таймаут (API не отвечает)"
        status_emoji = "🔴"
        response_time = "> 10 сек"
    except Exception as e:
        status_text = f"❌ Ошибка подключения: {str(e)[:50]}"
        status_emoji = "🔴"
        response_time = "N/A"
    
    text = (
        f"{status_emoji} Статус API\n\n"
        f"🌐 URL: {API_URL}\n"
        f"🔑 API Key: {API_KEY}\n"
        f"📊 Статус: {status_text}\n"
        f"⏱ Время ответа: {response_time}\n"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить снова", callback_data="admin_check_api")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "admin_export_db")
async def admin_export_database(callback: CallbackQuery):
    """Экспорт базы данных"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    await callback.answer("📦 Готовлю файлы...")
    
    try:
        # Отправляем database.json
        if os.path.exists(DATABASE_FILE):
            file = FSInputFile(DATABASE_FILE)
            await callback.message.answer_document(file, caption="📦 database.json")
        
        # Отправляем chat_history.json
        if os.path.exists(DB_FILE):
            file2 = FSInputFile(DB_FILE)
            await callback.message.answer_document(file2, caption="📦 chat_history.json")
        
        # Отправляем bot_settings.json
        if os.path.exists(SETTINGS_FILE):
            file3 = FSInputFile(SETTINGS_FILE)
            await callback.message.answer_document(file3, caption="📦 bot_settings.json")
        
        await callback.message.answer("✅ Экспорт завершен!")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка экспорта: {e}")


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    """Показать статистику"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    stats = get_bot_stats()
    users_db = get_all_users()
    
    # Топ 5 пользователей по запросам
    top_users = sorted(users_db.items(), key=lambda x: x[1]["total_requests"], reverse=True)[:5]
    
    text = (
        "📊 Статистика бота\n\n"
        f"👥 Всего пользователей: {stats['total_users']}\n"
        f"✅ Активных пользователей: {stats['active_users']}\n"
        f"📨 Всего запросов: {stats['total_requests']}\n\n"
        "🏆 Топ 5 пользователей:\n"
    )
    
    for i, (user_id, user_data) in enumerate(top_users, 1):
        username = user_data.get("username", "unknown")
        requests = user_data["total_requests"]
        
        # Показываем username с @ только если он не начинается с "user_"
        if username.startswith("user_"):
            username_display = f"ID {user_id}"
        else:
            username_display = f"@{username}"
        
        text += f"{i}. {username_display} - {requests} запросов\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    """Показать всех пользователей"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    users_db = get_all_users()
    
    text = "👥 Все пользователи:\n\n"
    
    for user_id, user_data in users_db.items():
        username = user_data.get("username", "unknown")
        model_tokens = user_data.get("model_tokens", {})
        total_tokens = sum(model_tokens.values())
        total = user_data["total_requests"]
        last_reset = datetime.fromisoformat(user_data["last_reset"]).strftime("%d.%m %H:%M")
        
        # Показываем username с @ только если он не начинается с "user_"
        if username.startswith("user_"):
            username_display = f"ID: {user_id}"
        else:
            username_display = f"@{username}"
        
        text += (
            f"👤 {username_display}\n"
            f"🆔 {user_id}\n"
            f"📊 Всего токенов: {total_tokens}\n"
            f"📈 Всего запросов: {total}\n"
            f"🕐 Обновление: {last_reset}\n\n"
        )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "admin_add_tokens")
async def admin_add_tokens_start(callback: CallbackQuery, state: FSMContext):
    """Начать процесс выдачи токенов"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    await state.set_state(AdminStates.waiting_for_user_id)
    await callback.message.answer("Введите ID пользователя:")
    await callback.answer()


@dp.message(AdminStates.waiting_for_user_id)
async def admin_get_user_id(message: Message, state: FSMContext):
    """Получить ID пользователя"""
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        user_id = int(message.text)
        
        # Проверяем существование пользователя
        users_db = get_all_users()
        if str(user_id) not in users_db:
            await message.answer(
                f"❌ Пользователь с ID {user_id} не найден в базе\n\n"
                f"Пользователь должен сначала написать боту хотя бы одно сообщение.\n"
                f"Попробуйте другой ID или отмените командой /admin"
            )
            return
        
        await state.update_data(target_user_id=user_id)
        await state.set_state(AdminStates.waiting_for_model_selection)
        
        # Показываем информацию о пользователе и выбор модели
        user_data = users_db[str(user_id)]
        username = user_data.get("username", "unknown")
        model_tokens = user_data.get("model_tokens", {})
        total_balance = sum(model_tokens.values())
        
        if username.startswith("user_"):
            username_display = f"ID {user_id}"
        else:
            username_display = f"@{username}"
        
        # Создаем кнопки для выбора модели
        buttons = []
        for model_id, model_info in AVAILABLE_MODELS.items():
            model_name = model_info["name"]
            buttons.append([InlineKeyboardButton(
                text=model_name,
                callback_data=f"addtokens_{model_id}"
            )])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        
        await message.answer(
            f"👤 Пользователь: {username_display}\n"
            f"📊 Всего токенов: {total_balance}\n\n"
            f"🤖 Выберите модель для выдачи токенов:",
            reply_markup=keyboard
        )
    except ValueError:
        await message.answer("❌ Неверный формат ID. Введите число:")


@dp.callback_query(F.data.startswith("addtokens_"))
async def admin_select_model_for_tokens(callback: CallbackQuery, state: FSMContext):
    """Выбрать модель для выдачи токенов"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    model_id = callback.data.replace("addtokens_", "")
    
    if model_id not in AVAILABLE_MODELS:
        await callback.answer("❌ Неизвестная модель")
        return
    
    model_name = AVAILABLE_MODELS[model_id]["name"]
    
    await state.update_data(target_model=model_id)
    await state.set_state(AdminStates.waiting_for_tokens_amount)
    
    await callback.message.answer(
        f"🤖 Модель: {model_name}\n\n"
        f"Сколько токенов выдать?"
    )
    await callback.answer()


@dp.message(AdminStates.waiting_for_tokens_amount)
async def admin_get_tokens_amount(message: Message, state: FSMContext):
    """Получить количество токенов"""
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        amount = int(message.text)
        
        if amount <= 0:
            await message.answer("❌ Количество токенов должно быть больше 0")
            return
        
        data = await state.get_data()
        target_user_id = data["target_user_id"]
        target_model = data.get("target_model", DEFAULT_MODEL)
        
        # Проверяем еще раз что пользователь существует
        users_db = get_all_users()
        if str(target_user_id) not in users_db:
            await message.answer(f"❌ Пользователь {target_user_id} больше не найден в базе")
            await state.clear()
            return
        
        # Выдаем токены для конкретной модели
        add_requests(target_user_id, amount, target_model)
        
        # Получаем обновленные данные
        users_db = get_all_users()
        user_data = users_db[str(target_user_id)]
        username = user_data.get("username", "unknown")
        new_balance = get_user_model_balance(target_user_id, target_model)
        model_name = AVAILABLE_MODELS[target_model]["name"]
        
        if username.startswith("user_"):
            username_display = f"ID {target_user_id}"
        else:
            username_display = f"@{username}"
        
        await message.answer(
            f"✅ Успешно выдано {amount} токенов\n\n"
            f"👤 Пользователь: {username_display}\n"
            f"🤖 Модель: {model_name}\n"
            f"📊 Новый баланс: {new_balance}"
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Неверный формат. Введите число:")


@dp.callback_query(F.data == "admin_change_limit")
async def admin_change_limit_start(callback: CallbackQuery, state: FSMContext):
    """Выбрать модель для изменения лимита"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    buttons = []
    for model_id, model_info in AVAILABLE_MODELS.items():
        model_name = model_info["name"]
        current_limit = get_model_limit(model_id)
        buttons.append([InlineKeyboardButton(
            text=f"{model_name} (лимит: {current_limit})",
            callback_data=f"setlimit_{model_id}"
        )])
    
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(
        "🤖 Выберите модель для изменения лимита:\n\n"
        "Текущие лимиты показаны в скобках.",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("setlimit_"))
async def admin_set_model_limit_start(callback: CallbackQuery, state: FSMContext):
    """Начать установку лимита для модели"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    model_id = callback.data.replace("setlimit_", "")
    
    if model_id not in AVAILABLE_MODELS:
        await callback.answer("❌ Неизвестная модель")
        return
    
    model_name = AVAILABLE_MODELS[model_id]["name"]
    current_limit = get_model_limit(model_id)
    
    await state.update_data(target_model=model_id)
    await state.set_state(AdminStates.waiting_for_model_limit)
    
    await callback.message.answer(
        f"⚙️ Изменение лимита для модели\n\n"
        f"🤖 Модель: {model_name}\n"
        f"📊 Текущий лимит: {current_limit} запросов\n\n"
        f"Введите новый лимит (число от 1 до 1000):"
    )
    await callback.answer()


@dp.message(AdminStates.waiting_for_model_limit)
async def admin_set_model_limit(message: Message, state: FSMContext):
    """Установить новый лимит для модели"""
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        new_limit = int(message.text)
        
        if new_limit < 1 or new_limit > 1000:
            await message.answer("❌ Лимит должен быть от 1 до 1000. Попробуйте снова:")
            return
        
        data = await state.get_data()
        model_id = data["target_model"]
        model_name = AVAILABLE_MODELS[model_id]["name"]
        old_limit = get_model_limit(model_id)
        
        set_model_limit(model_id, new_limit)
        
        await message.answer(
            f"✅ Лимит для модели изменен\n\n"
            f"🤖 Модель: {model_name}\n"
            f"Было: {old_limit} запросов\n"
            f"Стало: {new_limit} запросов\n\n"
            f"⚠️ Новый лимит применяется сразу для всех пользователей."
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Неверный формат. Введите число:")


@dp.callback_query(F.data == "admin_toggle_bot_creation")
async def admin_toggle_bot_creation(callback: CallbackQuery):
    """Включить/отключить создание ботов"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    current_status = is_bot_creation_enabled()
    new_status = not current_status
    set_bot_creation_enabled(new_status)
    
    status_text = "включено" if new_status else "отключено"
    emoji = "✅" if new_status else "❌"
    
    await callback.answer(f"{emoji} Создание ботов {status_text}", show_alert=True)
    
    # Обновляем админ-панель
    stats = get_bot_stats()
    bot_creation_status = "✅ Включено" if new_status else "❌ Отключено"
    bot_creation_button_text = "🔴 Отключить создание ботов" if new_status else "🟢 Включить создание ботов"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Все пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="➕ Выдать токены", callback_data="admin_add_tokens")],
        [InlineKeyboardButton(text="⚙️ Лимиты моделей", callback_data="admin_change_limit")],
        [InlineKeyboardButton(text=bot_creation_button_text, callback_data="admin_toggle_bot_creation")],
        [InlineKeyboardButton(text="🌐 Проверить API", callback_data="admin_check_api")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")]
    ])
    
    text = (
        "🔐 Админ-панель\n\n"
        f"👥 Всего пользователей: {stats['total_users']}\n"
        f"✅ Активных: {stats['active_users']}\n"
        f"📨 Всего запросов: {stats['total_requests']}\n"
        f"🤖 Создание ботов: {bot_creation_status}"
    )
    
    await callback.message.edit_text(text, reply_markup=keyboard)


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    """Вернуться в админ-панель"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    stats = get_bot_stats()
    bot_creation_status = "✅ Включено" if is_bot_creation_enabled() else "❌ Отключено"
    bot_creation_button_text = "🔴 Отключить создание ботов" if is_bot_creation_enabled() else "🟢 Включить создание ботов"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Все пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="➕ Выдать токены", callback_data="admin_add_tokens")],
        [InlineKeyboardButton(text="⚙️ Лимиты моделей", callback_data="admin_change_limit")],
        [InlineKeyboardButton(text=bot_creation_button_text, callback_data="admin_toggle_bot_creation")],
        [InlineKeyboardButton(text="🌐 Проверить API", callback_data="admin_check_api")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="💾 Экспорт БД", callback_data="admin_export_db")]
    ])
    
    text = (
        "� Админ-панлель\n\n"
        f"👥 Всего пользователей: {stats['total_users']}\n"
        f"✅ Активных: {stats['active_users']}\n"
        f"📨 Всего запросов: {stats['total_requests']}\n"
        f"🤖 Создание ботов: {bot_creation_status}"
    )
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.message(F.photo)
async def handle_photo(message: Message):
    """Обработка фотографий - распознавание текста"""
    # Регистрируем пользователя
    username = message.from_user.username or f"user_{message.from_user.id}"
    get_user_data(message.from_user.id, username)
    
    try:
        status_msg = await message.answer("📸 Распознаю текст на изображении...")
        
        # Получаем файл
        photo = message.photo[-1]  # Берем самое большое фото
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        
        # Открываем изображение
        image = Image.open(io.BytesIO(file_bytes.read()))
        
        # Улучшаем изображение для лучшего распознавания
        # Конвертируем в оттенки серого и увеличиваем контраст
        from PIL import ImageEnhance
        image = image.convert('L')  # Оттенки серого
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(2)  # Увеличиваем контраст
        
        # Пробуем разные языки
        text = ""
        try:
            # Проверяем, установлен ли Tesseract
            import shutil
            if not shutil.which('tesseract'):
                raise Exception("Tesseract не установлен")
            
            # Сначала пробуем русский + английский
            text = pytesseract.image_to_string(image, lang='rus+eng')
        except Exception as e:
            if "not installed" in str(e).lower() or "tesseract" in str(e).lower():
                await status_msg.delete()
                await message.answer(
                    "❌ OCR временно недоступен\n\n"
                    "Функция распознавания текста с изображений отключена.\n"
                    "Пожалуйста, отправьте текст сообщением."
                )
                return
            try:
                # Если не получилось, пробуем только английский
                text = pytesseract.image_to_string(image, lang='eng')
            except:
                # В крайнем случае без указания языка
                text = pytesseract.image_to_string(image)
        
        await status_msg.delete()
        
        # Убираем лишние пробелы и переносы
        text = text.strip()
        
        if text and len(text) > 2:  # Минимум 3 символа
            # Если есть подпись к фото, добавляем её как вопрос
            if message.caption:
                # Проверяем лимит запросов
                user_data = get_user_data(message.from_user.id, username)
                selected_model = user_data.get("selected_model", DEFAULT_MODEL)
                if not use_request(message.from_user.id, selected_model):
                    last_reset = datetime.fromisoformat(user_data["last_reset"])
                    next_reset = last_reset + timedelta(hours=24)
                    time_left = next_reset - datetime.now()
                    hours = int(time_left.total_seconds() // 3600)
                    minutes = int((time_left.total_seconds() % 3600) // 60)
                    
                    await message.answer(
                        f"❌ Вы исчерпали лимит запросов ({DAILY_LIMIT} в день)\n\n"
                        f"⏰ Лимит обновится через: {hours}ч {minutes}мин\n\n"
                        f"📝 Распознанный текст:\n{text}"
                    )
                    return
                
                user_message = f"На изображении текст:\n{text}\n\nВопрос: {message.caption}"
                
                thinking_msg = await message.answer("💭 Думаю...")
                await bot.send_chat_action(message.chat.id, "typing")
                
                ai_response = await get_ai_response(message.from_user.id, user_message)
                
                await thinking_msg.delete()
                await send_long_message(message, ai_response)
            else:
                # Просто отправляем распознанный текст (без использования запроса)
                await message.answer(f"📝 Распознанный текст:\n\n{text}")
        else:
            await message.answer(
                "❌ Не удалось распознать текст на изображении.\n\n"
                "Советы:\n"
                "• Убедитесь, что текст четкий и хорошо читаемый\n"
                "• Текст должен быть достаточно крупным\n"
                "• Избегайте размытых или темных фото\n"
                "• Попробуйте сфотографировать при хорошем освещении"
            )
            
    except Exception as e:
        logging.error(f"Ошибка распознавания текста: {e}")
        await message.answer(
            f"❌ Ошибка при обработке изображения\n\n"
            f"Возможно, Tesseract OCR не установлен или установлен неправильно.\n"
            f"Ошибка: {str(e)}"
        )

@dp.message(F.text)
async def handle_message(message: Message, state: FSMContext):
    # Проверяем, не находимся ли мы в состоянии FSM
    current_state = await state.get_state()
    if current_state:
        return

    if message.text.startswith('/'):
        return
    
    # Игнорируем кнопки меню - проверяем по содержанию текста
    if message.text and any(keyword in message.text for keyword in ["Создать бота", "Мои боты", "Чат с AI", "Выбрать модель"]):
        return

    # Регистрируем пользователя и проверяем лимиты
    username = message.from_user.username or f"user_{message.from_user.id}"
    user_data = get_user_data(message.from_user.id, username)
    
    # Проверяем лимит запросов
    user_data = get_user_data(message.from_user.id, username)
    selected_model = user_data.get("selected_model", DEFAULT_MODEL)
    if not use_request(message.from_user.id, selected_model):
        last_reset = datetime.fromisoformat(user_data["last_reset"])
        next_reset = last_reset.replace(hour=last_reset.hour, minute=last_reset.minute) + timedelta(hours=24)
        time_left = next_reset - datetime.now()
        hours = int(time_left.total_seconds() // 3600)
        minutes = int((time_left.total_seconds() % 3600) // 60)
        
        model_name = AVAILABLE_MODELS.get(selected_model, {}).get("name", selected_model)
        
        await message.answer(
            f"❌ Вы исчерпали токены для модели {model_name}\n\n"
            f"⏰ Токены обновятся через: {hours}ч {minutes}мин\n"
            f"📅 Последнее обновление: {last_reset.strftime('%d.%m.%Y %H:%M')}\n\n"
        )
        return

    # Проверяем, просит ли пользователь отправить ответ файлом
    user_text = message.text.lower()
    force_file = any(keyword in user_text for keyword in [
        'отправь файлом', 'пришли файлом', 'скинь файлом',
        'в файле', 'как файл', 'файлом', 'в txt',
        'send as file', 'as a file', 'in file',
        'отправь сообщение в текстовом файле', 'сообщение в файле'
    ])

    thinking_msg = await message.answer("💭 Думаю...")
    await bot.send_chat_action(message.chat.id, "typing")

    ai_response = await get_ai_response(message.from_user.id, message.text)
    
    # Форматируем ответ: добавляем кавычки к цитатам и выделяем код
    ai_response = format_ai_response(ai_response)

    await thinking_msg.delete()
    
    # Показываем оставшиеся запросы
    user_data = get_user_data(message.from_user.id, username)
    selected_model = user_data.get("selected_model", DEFAULT_MODEL)
    model_tokens = user_data.get("model_tokens", {})
    requests_left = model_tokens.get(selected_model, 0)
    
    await send_long_message(message, ai_response, force_file=force_file)
    
    if requests_left <= 5:
        model_name = AVAILABLE_MODELS[selected_model]["name"]
        await message.answer(f"⚠️ Осталось токенов для {model_name}: {requests_left}")


def migrate_database():
    """Миграция базы данных - добавление новых моделей для существующих пользователей"""
    try:
        db = load_database()
        updated = False
        
        for user_id, user_data in db.get("users", {}).items():
            # Проверяем, есть ли model_tokens
            if "model_tokens" not in user_data:
                user_data["model_tokens"] = {}
                updated = True
            
            # Добавляем недостающие модели
            for model_id in AVAILABLE_MODELS.keys():
                if model_id not in user_data["model_tokens"]:
                    user_data["model_tokens"][model_id] = get_model_limit(model_id)
                    updated = True
                    logging.info(f"Добавлена модель {model_id} для пользователя {user_id}")
            
            # Проверяем другие поля
            if "selected_model" not in user_data:
                user_data["selected_model"] = DEFAULT_MODEL
                updated = True
            
            if "bots" not in user_data:
                user_data["bots"] = []
                updated = True
            
            if "total_requests" not in user_data:
                user_data["total_requests"] = 0
                updated = True
        
        if updated:
            save_database(db)
            logging.info("✅ База данных успешно обновлена")
        else:
            logging.info("✅ База данных актуальна")
            
    except Exception as e:
        logging.error(f"Ошибка миграции базы данных: {e}")


async def main():
    # Выполняем миграцию базы данных при запуске
    migrate_database()
    
    logging.info("🚀 Мультифункциональный бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
