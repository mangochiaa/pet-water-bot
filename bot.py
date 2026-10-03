"""
Pet Water Tracker — Telegram-бот для учёта питья воды питомцами.

Запуск:
    python bot.py

Токен берётся из переменной окружения BOT_TOKEN (файл .env).
Данные хранятся в SQLite (bot.db).
При первом запуске старые pets.json и water_log.json переносятся в БД,
а схема старой базы автоматически дополняется новыми колонками.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import sqlite3
import warnings
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Any, Generator, IO, NamedTuple, Optional

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
from telegram.warnings import PTBUserWarning

# ---------------------------------------------------------------------------
# Настройки и константы
# ---------------------------------------------------------------------------

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "bot.db"
PETS_FILE = BASE_DIR / "pets.json"
WATER_LOG_FILE = BASE_DIR / "water_log.json"

# Состояния диалогов (у каждого ConversationHandler свой набор)
ASK_NAME, ASK_SPECIES, ASK_CUSTOM_SPECIES, ASK_WEIGHT, ASK_AGE = range(5)
ASK_WATER_AMOUNT = 5

# Префиксы callback_data у Inline-кнопок
WATER_CALLBACK_PREFIX = "water:"        # выбран питомец в /water
WATER_MARK_PREFIX = "water_mark:"       # «Просто отметка» без объёма
WATER_ML_PREFIX = "water_ml:"           # «Указать мл» — дальше ждём число
SPECIES_CALLBACK_PREFIX = "species:"    # выбор вида в /add_pet

# Границы допустимых значений
MAX_AMOUNT_ML = 2000
MAX_WEIGHT_KG = 500
MAX_AGE_YEARS = 50
MAX_TEXT_LEN = 50

# Код вида -> название по-русски (код хранится в pets.species_code)
SPECIES_CHOICES = {
    "dog": "Собака",
    "cat": "Кот",
    "rodent": "Грызун",
    "bird": "Птица",
    "other": "Другое",
}

SPECIES_EMOJI = {
    "dog": "🐶",
    "cat": "🐱",
    "rodent": "🐹",
    "bird": "🐦",
    "other": "❓",
}

# Как старые текстовые значения pets.species переводятся в код.
# Всё, чего нет в словаре, становится "other" с сохранением текста.
SPECIES_TEXT_TO_CODE = {
    "собака": "dog",
    "пёс": "dog",
    "пес": "dog",
    "dog": "dog",
    "кот": "cat",
    "кошка": "cat",
    "cat": "cat",
    "грызун": "rodent",
    "хомяк": "rodent",
    "rodent": "rodent",
    "птица": "bird",
    "попугай": "bird",
    "bird": "bird",
}

# Норма воды: мл на кг веса в сутки. None — расчёт недоступен.
BASE_COEFFICIENT_ML_PER_KG: dict[str, Optional[int]] = {
    "dog": 50,
    "cat": 50,
    "rodent": 100,
    "bird": 100,
    "other": None,
}

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Добавить питомца", "🐾 Мои питомцы"],
        ["💧 Попил воды", "📊 Сегодня"],
        ["📅 За неделю", "🏆 Топ"],
        ["📊 Норма воды", "❓ Помощь"],
    ],
    resize_keyboard=True,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx пишет в INFO полный URL запроса, а в нём есть токен бота — глушим
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Диалог записи мл начинается с Inline-кнопки, а продолжается текстом.
# PTB предупреждает, что при per_message=False такие кнопки не отслеживаются
# по отдельным сообщениям — для нас это и есть нужное поведение.
warnings.filterwarnings("ignore", message=r".*per_message.*", category=PTBUserWarning)


class PetStats(NamedTuple):
    """Сводка отметок по одному питомцу за период."""

    times: int          # сколько всего отметок (с объёмом и без)
    total_ml: float     # сумма amount_ml, NULL считается как 0
    measured: int       # сколько отметок было с указанным объёмом


EMPTY_STATS = PetStats(0, 0.0, 0)


# ---------------------------------------------------------------------------
# SQLite: подключение, схема, миграции
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
    """Создаёт таблицы (если их нет) и доводит старую схему до актуальной."""
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
                    created_at TEXT NOT NULL,
                    species_code TEXT,
                    custom_species TEXT,
                    age_years REAL
                );

                CREATE TABLE IF NOT EXISTS water_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pet_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    amount_ml REAL,
                    FOREIGN KEY (pet_id) REFERENCES pets(id)
                );
                """
            )
        migrate_pets_columns()
        migrate_water_log_amount()
        backfill_species_codes()
        logger.info("База данных готова: %s", DB_FILE)
    except sqlite3.Error as error:
        logger.exception("Не удалось подготовить базу: %s", error)
        raise SystemExit("Ошибка инициализации SQLite. Подробности в логе.")


