"""
Pet Water Tracker — Telegram-бот для учёта питья воды питомцами.

Запуск:
    python bot.py

Токен берётся из переменной окружения BOT_TOKEN (файл .env).
Данные хранятся в SQLite (bot.db).
При первом запуске старые pets.json и water_log.json переносятся в БД.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Any, Generator, IO, Optional

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

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "bot.db"
PETS_FILE = BASE_DIR / "pets.json"
WATER_LOG_FILE = BASE_DIR / "water_log.json"

# Состояния диалогов (у каждого ConversationHandler свой набор)
ASK_NAME, ASK_SPECIES, ASK_WEIGHT = range(3)
ASK_WATER_PET, ASK_WATER_AMOUNT = range(3, 5)

WATER_CALLBACK_PREFIX = "water:"
SPECIES_CALLBACK_PREFIX = "species:"
MAX_AMOUNT_ML = 2000

# Кнопки вида: callback-ключ -> текст, который пишем в БД
SPECIES_CHOICES = {
    "dog": "Собака",
    "cat": "Кот",
    "bird": "Птица",
    "rodent": "Грызун",
}

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Добавить питомца", "🐾 Мои питомцы"],
        ["💧 Попил воды", "📊 Сегодня"],
        ["📅 За неделю", "🏆 Топ"],
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
# SQLite: подключение, схема, миграция из JSON
# ---------------------------------------------------------------------------

@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Открывает соединение с БД, фиксирует изменения или откатывает их."""
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    try:
        with get_db() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    species TEXT NOT NULL,
                    weight REAL NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS water_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pet_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    amount_ml REAL NOT NULL DEFAULT 0,
                    FOREIGN KEY (pet_id) REFERENCES pets(id)
                );
                """
            )
        migrate_amount_ml_column()
        logger.info("База данных готова: %s", DB_FILE)
    except sqlite3.Error as error:
        logger.exception("Не удалось создать таблицы: %s", error)
        raise SystemExit("Ошибка инициализации SQLite. Подробности в логе.")


def migrate_amount_ml_column() -> None:
    """Добавляет amount_ml в уже существующую таблицу water_log, если колонки нет.

    Старым записям SQLite проставит DEFAULT 0 — объём тогда был неизвестен.
    """
    try:
        with get_db() as connection:
            columns = [
                row["name"]
                for row in connection.execute("PRAGMA table_info(water_log)").fetchall()
            ]
            if "amount_ml" in columns:
                return
            connection.execute(
                "ALTER TABLE water_log ADD COLUMN amount_ml REAL NOT NULL DEFAULT 0"
            )
        logger.info("Колонка water_log.amount_ml добавлена, старые записи = 0 мл")
    except sqlite3.Error as error:
        logger.exception("Не удалось добавить amount_ml: %s", error)
        raise


def _read_json(path: Path, default: Any) -> Any:
    """Читает JSON для одноразовой миграции в SQLite."""
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        logger.error("Не удалось прочитать %s: %s", path, error)
        return default


def migrate_json_to_sqlite() -> None:
    """
    Переносит данные из pets.json и water_log.json в bot.db.

    Запускается при старте, только если JSON-файлы ещё лежат рядом с ботом.
    Старые строковые id питомцев заменяются на INTEGER PRIMARY KEY;
    записи журнала привязываются к новым id. После успеха JSON удаляются.
    """
    if not PETS_FILE.exists() and not WATER_LOG_FILE.exists():
        return

    pets_data = _read_json(PETS_FILE, {})
    log_data = _read_json(WATER_LOG_FILE, [])
    if not isinstance(pets_data, dict):
        pets_data = {}
    if not isinstance(log_data, list):
        log_data = []

    # Старый JSON-id -> новый INTEGER id в SQLite
    id_map: dict[str, int] = {}
    created_at = datetime.now().isoformat(timespec="seconds")

    try:
        with get_db() as connection:
            for user_key, pets in pets_data.items():
                try:
                    user_id = int(user_key)
                except (TypeError, ValueError):
                    logger.warning("Пропущен некорректный user_id в JSON: %s", user_key)
                    continue
                if not isinstance(pets, list):
                    continue
                for pet in pets:
                    old_id = str(pet.get("id", ""))
                    weight = pet.get("weight", pet.get("weight_kg", 0))
                    cursor = connection.execute(
                        """
                        INSERT INTO pets (user_id, name, species, weight, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            str(pet.get("name", "Без имени")),
                            str(pet.get("species", "не указан")),
                            float(weight or 0),
                            created_at,
                        ),
                    )
                    if old_id:
                        id_map[old_id] = cursor.lastrowid

            for entry in log_data:
                old_pet_id = str(entry.get("pet_id", ""))
                new_pet_id = id_map.get(old_pet_id)
                if new_pet_id is None:
                    logger.warning("Пропущена запись журнала без питомца: %s", entry)
                    continue
                try:
                    user_id = int(entry.get("user_id"))
                except (TypeError, ValueError):
                    continue
                timestamp = str(entry.get("timestamp") or created_at)
                connection.execute(
                    """
                    INSERT INTO water_log (pet_id, user_id, timestamp, amount_ml)
                    VALUES (?, ?, ?, ?)
                    """,
                    (new_pet_id, user_id, timestamp, 0),
                )
        logger.info(
            "Миграция JSON → SQLite завершена: питомцев %s, записей журнала %s",
            len(id_map),
            len(log_data),
        )
    except (sqlite3.Error, TypeError, ValueError) as error:
        logger.exception("Миграция JSON не удалась, файлы оставлены: %s", error)
        return

    # JSON больше не нужны; код миграции остаётся на случай повторного запуска
    for path in (PETS_FILE, WATER_LOG_FILE):
        try:
            if path.exists():
                path.unlink()
                logger.info("Удалён старый файл %s", path.name)
        except OSError as error:
            logger.warning("Не удалось удалить %s: %s", path, error)


