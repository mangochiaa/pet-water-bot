"""
Pet Water Tracker — Telegram-бот для учёта питья воды питомцами.

Запуск:
    python bot.py

Токен берётся из переменной окружения BOT_TOKEN (файл .env).
Данные сохраняются в pets.json и water_log.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Настройки и константы
# ---------------------------------------------------------------------------

# Загружаем переменные из .env (рядом с этим файлом)
load_dotenv()

# Пути к JSON-файлам с данными
BASE_DIR = Path(__file__).resolve().parent
PETS_FILE = BASE_DIR / "pets.json"
WATER_LOG_FILE = BASE_DIR / "water_log.json"

# Состояния диалога /add_pet
ASK_NAME, ASK_SPECIES, ASK_WEIGHT = range(3)

# Префикс callback-данных для отметки «попил воду»
WATER_CALLBACK_PREFIX = "water:"

# Главное меню (кнопки снизу экрана)
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Добавить питомца", "🐾 Мои питомцы"],
        ["💧 Попил воды", "📊 Статистика"],
        ["❓ Помощь"],
    ],
    resize_keyboard=True,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Работа с JSON-хранилищем
# ---------------------------------------------------------------------------

def _read_json(path: Path, default: Any) -> Any:
    """Читает JSON-файл. Если файла нет или он повреждён — возвращает default."""
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        logger.error("Не удалось прочитать %s: %s", path, error)
        return default


def _write_json(path: Path, data: Any) -> None:
    """Сохраняет данные в JSON-файл с отступами для удобного чтения."""
    try:
        with path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    except OSError as error:
        logger.error("Не удалось записать %s: %s", path, error)
        raise


def load_pets() -> dict[str, list[dict[str, Any]]]:
    """
    Загружает питомцев.

    Формат:
        {
          "123456789": [
            {"id": "...", "name": "Мурка", "species": "кот", "weight_kg": 4.2}
          ]
        }
    Ключ — Telegram user_id в виде строки.
    """
    data = _read_json(PETS_FILE, {})
    if not isinstance(data, dict):
        return {}
    return data


def save_pets(pets: dict[str, list[dict[str, Any]]]) -> None:
    """Сохраняет список питомцев."""
    _write_json(PETS_FILE, pets)


def load_water_log() -> list[dict[str, Any]]:
    """
    Загружает журнал поения.

    Формат записи:
        {"user_id": "...", "pet_id": "...", "timestamp": "2026-10-02T16:20:00"}
    """
    data = _read_json(WATER_LOG_FILE, [])
    if not isinstance(data, list):
        return []
    return data


def save_water_log(log: list[dict[str, Any]]) -> None:
    """Сохраняет журнал поения."""
    _write_json(WATER_LOG_FILE, log)


def get_user_pets(user_id: int) -> list[dict[str, Any]]:
    """Возвращает питомцев конкретного пользователя."""
    return load_pets().get(str(user_id), [])


def add_pet(user_id: int, name: str, species: str, weight_kg: float) -> dict[str, Any]:
    """Добавляет питомца и возвращает созданную запись."""
    pets = load_pets()
    user_key = str(user_id)
    pet = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "species": species,
        "weight_kg": weight_kg,
    }
    pets.setdefault(user_key, []).append(pet)
    save_pets(pets)
    return pet


def log_water(user_id: int, pet_id: str) -> bool:
    """
    Добавляет запись «питомец попил воду».
    Возвращает False, если питомец не найден у этого пользователя.
    """
    pets = get_user_pets(user_id)
    if not any(pet["id"] == pet_id for pet in pets):
        return False

    log = load_water_log()
    log.append(
        {
            "user_id": str(user_id),
            "pet_id": pet_id,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
    )
    save_water_log(log)
    return True


def today_stats(user_id: int) -> dict[str, int]:
    """Считает, сколько раз каждый питомец пил сегодня. Ключ — pet_id."""
    today = date.today().isoformat()
    counts: dict[str, int] = {}
    for entry in load_water_log():
        if entry.get("user_id") != str(user_id):
            continue
        timestamp = str(entry.get("timestamp", ""))
        if not timestamp.startswith(today):
            continue
        pet_id = entry.get("pet_id")
        if pet_id:
            counts[pet_id] = counts.get(pet_id, 0) + 1
    return counts


def pets_keyboard(pets: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Клавиатура с кнопками по одному питомцу на строку."""
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{pet['name']} ({pet['species']})",
                callback_data=f"{WATER_CALLBACK_PREFIX}{pet['id']}",
            )
        ]
        for pet in pets
    ]
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Команды и меню
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /start — приветствие и главное меню. """
    try:
        user = update.effective_user
        name = user.first_name if user else "друг"
        await update.message.reply_text(
            f"Привет, {name}! 🐾\n\n"
            "Я Pet Water Tracker — помогу следить, как часто ваши питомцы пьют воду.\n\n"
            "Выберите действие в меню ниже или отправьте команду.",
            reply_markup=MAIN_MENU,
        )
    except Exception as error:
        logger.exception("Ошибка в /start: %s", error)
        await _safe_reply(update, "Не получилось показать приветствие. Попробуйте ещё раз.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /help — краткая справка по командам. """
    try:
        await update.message.reply_text(
            "📖 Справка\n\n"
            "/start — приветствие и меню\n"
            "/add_pet — добавить питомца (имя, вид, вес)\n"
            "/pets — список ваших питомцев\n"
            "/water — отметить, что питомец попил воды\n"
            "/stats — сколько раз пили сегодня\n"
            "/help — эта справка\n"
            "/cancel — отменить добавление питомца\n\n"
            "Данные хранятся только у вас в файлах бота.",
            reply_markup=MAIN_MENU,
        )
    except Exception as error:
        logger.exception("Ошибка в /help: %s", error)
        await _safe_reply(update, "Не получилось показать справку.")