def _table_columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    """Имя колонки -> строка PRAGMA table_info (там есть тип и флаг notnull)."""
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"]: row for row in rows}


def migrate_pets_columns() -> None:
    """Добавляет в pets колонки species_code, custom_species, age_years, если их нет."""
    new_columns = {
        "species_code": "TEXT",
        "custom_species": "TEXT",
        "age_years": "REAL",
    }
    try:
        with get_db() as connection:
            existing = _table_columns(connection, "pets")
            for column, column_type in new_columns.items():
                if column in existing:
                    continue
                connection.execute(f"ALTER TABLE pets ADD COLUMN {column} {column_type}")
                logger.info("Колонка pets.%s добавлена", column)
    except sqlite3.Error as error:
        logger.exception("Не удалось добавить колонки в pets: %s", error)
        raise


def migrate_water_log_amount() -> None:
    """
    Приводит water_log.amount_ml к виду «REAL, может быть NULL».

    NULL означает «отметка без объёма». Варианты старой схемы:
    - колонки нет совсем -> просто ALTER TABLE ADD COLUMN;
    - колонка есть, но с NOT NULL DEFAULT 0 (прошлая версия бота) ->
      SQLite не умеет снимать NOT NULL через ALTER, поэтому пересобираем
      таблицу в одной транзакции. Старые нули превращаем в NULL:
      объём для них никогда не вводился.
    """
    try:
        with get_db() as connection:
            columns = _table_columns(connection, "water_log")

            if "amount_ml" not in columns:
                connection.execute("ALTER TABLE water_log ADD COLUMN amount_ml REAL")
                logger.info("Колонка water_log.amount_ml добавлена")
                return

            if not columns["amount_ml"]["notnull"]:
                return

            connection.execute("BEGIN")
            connection.execute(
                """
                CREATE TABLE water_log_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pet_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    amount_ml REAL,
                    FOREIGN KEY (pet_id) REFERENCES pets(id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO water_log_new (id, pet_id, user_id, timestamp, amount_ml)
                SELECT id, pet_id, user_id, timestamp, NULLIF(amount_ml, 0)
                FROM water_log
                """
            )
            connection.execute("DROP TABLE water_log")
            connection.execute("ALTER TABLE water_log_new RENAME TO water_log")
        logger.info("water_log пересобрана: amount_ml теперь допускает NULL")
    except sqlite3.Error as error:
        logger.exception("Не удалось обновить water_log.amount_ml: %s", error)
        raise


def species_code_from_text(species: str) -> str:
    """Переводит произвольный текст вида («Кошка», «dog») в код из SPECIES_CHOICES."""
    return SPECIES_TEXT_TO_CODE.get(species.strip().lower(), "other")