# ---------------------------------------------------------------------------
# Запросы к данным
# ---------------------------------------------------------------------------

def get_user_pets(user_id: int) -> list[dict[str, Any]]:
    """Возвращает питомцев пользователя, от старых к новым."""
    try:
        with get_db() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, name, species, weight, created_at
                FROM pets
                WHERE user_id = ?
                ORDER BY id
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as error:
        logger.exception("Ошибка чтения питомцев: %s", error)
        return []


def get_pet(user_id: int, pet_id: int) -> Optional[dict[str, Any]]:
    """Находит питомца по id, только если он принадлежит пользователю."""
    try:
        with get_db() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, name, species, weight, created_at
                FROM pets
                WHERE id = ? AND user_id = ?
                """,
                (pet_id, user_id),
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as error:
        logger.exception("Ошибка поиска питомца: %s", error)
        return None


def add_pet(user_id: int, name: str, species: str, weight: float) -> Optional[dict[str, Any]]:
    """Добавляет питомца и возвращает созданную запись."""
    created_at = datetime.now().isoformat(timespec="seconds")
    try:
        with get_db() as connection:
            cursor = connection.execute(
                """
                INSERT INTO pets (user_id, name, species, weight, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, name, species, weight, created_at),
            )
            pet_id = cursor.lastrowid
        return {
            "id": pet_id,
            "user_id": user_id,
            "name": name,
            "species": species,
            "weight": weight,
            "created_at": created_at,
        }
    except sqlite3.Error as error:
        logger.exception("Ошибка добавления питомца: %s", error)
        return None


def log_water(user_id: int, pet_id: int, amount_ml: float) -> bool:
    """Пишет отметку «попил воду» с объёмом в мл. False — питомец не найден или ошибка БД."""
    if get_pet(user_id, pet_id) is None:
        return False
    timestamp = datetime.now().isoformat(timespec="seconds")
    try:
        with get_db() as connection:
            connection.execute(
                """
                INSERT INTO water_log (pet_id, user_id, timestamp, amount_ml)
                VALUES (?, ?, ?, ?)
                """,
                (pet_id, user_id, timestamp, amount_ml),
            )
        return True
    except sqlite3.Error as error:
        logger.exception("Ошибка записи в журнал: %s", error)
        return False


def _stats_map(rows: list[sqlite3.Row]) -> dict[int, tuple[int, float]]:
    """pet_id -> (сколько раз, суммарный объём мл)."""
    return {
        int(row["pet_id"]): (int(row["times"]), float(row["total_ml"] or 0))
        for row in rows
    }


def today_stats(user_id: int) -> dict[int, tuple[int, float]]:
    """Отметки и объём за сегодня (локальная дата)."""
    try:
        with get_db() as connection:
            rows = connection.execute(
                """
                SELECT pet_id,
                       COUNT(*) AS times,
                       COALESCE(SUM(amount_ml), 0) AS total_ml
                FROM water_log
                WHERE user_id = ?
                  AND date(timestamp) = date('now', 'localtime')
                GROUP BY pet_id
                """,
                (user_id,),
            ).fetchall()
        return _stats_map(rows)
    except sqlite3.Error as error:
        logger.exception("Ошибка статистики за сегодня: %s", error)
        return {}