async def list_pets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /pets — показать всех питомцев пользователя. """
    try:
        pets = get_user_pets(update.effective_user.id)
        if not pets:
            await update.message.reply_text(
                "Пока нет ни одного питомца.\n"
                "Добавьте первого командой /add_pet или кнопкой «➕ Добавить питомца».",
                reply_markup=MAIN_MENU,
            )
            return

        lines = ["🐾 Ваши питомцы:\n"]
        for index, pet in enumerate(pets, start=1):
            lines.append(
                f"{index}. {pet['name']} — {pet['species']}, {pet['weight_kg']} кг"
            )
        await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)
    except Exception as error:
        logger.exception("Ошибка в /pets: %s", error)
        await _safe_reply(update, "Не получилось загрузить список питомцев.")


async def water_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /water — выбор питомца, который попил воду. """
    try:
        pets = get_user_pets(update.effective_user.id)
        if not pets:
            await update.message.reply_text(
                "Сначала добавьте питомца командой /add_pet.",
                reply_markup=MAIN_MENU,
            )
            return

        await update.message.reply_text(
            "Кто попил воду? Нажмите на имя:",
            reply_markup=pets_keyboard(pets),
        )
    except Exception as error:
        logger.exception("Ошибка в /water: %s", error)
        await _safe_reply(update, "Не получилось показать список для отметки.")