def backfill_species_codes() -> None:
    """
    Заполняет species_code у питомцев, добавленных до появления кнопок.

    «Собака»/«Кот»/… -> dog/cat/…; всё остальное -> other,
    а исходный текст переносится в custom_species, чтобы не потерять его.
    """
    try:
        with get_db() as connection:
            rows = connection.execute(
                "SELECT id, species FROM pets WHERE species_code IS NULL"
            ).fetchall()
            for row in rows:
                text = str(row["species"] or "")
                code = species_code_from_text(text)
                custom = text.strip() if code == "other" and text.strip() else None
                connection.execute(
                    "UPDATE pets SET species_code = ?, custom_species = ? WHERE id = ?",
                    (code, custom, row["id"]),
                )
        if rows:
            logger.info("species_code заполнен у %s питомцев", len(rows))
    except sqlite3.Error as error:
        logger.exception("Не удалось заполнить species_code: %s", error)
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
                    species = str(pet.get("species", "не указан"))
                    code = species_code_from_text(species)
                    cursor = connection.execute(
                        """
                        INSERT INTO pets (user_id, name, species, weight, created_at,
                                          species_code, custom_species)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            str(pet.get("name", "Без имени")),
                            species,
                            float(weight or 0),
                            created_at,
                            code,
                            species if code == "other" else None,
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
                # В JSON объём не хранился — пишем NULL («без объёма»)
                connection.execute(
                    """
                    INSERT INTO water_log (pet_id, user_id, timestamp, amount_ml)
                    VALUES (?, ?, ?, NULL)
                    """,
                    (new_pet_id, user_id, timestamp),
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

PET_COLUMNS = (
    "id, user_id, name, species, weight, created_at, "
    "species_code, custom_species, age_years"
)


def get_user_pets(user_id: int) -> list[dict[str, Any]]:
    """Возвращает питомцев пользователя, от старых к новым."""
    try:
        with get_db() as connection:
            rows = connection.execute(
                f"SELECT {PET_COLUMNS} FROM pets WHERE user_id = ? ORDER BY id",
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
                f"SELECT {PET_COLUMNS} FROM pets WHERE id = ? AND user_id = ?",
                (pet_id, user_id),
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as error:
        logger.exception("Ошибка поиска питомца: %s", error)
        return None


def add_pet(
    user_id: int,
    name: str,
    species_code: str,
    custom_species: Optional[str],
    weight: float,
    age_years: float,
) -> Optional[dict[str, Any]]:
    """
    Добавляет питомца и возвращает созданную запись.

    В старую колонку species пишем человекочитаемое название
    («Собака» или текст пользователя для «Другое»): она NOT NULL
    и остаётся понятной при просмотре базы вручную.
    """
    created_at = datetime.now().isoformat(timespec="seconds")
    species = custom_species if species_code == "other" and custom_species else SPECIES_CHOICES[species_code]
    try:
        with get_db() as connection:
            cursor = connection.execute(
                """
                INSERT INTO pets (user_id, name, species, weight, created_at,
                                  species_code, custom_species, age_years)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, name, species, weight, created_at,
                 species_code, custom_species, age_years),
            )
            pet_id = cursor.lastrowid
        return {
            "id": pet_id,
            "user_id": user_id,
            "name": name,
            "species": species,
            "weight": weight,
            "created_at": created_at,
            "species_code": species_code,
            "custom_species": custom_species,
            "age_years": age_years,
        }
    except sqlite3.Error as error:
        logger.exception("Ошибка добавления питомца: %s", error)
        return None


def log_water(user_id: int, pet_id: int, amount_ml: Optional[float] = None) -> bool:
    """
    Пишет отметку «попил воду».

    amount_ml=None — «просто отметка», объём неизвестен (в БД NULL).
    False — питомец не найден или ошибка БД.
    """
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


def _stats_map(rows: list[sqlite3.Row]) -> dict[int, PetStats]:
    """pet_id -> PetStats."""
    return {
        int(row["pet_id"]): PetStats(
            int(row["times"]),
            float(row["total_ml"] or 0),
            int(row["measured"]),
        )
        for row in rows
    }


# SUM игнорирует NULL, а COALESCE подставляет 0, если объёма не было вовсе.
# COUNT(amount_ml) считает только отметки с указанным объёмом.
STATS_SELECT = """
    SELECT pet_id,
           COUNT(*) AS times,
           COALESCE(SUM(amount_ml), 0) AS total_ml,
           COUNT(amount_ml) AS measured
    FROM water_log
"""