def week_stats(user_id: int) -> dict[int, tuple[int, float]]:
    """Отметки и объём за последние 7 дней."""
    try:
        with get_db() as connection:
            rows = connection.execute(
                """
                SELECT pet_id,
                       COUNT(*) AS times,
                       COALESCE(SUM(amount_ml), 0) AS total_ml
                FROM water_log
                WHERE user_id = ?
                  AND datetime(timestamp) >= datetime('now', 'localtime', '-7 days')
                GROUP BY pet_id
                """,
                (user_id,),
            ).fetchall()
        return _stats_map(rows)
    except sqlite3.Error as error:
        logger.exception("Ошибка статистики за неделю: %s", error)
        return {}


def top_pets_week(user_id: int) -> list[tuple[dict[str, Any], int, float]]:
    """Питомцы пользователя: сортировка по числу отметок за 7 дней."""
    pets = get_user_pets(user_id)
    stats = week_stats(user_id)
    ranked = []
    for pet in pets:
        times, total_ml = stats.get(int(pet["id"]), (0, 0.0))
        ranked.append((pet, times, total_ml))
    ranked.sort(key=lambda item: (-item[1], item[0]["name"]))
    return ranked


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


def species_keyboard() -> InlineKeyboardMarkup:
    """Inline-кнопки выбора вида питомца."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🐶 Собака", callback_data=f"{SPECIES_CALLBACK_PREFIX}dog"),
                InlineKeyboardButton("🐱 Кот", callback_data=f"{SPECIES_CALLBACK_PREFIX}cat"),
            ],
            [
                InlineKeyboardButton("🐦 Птица", callback_data=f"{SPECIES_CALLBACK_PREFIX}bird"),
                InlineKeyboardButton("🐹 Грызун", callback_data=f"{SPECIES_CALLBACK_PREFIX}rodent"),
            ],
        ]
    )


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
            "/add_pet — добавить питомца (имя, вид кнопкой, вес)\n"
            "/pets — список ваших питомцев\n"
            "/water — отметить воду и объём в мл\n"
            "/stats — сколько раз и сколько мл сегодня\n"
            "/stats_week — статистика за последние 7 дней\n"
            "/top — топ питомцев за неделю\n"
            "/help — эта справка\n"
            "/cancel — отменить добавление питомца или запись воды\n\n"
            "Данные хранятся в локальной базе SQLite (bot.db).",
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
                f"{index}. {pet['name']} — {pet['species']}, {pet['weight']} кг"
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

        raw_id = data[len(WATER_CALLBACK_PREFIX) :]
        try:
            pet_id = int(raw_id)
        except ValueError:
            await query.edit_message_text("Некорректный питомец. Откройте /water ещё раз.")
            return

        user_id = query.from_user.id
        pet = get_pet(user_id, pet_id)
        if pet is None:
            await query.edit_message_text("Этот питомец не найден. Обновите список /pets.")
            return

        if not log_water(user_id, pet_id):
            await query.edit_message_text("Не удалось сохранить отметку. Попробуйте снова.")
            return

        now = datetime.now().strftime("%H:%M")
        await query.edit_message_text(
            f"💧 Отмечено: {pet['name']} попил(а) воду в {now}.\n"
            "Статистика: /stats · за неделю: /stats_week · топ: /top"
        )
    except Exception as error:
        logger.exception("Ошибка в water_callback: %s", error)
        try:
            await query.edit_message_text("Произошла ошибка при сохранении отметки.")
        except Exception:
            pass


def _format_stats(title: str, pets: list[dict[str, Any]], counts: dict[int, int]) -> str:
    """Собирает текст статистики по списку питомцев."""
    lines = [title, ""]
    total = 0
    for pet in pets:
        times = counts.get(int(pet["id"]), 0)
        total += times
        lines.append(f"• {pet['name']}: {times} {_times_word(times)}")
    lines.append(f"\nВсего отметок: {total}")
    return "\n".join(lines)


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

        today_label = date.today().strftime("%d.%m.%Y")
        text = _format_stats(
            f"📊 Статистика за сегодня ({today_label}):",
            pets,
            today_stats(user_id),
        )
        await update.message.reply_text(text, reply_markup=MAIN_MENU)
    except Exception as error:
        logger.exception("Ошибка в /stats: %s", error)
        await _safe_reply(update, "Не получилось посчитать статистику.")


async def stats_week_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /stats_week — сколько раз каждый питомец пил за 7 дней. """
    try:
        user_id = update.effective_user.id
        pets = get_user_pets(user_id)
        if not pets:
            await update.message.reply_text(
                "Нет питомцев — нечего считать. Добавьте питомца: /add_pet",
                reply_markup=MAIN_MENU,
            )
            return

        text = _format_stats(
            "📅 Статистика за последние 7 дней:",
            pets,
            week_stats(user_id),
        )
        await update.message.reply_text(text, reply_markup=MAIN_MENU)
    except Exception as error:
        logger.exception("Ошибка в /stats_week: %s", error)
        await _safe_reply(update, "Не получилось посчитать статистику за неделю.")


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /top — рейтинг питомцев по числу отметок за неделю. """
    try:
        user_id = update.effective_user.id
        ranked = top_pets_week(user_id)
        if not ranked:
            await update.message.reply_text(
                "Нет питомцев для рейтинга. Добавьте питомца: /add_pet",
                reply_markup=MAIN_MENU,
            )
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = ["🏆 Топ питомцев за 7 дней:\n"]
        for index, (pet, times) in enumerate(ranked, start=1):
            medal = medals[index - 1] if index <= 3 else f"{index}."
            lines.append(
                f"{medal} {pet['name']} ({pet['species']}) — {times} {_times_word(times)}"
            )
        await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)
    except Exception as error:
        logger.exception("Ошибка в /top: %s", error)
        await _safe_reply(update, "Не получилось построить топ.")


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
        context.user_data.pop("new_pet_name", None)
        context.user_data.pop("new_pet_species", None)

        if pet is None:
            await update.message.reply_text(
                "Не получилось сохранить питомца в базу. Попробуйте /add_pet ещё раз.",
                reply_markup=MAIN_MENU,
            )
            return ConversationHandler.END

        await update.message.reply_text(
            f"✅ Питомец добавлен!\n\n"
            f"Имя: {pet['name']}\n"
            f"Вид: {pet['species']}\n"
            f"Вес: {pet['weight']} кг\n\n"
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

async def _delete_webhook(application: Application) -> None:
    """Перед polling снимаем webhook, иначе апдейты могут приходить дважды."""
    await application.bot.delete_webhook(drop_pending_updates=True)
    logger.info(
        "Webhook удалён. Хендлеров в группе 0: %s",
        len(application.handlers.get(0, [])),
    )


def build_application(token: str) -> Application:
    """Собирает Application и регистрирует каждый обработчик ровно один раз."""
    application = (
        Application.builder()
        .token(token)
        .post_init(_delete_webhook)
        .build()
    )

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
        block=True,
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
    application.add_handler(MessageHandler(filters.Regex("^📊 Сегодня$"), stats_command))
    application.add_handler(CommandHandler("stats_week", stats_week_command))
    application.add_handler(MessageHandler(filters.Regex("^📅 За неделю$"), stats_week_command))
    application.add_handler(CommandHandler("top", top_command))
    application.add_handler(MessageHandler(filters.Regex("^🏆 Топ$"), top_command))
    application.add_handler(
        CallbackQueryHandler(water_callback, pattern=f"^{WATER_CALLBACK_PREFIX}")
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))
    application.add_error_handler(error_handler)

    return application


def _ensure_event_loop() -> None:
    """Создаёт текущий event loop, если его ещё нет (Python 3.12+)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _acquire_singleton_lock() -> IO[str]:
    """Не даёт запустить второй процесс с тем же ботом."""
    lock_path = BASE_DIR / ".bot.lock"
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise SystemExit(
            "Бот уже запущен в другом процессе (терминал, IDE, сервер). "
            "Остановите лишний экземпляр — из-за него ответы приходят дважды."
        )
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def main() -> None:
    """Готовит БД, читает токен и запускает long polling."""
    token = os.getenv("BOT_TOKEN")
    if not token or token == "your_token_here":
        raise SystemExit(
            "Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен от @BotFather."
        )

    _lock_file = _acquire_singleton_lock()
    init_db()
    migrate_json_to_sqlite()

    _ensure_event_loop()
    application = build_application(token)
    logger.info("Бот Pet Water Tracker запущен, pid=%s", os.getpid())
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )

    _lock_file.close()


if __name__ == "__main__":
    main()