async def water_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатия Inline-кнопки «питомец попил воду»."""
    query = update.callback_query
    try:
        await query.answer()
        data = query.data or ""
        if not data.startswith(WATER_CALLBACK_PREFIX):
            return

        pet_id = data[len(WATER_CALLBACK_PREFIX) :]
        user_id = query.from_user.id
        pets = get_user_pets(user_id)
        pet = next((item for item in pets if item["id"] == pet_id), None)

        if pet is None:
            await query.edit_message_text("Этот питомец не найден. Обновите список /pets.")
            return

        if not log_water(user_id, pet_id):
            await query.edit_message_text("Не удалось сохранить отметку. Попробуйте снова.")
            return

        now = datetime.now().strftime("%H:%M")
        await query.edit_message_text(
            f"💧 Отмечено: {pet['name']} попил(а) воду в {now}.\n"
            "Так держать! Можно посмотреть статистику: /stats"
        )
    except Exception as error:
        logger.exception("Ошибка в water_callback: %s", error)
        try:
            await query.edit_message_text("Произошла ошибка при сохранении отметки.")
        except Exception:
            pass


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /stats — сколько раз каждый питомец пил сегодня. """
    try:
        user_id = update.effective_user.id
        pets = get_user_pets(user_id)
        if not pets:
            await update.message.reply_text(
                "Нет питомцев — нечего считать. Добавьте питомца: /add_pet",
                reply_markup=MAIN_MENU,
            )
            return

        counts = today_stats(user_id)
        today_label = date.today().strftime("%d.%m.%Y")
        lines = [f"📊 Статистика за сегодня ({today_label}):\n"]
        total = 0
        for pet in pets:
            times = counts.get(pet["id"], 0)
            total += times
            word = _times_word(times)
            lines.append(f"• {pet['name']}: {times} {word}")

        lines.append(f"\nВсего отметок: {total}")
        await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)
    except Exception as error:
        logger.exception("Ошибка в /stats: %s", error)
        await _safe_reply(update, "Не получилось посчитать статистику.")


def _times_word(count: int) -> str:
    """Склонение слова «раз» для русского языка."""
    n = abs(count) % 100
    if 11 <= n <= 14:
        return "раз"
    last = n % 10
    if last == 1:
        return "раз"
    if 2 <= last <= 4:
        return "раза"
    return "раз"


# ---------------------------------------------------------------------------
# Диалог добавления питомца (ConversationHandler)
# ---------------------------------------------------------------------------

async def add_pet_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало диалога: спрашиваем имя."""
    try:
        await update.message.reply_text(
            "Как зовут питомца?\n"
            "Чтобы отменить, отправьте /cancel."
        )
        return ASK_NAME
    except Exception as error:
        logger.exception("Ошибка в add_pet_start: %s", error)
        await _safe_reply(update, "Не получилось начать добавление. Попробуйте /add_pet ещё раз.")
        return ConversationHandler.END


async def add_pet_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняем имя и спрашиваем вид."""
    try:
        name = (update.message.text or "").strip()
        if not name:
            await update.message.reply_text("Имя не должно быть пустым. Напишите, как зовут питомца.")
            return ASK_NAME

        context.user_data["new_pet_name"] = name
        await update.message.reply_text(
            f"Отлично, {name}! Какой это вид?\n"
            "Например: кот, собака, кролик, попугай."
        )
        return ASK_SPECIES
    except Exception as error:
        logger.exception("Ошибка в add_pet_name: %s", error)
        await _safe_reply(update, "Ошибка при сохранении имени. Попробуйте ещё раз или /cancel.")
        return ASK_NAME