def today_stats(user_id: int) -> dict[int, PetStats]:
    """Отметки и объём за сегодня (локальная дата)."""
    try:
        with get_db() as connection:
            rows = connection.execute(
                STATS_SELECT
                + """
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


def week_stats(user_id: int) -> dict[int, PetStats]:
    """Отметки и объём за последние 7 дней."""
    try:
        with get_db() as connection:
            rows = connection.execute(
                STATS_SELECT
                + """
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


def top_pets_week(user_id: int) -> list[tuple[dict[str, Any], PetStats]]:
    """Питомцы пользователя: сортировка по числу отметок за 7 дней, затем по мл."""
    pets = get_user_pets(user_id)
    stats = week_stats(user_id)
    ranked = [(pet, stats.get(int(pet["id"]), EMPTY_STATS)) for pet in pets]
    ranked.sort(key=lambda item: (-item[1].times, -item[1].total_ml, item[0]["name"]))
    return ranked


# ---------------------------------------------------------------------------
# Норма воды
# ---------------------------------------------------------------------------

def age_multiplier(age_years: Optional[float]) -> float:
    """Поправка на возраст: молодым и пожилым нужно больше воды.

    Возраст не указан (питомцы, добавленные до этой версии) — считаем взрослым.
    """
    if age_years is None:
        return 1.0
    if age_years < 1:
        return 1.3
    if age_years <= 7:
        return 1.0
    return 1.2


def recommended_ml(pet: dict[str, Any]) -> Optional[float]:
    """recommended_ml = weight_kg * base_coefficient * age_multiplier; None — недоступно."""
    coefficient = BASE_COEFFICIENT_ML_PER_KG.get(pet.get("species_code") or "other")
    if coefficient is None:
        return None
    return float(pet["weight"]) * coefficient * age_multiplier(pet.get("age_years"))


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------

def species_label(pet: dict[str, Any]) -> str:
    """Название вида для показа: своё для «Другое», иначе из SPECIES_CHOICES."""
    code = pet.get("species_code")
    if code == "other":
        return pet.get("custom_species") or pet.get("species") or "Другое"
    return SPECIES_CHOICES.get(code or "", pet.get("species") or "не указан")


def species_emoji(pet: dict[str, Any]) -> str:
    return SPECIES_EMOJI.get(pet.get("species_code") or "other", "❓")


def _format_number(value: float) -> str:
    """13.0 -> «13», 3.5 -> «3.5», 4.25 -> «4.25»."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_age(age_years: Optional[float]) -> str:
    """Возраст по-русски: «1 год», «3 года», «5 лет», «0.5 года»."""
    if age_years is None:
        return "возраст не указан"
    if age_years != int(age_years):
        return f"{_format_number(age_years)} года"
    years = int(age_years)
    n = years % 100
    if 11 <= n <= 14:
        word = "лет"
    elif n % 10 == 1:
        word = "год"
    elif 2 <= n % 10 <= 4:
        word = "года"
    else:
        word = "лет"
    return f"{years} {word}"


def _times_word(count: int) -> str:
    """Склонение слова «раз» для русского языка."""
    n = abs(count) % 100
    if 11 <= n <= 14:
        return "раз"
    last = n % 10
    if 2 <= last <= 4:
        return "раза"
    return "раз"


def _format_pet_stats(stats: PetStats) -> str:
    """«3 раза, 450 мл» — объём показываем, только если он где-то указан."""
    text = f"{stats.times} {_times_word(stats.times)}"
    if stats.measured:
        text += f", {_format_number(stats.total_ml)} мл"
    return text


def _format_stats(title: str, pets: list[dict[str, Any]], stats: dict[int, PetStats]) -> str:
    """Собирает текст статистики по списку питомцев."""
    lines = [title, ""]
    total_times = 0
    total_ml = 0.0
    for pet in pets:
        pet_stats = stats.get(int(pet["id"]), EMPTY_STATS)
        total_times += pet_stats.times
        total_ml += pet_stats.total_ml
        lines.append(f"• {pet['name']}: {_format_pet_stats(pet_stats)}")
    lines.append(f"\nВсего отметок: {total_times}")
    lines.append(f"Всего выпито: {_format_number(total_ml)} мл")
    lines.append("(отметки без объёма в сумму мл не входят)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def pets_keyboard(pets: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Клавиатура с кнопками по одному питомцу на строку."""
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{species_emoji(pet)} {pet['name']} ({species_label(pet)})",
                callback_data=f"{WATER_CALLBACK_PREFIX}{pet['id']}",
            )
        ]
        for pet in pets
    ]
    return InlineKeyboardMarkup(buttons)


def water_mode_keyboard(pet_id: int) -> InlineKeyboardMarkup:
    """После выбора питомца: отметить без объёма или ввести мл."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💧 Просто отметка", callback_data=f"{WATER_MARK_PREFIX}{pet_id}"),
                InlineKeyboardButton("📏 Указать мл", callback_data=f"{WATER_ML_PREFIX}{pet_id}"),
            ]
        ]
    )


def species_keyboard() -> InlineKeyboardMarkup:
    """Inline-кнопки выбора вида питомца."""

    def button(code: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            f"{SPECIES_EMOJI[code]} {SPECIES_CHOICES[code]}",
            callback_data=f"{SPECIES_CALLBACK_PREFIX}{code}",
        )

    return InlineKeyboardMarkup(
        [
            [button("dog"), button("cat"), button("rodent")],
            [button("bird"), button("other")],
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
            "Я Pet Water Tracker — помогу следить, как часто и сколько пьют ваши питомцы, "
            "и подскажу дневную норму воды.\n\n"
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
            "/add_pet — добавить питомца (имя, вид кнопкой, вес, возраст)\n"
            "/pets — список ваших питомцев\n"
            "/water — отметить воду: просто отметка или объём в мл\n"
            "/stats — сколько раз и сколько мл сегодня\n"
            "/stats_week — статистика за последние 7 дней\n"
            "/top — топ питомцев за неделю\n"
            "/norm — дневная норма воды и сколько выпито сегодня\n"
            "/help — эта справка\n"
            "/cancel — отменить добавление питомца или ввод мл\n\n"
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
                f"{index}. {species_emoji(pet)} {pet['name']} — {species_label(pet)}, "
                f"{_format_number(pet['weight'])} кг, {_format_age(pet.get('age_years'))}"
            )
        await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)
    except Exception as error:
        logger.exception("Ошибка в /pets: %s", error)
        await _safe_reply(update, "Не получилось загрузить список питомцев.")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /stats — сколько раз и сколько мл каждый питомец пил сегодня. """
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
    """ /stats_week — сколько раз и сколько мл каждый питомец пил за 7 дней. """
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
        for index, (pet, stats) in enumerate(ranked, start=1):
            medal = medals[index - 1] if index <= 3 else f"{index}."
            lines.append(
                f"{medal} {pet['name']} ({species_label(pet)}) — {_format_pet_stats(stats)}"
            )
        await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)
    except Exception as error:
        logger.exception("Ошибка в /top: %s", error)
        await _safe_reply(update, "Не получилось построить топ.")


async def norm_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /norm — рекомендуемая дневная норма воды и прогресс за сегодня. """
    try:
        user_id = update.effective_user.id
        pets = get_user_pets(user_id)
        if not pets:
            await update.message.reply_text(
                "Нет питомцев — не для кого считать норму. Добавьте питомца: /add_pet",
                reply_markup=MAIN_MENU,
            )
            return

        stats = today_stats(user_id)
        lines = ["📊 Дневная норма воды:\n"]
        has_unknown_age = False
        for pet in pets:
            header = (
                f"{species_emoji(pet)} {pet['name']} ({species_label(pet)}, "
                f"{_format_number(pet['weight'])} кг, {_format_age(pet.get('age_years'))})"
            )
            norm = recommended_ml(pet)
            if norm is None:
                lines.append(f"{header}: расчёт нормы недоступен")
                lines.append("")
                continue

            if pet.get("age_years") is None:
                has_unknown_age = True
            lines.append(f"{header}: {norm:.0f} мл/день")

            pet_stats = stats.get(int(pet["id"]), EMPTY_STATS)
            if pet_stats.measured:
                percent = pet_stats.total_ml / norm * 100 if norm > 0 else 0
                lines.append(
                    f"Сегодня выпил: {_format_number(pet_stats.total_ml)} мл "
                    f"({percent:.0f}% от нормы)"
                )
            lines.append("")

        if has_unknown_age:
            lines.append(
                "ℹ️ Для питомцев без возраста норма посчитана как для взрослых (1–7 лет)."
            )
        lines.append("Норма ориентировочная — при сомнениях посоветуйтесь с ветеринаром.")
        await update.message.reply_text("\n".join(lines).strip(), reply_markup=MAIN_MENU)
    except Exception as error:
        logger.exception("Ошибка в /norm: %s", error)
        await _safe_reply(update, "Не получилось рассчитать норму воды.")


# ---------------------------------------------------------------------------
# Отметка воды: /water -> питомец -> «Просто отметка» или «Указать мл»
# ---------------------------------------------------------------------------

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


def _pet_id_from_callback(data: str, prefix: str) -> Optional[int]:
    """Достаёт id питомца из callback_data вида «<prefix><id>»."""
    if not data.startswith(prefix):
        return None
    try:
        return int(data[len(prefix):])
    except ValueError:
        return None


async def water_pet_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажата кнопка питомца: предлагаем отметку без объёма или ввод мл."""
    query = update.callback_query
    try:
        await query.answer()
        pet_id = _pet_id_from_callback(query.data or "", WATER_CALLBACK_PREFIX)
        if pet_id is None:
            await query.edit_message_text("Некорректный питомец. Откройте /water ещё раз.")
            return

        pet = get_pet(query.from_user.id, pet_id)
        if pet is None:
            await query.edit_message_text("Этот питомец не найден. Обновите список /pets.")
            return

        await query.edit_message_text(
            f"{species_emoji(pet)} {pet['name']}: как отметить воду?",
            reply_markup=water_mode_keyboard(pet_id),
        )
    except Exception as error:
        logger.exception("Ошибка в water_pet_chosen: %s", error)
        try:
            await query.edit_message_text("Произошла ошибка. Откройте /water ещё раз.")
        except Exception:
            pass