async def add_pet_species(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняем вид и спрашиваем вес."""
    try:
        species = (update.message.text or "").strip()
        if not species:
            await update.message.reply_text("Укажите вид питомца текстом, например: кот.")
            return ASK_SPECIES

        context.user_data["new_pet_species"] = species
        await update.message.reply_text(
            "Сколько весит питомец в килограммах?\n"
            "Можно дробное число, например: 4.2"
        )
        return ASK_WEIGHT
    except Exception as error:
        logger.exception("Ошибка в add_pet_species: %s", error)
        await _safe_reply(update, "Ошибка при сохранении вида. Попробуйте ещё раз или /cancel.")
        return ASK_SPECIES


async def add_pet_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Проверяем вес, сохраняем питомца и завершаем диалог."""
    try:
        raw = (update.message.text or "").strip().replace(",", ".")
        try:
            weight = float(raw)
        except ValueError:
            await update.message.reply_text(
                "Это не похоже на число. Введите вес в кг, например: 5 или 3.5"
            )
            return ASK_WEIGHT

        if weight <= 0 or weight > 500:
            await update.message.reply_text(
                "Вес должен быть больше 0 и разумным (до 500 кг). Попробуйте снова."
            )
            return ASK_WEIGHT

        name = context.user_data.get("new_pet_name", "Без имени")
        species = context.user_data.get("new_pet_species", "не указан")
        pet = add_pet(update.effective_user.id, name, species, round(weight, 2))

        # Очищаем временные данные диалога
        context.user_data.pop("new_pet_name", None)
        context.user_data.pop("new_pet_species", None)

        await update.message.reply_text(
            f"✅ Питомец добавлен!\n\n"
            f"Имя: {pet['name']}\n"
            f"Вид: {pet['species']}\n"
            f"Вес: {pet['weight_kg']} кг\n\n"
            "Отметить воду можно командой /water.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END
    except Exception as error:
        logger.exception("Ошибка в add_pet_weight: %s", error)
        await _safe_reply(update, "Не получилось сохранить питомца. Попробуйте /add_pet заново.")
        return ConversationHandler.END


async def cancel_add_pet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ /cancel — выход из диалога добавления. """
    context.user_data.pop("new_pet_name", None)
    context.user_data.pop("new_pet_species", None)
    await update.message.reply_text(
        "Добавление отменено. Можно начать снова: /add_pet",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Вспомогательные обработчики
# ---------------------------------------------------------------------------

async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на непонятное сообщение вне диалога."""
    await update.message.reply_text(
        "Не понял сообщение. Откройте меню или напишите /help.",
        reply_markup=MAIN_MENU,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик необработанных исключений."""
    logger.exception("Необработанная ошибка: %s", context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Что-то пошло не так. Попробуйте ещё раз или /help."
            )
        except Exception:
            pass


async def _safe_reply(update: Update, text: str) -> None:
    """Пытается ответить пользователю, даже если часть апдейта отсутствует."""
    try:
        if update.message:
            await update.message.reply_text(text, reply_markup=MAIN_MENU)
        elif update.effective_message:
            await update.effective_message.reply_text(text, reply_markup=MAIN_MENU)
    except Exception as error:
        logger.error("Не удалось отправить сообщение об ошибке: %s", error)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def build_application(token: str) -> Application:
    """Собирает Application и регистрирует все обработчики."""
    application = Application.builder().token(token).build()

    # Пошаговое добавление питомца
    add_pet_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("add_pet", add_pet_start),
            MessageHandler(filters.Regex("^➕ Добавить питомца$"), add_pet_start),
        ],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_pet_name)],
            ASK_SPECIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_pet_species)],
            ASK_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_pet_weight)],
        },
        fallbacks=[CommandHandler("cancel", cancel_add_pet)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.Regex("^❓ Помощь$"), help_command))
    application.add_handler(add_pet_conversation)
    application.add_handler(CommandHandler("pets", list_pets))
    application.add_handler(MessageHandler(filters.Regex("^🐾 Мои питомцы$"), list_pets))
    application.add_handler(CommandHandler("water", water_command))
    application.add_handler(MessageHandler(filters.Regex("^💧 Попил воды$"), water_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.Regex("^📊 Статистика$"), stats_command))
    application.add_handler(CallbackQueryHandler(water_callback, pattern=f"^{WATER_CALLBACK_PREFIX}"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))
    application.add_error_handler(error_handler)

    return application


def _ensure_event_loop() -> None:
    """Создаёт текущий event loop, если его ещё нет.

    python-telegram-bot 21.x внутри вызывает asyncio.get_event_loop().
    На Python 3.12+ (в том числе 3.14) в главном потоке loop больше
    не создаётся автоматически, из-за чего возникает RuntimeError.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def main() -> None:
    """Читает токен и запускает long polling."""
    token = os.getenv("BOT_TOKEN")
    if not token or token == "your_token_here":
        raise SystemExit(
            "Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен от @BotFather."
        )

    _ensure_event_loop()
    application = build_application(token)
    logger.info("Бот Pet Water Tracker запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