async def water_simple_mark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«💧 Просто отметка» — сохраняем запись с amount_ml = NULL."""
    query = update.callback_query
    try:
        await query.answer()
        pet_id = _pet_id_from_callback(query.data or "", WATER_MARK_PREFIX)
        if pet_id is None:
            await query.edit_message_text("Некорректный питомец. Откройте /water ещё раз.")
            return

        user_id = query.from_user.id
        pet = get_pet(user_id, pet_id)
        if pet is None:
            await query.edit_message_text("Этот питомец не найден. Обновите список /pets.")
            return

        if not log_water(user_id, pet_id, None):
            await query.edit_message_text("Не удалось сохранить отметку. Попробуйте снова.")
            return

        now = datetime.now().strftime("%H:%M")
        await query.edit_message_text(
            f"💧 Отмечено: {pet['name']} попил(а) воду в {now} (без объёма).\n"
            "Статистика: /stats · за неделю: /stats_week · норма: /norm"
        )
    except Exception as error:
        logger.exception("Ошибка в water_simple_mark: %s", error)
        try:
            await query.edit_message_text("Произошла ошибка при сохранении отметки.")
        except Exception:
            pass


async def water_ml_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«📏 Указать мл» — запоминаем питомца и ждём число от пользователя."""
    query = update.callback_query
    try:
        await query.answer()
        pet_id = _pet_id_from_callback(query.data or "", WATER_ML_PREFIX)
        pet = get_pet(query.from_user.id, pet_id) if pet_id is not None else None
        if pet is None:
            await query.edit_message_text("Этот питомец не найден. Откройте /water ещё раз.")
            return ConversationHandler.END

        context.user_data["water_pet_id"] = pet_id
        await query.edit_message_text(
            f"Сколько мл выпил(а) {pet['name']}?\n"
            f"Введите число больше 0 и не больше {MAX_AMOUNT_ML}, например: 150\n"
            "Отменить — /cancel"
        )
        return ASK_WATER_AMOUNT
    except Exception as error:
        logger.exception("Ошибка в water_ml_start: %s", error)
        try:
            await query.edit_message_text("Произошла ошибка. Откройте /water ещё раз.")
        except Exception:
            pass
        return ConversationHandler.END


async def water_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Проверяем введённый объём и сохраняем отметку."""
    try:
        raw = (update.message.text or "").strip().replace(",", ".")
        try:
            amount = float(raw)
        except ValueError:
            await update.message.reply_text(
                f"Это не похоже на число. Введите объём в мл (больше 0, до {MAX_AMOUNT_ML}) или /cancel."
            )
            return ASK_WATER_AMOUNT

        if not 0 < amount <= MAX_AMOUNT_ML:
            await update.message.reply_text(
                f"Объём должен быть больше 0 и не больше {MAX_AMOUNT_ML} мл. Попробуйте снова."
            )
            return ASK_WATER_AMOUNT

        user_id = update.effective_user.id
        pet_id = context.user_data.pop("water_pet_id", None)
        pet = get_pet(user_id, pet_id) if pet_id is not None else None
        if pet is None:
            await update.message.reply_text(
                "Не понял, для какого питомца отметка. Откройте /water ещё раз.",
                reply_markup=MAIN_MENU,
            )
            return ConversationHandler.END

        amount = round(amount, 1)
        if not log_water(user_id, pet_id, amount):
            await update.message.reply_text(
                "Не удалось сохранить отметку. Попробуйте /water ещё раз.",
                reply_markup=MAIN_MENU,
            )
            return ConversationHandler.END

        now = datetime.now().strftime("%H:%M")
        await update.message.reply_text(
            f"💧 Отмечено: {pet['name']} выпил(а) {_format_number(amount)} мл в {now}.\n"
            "Статистика: /stats · за неделю: /stats_week · норма: /norm",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END
    except Exception as error:
        logger.exception("Ошибка в water_amount: %s", error)
        context.user_data.pop("water_pet_id", None)
        await _safe_reply(update, "Произошла ошибка при сохранении отметки.")
        return ConversationHandler.END


# ---------------------------------------------------------------------------
# Диалог добавления питомца: имя -> вид -> (своё название) -> вес -> возраст
# ---------------------------------------------------------------------------

NEW_PET_KEYS = ("new_pet_name", "new_pet_species_code", "new_pet_custom_species", "new_pet_weight")


def _clear_new_pet(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удаляет черновик питомца из user_data."""
    for key in NEW_PET_KEYS:
        context.user_data.pop(key, None)


async def add_pet_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало диалога: спрашиваем имя."""
    try:
        _clear_new_pet(context)
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
    """Сохраняем имя и показываем кнопки выбора вида."""
    try:
        name = (update.message.text or "").strip()
        if not name:
            await update.message.reply_text("Имя не должно быть пустым. Напишите, как зовут питомца.")
            return ASK_NAME
        if len(name) > MAX_TEXT_LEN:
            await update.message.reply_text(f"Слишком длинное имя — до {MAX_TEXT_LEN} символов.")
            return ASK_NAME

        context.user_data["new_pet_name"] = name
        await update.message.reply_text(
            f"Отлично, {name}! Кто это?",
            reply_markup=species_keyboard(),
        )
        return ASK_SPECIES
    except Exception as error:
        logger.exception("Ошибка в add_pet_name: %s", error)
        await _safe_reply(update, "Ошибка при сохранении имени. Попробуйте ещё раз или /cancel.")
        return ASK_NAME


async def add_pet_species_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Нажата кнопка вида. «Другое» — просим название текстом, иначе сразу вес."""
    query = update.callback_query
    try:
        await query.answer()
        code = (query.data or "")[len(SPECIES_CALLBACK_PREFIX):]
        if code not in SPECIES_CHOICES:
            await query.edit_message_text("Неизвестный вид. Выберите кнопкой:", reply_markup=species_keyboard())
            return ASK_SPECIES

        context.user_data["new_pet_species_code"] = code
        context.user_data.pop("new_pet_custom_species", None)

        if code == "other":
            await query.edit_message_text(
                "❓ Другое. Напишите, кто это, например: кролик, хорёк, черепаха."
            )
            return ASK_CUSTOM_SPECIES

        await query.edit_message_text(f"Вид: {SPECIES_EMOJI[code]} {SPECIES_CHOICES[code]}")
        await query.message.reply_text(
            "Сколько весит питомец в килограммах?\n"
            "Можно дробное число, например: 4.2"
        )
        return ASK_WEIGHT
    except Exception as error:
        logger.exception("Ошибка в add_pet_species_chosen: %s", error)
        try:
            await query.message.reply_text("Ошибка при выборе вида. Попробуйте ещё раз или /cancel.")
        except Exception:
            pass
        return ASK_SPECIES


async def add_pet_species_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пользователь написал текст вместо нажатия кнопки — напоминаем про кнопки."""
    try:
        await update.message.reply_text(
            "Пожалуйста, выберите вид кнопкой. Если нужного нет — нажмите «❓ Другое».",
            reply_markup=species_keyboard(),
        )
    except Exception as error:
        logger.exception("Ошибка в add_pet_species_text: %s", error)
    return ASK_SPECIES


async def add_pet_custom_species(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняем своё название вида («Другое») и спрашиваем вес."""
    try:
        custom = (update.message.text or "").strip()
        if not custom:
            await update.message.reply_text("Напишите, кто это, например: кролик.")
            return ASK_CUSTOM_SPECIES
        if len(custom) > MAX_TEXT_LEN:
            await update.message.reply_text(f"Слишком длинно — до {MAX_TEXT_LEN} символов.")
            return ASK_CUSTOM_SPECIES

        context.user_data["new_pet_custom_species"] = custom
        await update.message.reply_text(
            "Сколько весит питомец в килограммах?\n"
            "Можно дробное число, например: 4.2"
        )
        return ASK_WEIGHT
    except Exception as error:
        logger.exception("Ошибка в add_pet_custom_species: %s", error)
        await _safe_reply(update, "Ошибка при сохранении вида. Попробуйте ещё раз или /cancel.")
        return ASK_CUSTOM_SPECIES


async def add_pet_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Проверяем вес и спрашиваем возраст."""
    try:
        raw = (update.message.text or "").strip().replace(",", ".")
        try:
            weight = float(raw)
        except ValueError:
            await update.message.reply_text(
                "Это не похоже на число. Введите вес в кг, например: 5 или 3.5"
            )
            return ASK_WEIGHT

        if not 0 < weight <= MAX_WEIGHT_KG:
            await update.message.reply_text(
                f"Вес должен быть больше 0 и разумным (до {MAX_WEIGHT_KG} кг). Попробуйте снова."
            )
            return ASK_WEIGHT

        context.user_data["new_pet_weight"] = round(weight, 2)
        await update.message.reply_text(
            "Сколько питомцу лет?\n"
            "Можно дробное число: 0.5 — полгода, 1.5 — полтора года."
        )
        return ASK_AGE
    except Exception as error:
        logger.exception("Ошибка в add_pet_weight: %s", error)
        await _safe_reply(update, "Ошибка при сохранении веса. Попробуйте ещё раз или /cancel.")
        return ASK_WEIGHT


async def add_pet_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Проверяем возраст, сохраняем питомца и завершаем диалог."""
    try:
        raw = (update.message.text or "").strip().replace(",", ".")
        try:
            age = float(raw)
        except ValueError:
            await update.message.reply_text(
                "Это не похоже на число. Введите возраст в годах, например: 3 или 0.5"
            )
            return ASK_AGE

        if not 0 < age < MAX_AGE_YEARS:
            await update.message.reply_text(
                f"Возраст должен быть больше 0 и меньше {MAX_AGE_YEARS} лет. Попробуйте снова."
            )
            return ASK_AGE

        user_data = context.user_data
        species_code = user_data.get("new_pet_species_code")
        weight = user_data.get("new_pet_weight")
        if species_code not in SPECIES_CHOICES or weight is None:
            # Черновик потерялся (например, бот перезапускался посреди диалога)
            _clear_new_pet(context)
            await update.message.reply_text(
                "Данные питомца потерялись. Начните заново: /add_pet",
                reply_markup=MAIN_MENU,
            )
            return ConversationHandler.END

        pet = add_pet(
            update.effective_user.id,
            user_data.get("new_pet_name", "Без имени"),
            species_code,
            user_data.get("new_pet_custom_species"),
            weight,
            round(age, 1),
        )
        _clear_new_pet(context)

        if pet is None:
            await update.message.reply_text(
                "Не получилось сохранить питомца в базу. Попробуйте /add_pet ещё раз.",
                reply_markup=MAIN_MENU,
            )
            return ConversationHandler.END

        norm = recommended_ml(pet)
        norm_line = f"Норма воды: {norm:.0f} мл/день" if norm is not None else "Норма воды: расчёт недоступен"
        await update.message.reply_text(
            f"✅ Питомец добавлен!\n\n"
            f"Имя: {pet['name']}\n"
            f"Вид: {species_emoji(pet)} {species_label(pet)}\n"
            f"Вес: {_format_number(pet['weight'])} кг\n"
            f"Возраст: {_format_age(pet['age_years'])}\n"
            f"{norm_line}\n\n"
            "Отметить воду можно командой /water.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END
    except Exception as error:
        logger.exception("Ошибка в add_pet_age: %s", error)
        _clear_new_pet(context)
        await _safe_reply(update, "Не получилось сохранить питомца. Попробуйте /add_pet заново.")
        return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ /cancel — выход из любого диалога (добавление питомца или ввод мл). """
    _clear_new_pet(context)
    context.user_data.pop("water_pet_id", None)
    try:
        await update.message.reply_text(
            "Действие отменено. Меню ниже 👇",
            reply_markup=MAIN_MENU,
        )
    except Exception as error:
        logger.exception("Ошибка в /cancel: %s", error)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Вспомогательные обработчики
# ---------------------------------------------------------------------------

async def stale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка из старого сообщения (диалог уже закончился) — просто отвечаем."""
    try:
        await update.callback_query.answer("Эта кнопка устарела. Начните заново из меню.")
    except Exception as error:
        logger.error("Не удалось ответить на устаревшую кнопку: %s", error)


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

    text_input = filters.TEXT & ~filters.COMMAND

    add_pet_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("add_pet", add_pet_start),
            MessageHandler(filters.Regex("^➕ Добавить питомца$"), add_pet_start),
        ],
        states={
            ASK_NAME: [MessageHandler(text_input, add_pet_name)],
            ASK_SPECIES: [
                CallbackQueryHandler(add_pet_species_chosen, pattern=f"^{SPECIES_CALLBACK_PREFIX}"),
                MessageHandler(text_input, add_pet_species_text),
            ],
            ASK_CUSTOM_SPECIES: [MessageHandler(text_input, add_pet_custom_species)],
            ASK_WEIGHT: [MessageHandler(text_input, add_pet_weight)],
            ASK_AGE: [MessageHandler(text_input, add_pet_age)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        block=True,
    )

    # Ввод мл: начинается с кнопки «📏 Указать мл», дальше ждём число
    water_amount_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(water_ml_start, pattern=rf"^{WATER_ML_PREFIX}\d+$"),
        ],
        states={
            ASK_WATER_AMOUNT: [MessageHandler(text_input, water_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        block=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.Regex("^❓ Помощь$"), help_command))
    application.add_handler(add_pet_conversation)
    application.add_handler(water_amount_conversation)
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
    application.add_handler(CommandHandler("norm", norm_command))
    application.add_handler(MessageHandler(filters.Regex("^📊 Норма воды$"), norm_command))
    application.add_handler(
        CallbackQueryHandler(water_pet_chosen, pattern=rf"^{WATER_CALLBACK_PREFIX}\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(water_simple_mark, pattern=rf"^{WATER_MARK_PREFIX}\d+$")
    )
    # Любая другая кнопка (например, выбор вида после /cancel) — «устарела»
    application.add_handler(CallbackQueryHandler(stale_callback))
    application.add_handler(MessageHandler(text_input, unknown_text))
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
