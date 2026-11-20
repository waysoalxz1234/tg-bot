"""
Универсальный Telegram-бот: ИИ-ответчик + управление группами.

Возможности:
  • личный ассистент — отвечает владельцу через ИИ;
  • в группах: отвечает на упоминания и реплаи, если включён ИИ-режим;
  • модерация: мат/спам-фильтр, приветствие новичков, антифлуд,
    запись в mute, кик, выдача правил, закреп;
  • работает с учётом того, что бот должен быть админом в группе.

Запуск:  .venv/bin/python bot.py
Конфиг:  .env рядом с файлом (BOT_TOKEN, OWNER_ID, AI_*)
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from collections import defaultdict, deque
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, ChatPermissions, BotCommand
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web as aio_web
import ssl

# --- SSL-контекст для GigaChat (сертификаты Сбера) ---
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
# -----------------------------------------------------

# ─────────────────────────── конфиг ───────────────────────────

def load_env(path: Path = Path(__file__).with_name(".env")) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

load_env()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)

AI_ENABLED = os.getenv("AI_ENABLED", "1") == "1"
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
AI_SYSTEM = os.getenv(
    "AI_SYSTEM",
    "Ты — личный секретарь. Отвечай по-русски, кратко и по делу, обращайся на «вы». "
    "Помогай с планами, делами, заметками и напоминаниями. Если данных не хватает — "
    "задай один короткий уточняющий вопрос.",
)

BANNED_WORDS = [w.strip().lower() for w in os.getenv(
    "BANNED_WORDS",
    "мат1,мат2,казино,ставки,заработок из дома,криптосигналы",
).split(",") if w.strip()]

WELCOME = os.getenv(
    "WELCOME",
    "Добро пожаловать, {name}! Прочитайте правила: /rules",
)

FLOOD_LIMIT = int(os.getenv("FLOOD_LIMIT", "7"))    # сообщений
FLOOD_WINDOW = int(os.getenv("FLOOD_WINDOW", "10")) # за N секунд
MUTE_MINUTES = int(os.getenv("MUTE_MINUTES", "10"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан — заполните .env (пример в .env.example)")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ─────────────────────────── состояние ───────────────────────────

# chat_id -> {ai: bool}
chat_settings: dict[int, dict] = defaultdict(lambda: {"ai": True})
# (chat_id, user_id) -> deque[timestamps]
flood: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=50))
# (chat_id, user_id) -> последний текст (антиповтор)
last_msg: dict[tuple[int, int], str] = {}
# ─── состояние секретаря ───
# chat_id -> list[{"role","content"}] — память диалога
dialog_history: dict[int, list] = defaultdict(list)
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "16"))
# chat_id -> list[str] — заметки
notes: dict[int, list] = defaultdict(list)
# список напоминаний: (when_ts, chat_id, text)
reminders: list[dict] = []

# message_id -> {chat_id, user_id} для кнопок
mute_candidates: dict[int, dict] = {}

# ─────────────────────────── утилиты ───────────────────────────

async def is_admin(message: Message, user_id: int | None = None) -> bool:
    uid = user_id or (message.from_user.id if message.from_user else 0)
    if OWNER_ID and uid == OWNER_ID:
        return True
    try:
        member = await bot.get_chat_member(message.chat.id, uid)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def target_user(message: Message) -> tuple[int | None, str]:
    """Кого касается команда: реплай или аргумент."""
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, u.full_name
    return None, ""


def contains_banned(text: str) -> str | None:
    low = text.lower()
    for w in BANNED_WORDS:
        if w and w in low:
            return w
    return None


# ─── GigaChat: получение и обновление токена доступа ───
# GigaChat выдаёт access_token на 30 минут. Держим его в памяти
# и обновляем заранее.
_giga_token: dict = {"value": "", "expires": 0}


def _giga_is_used() -> bool:
    return "giga" in (AI_BASE_URL or "").lower()


def _giga_credentials() -> str:
    """GIGACHAT_KEY (готовый Authorization Key) или склейка ID:SECRET."""
    key = os.getenv("GIGACHAT_KEY", "").strip()
    if key:
        return key
    cid = os.getenv("GIGACHAT_CLIENT_ID", "").strip()
    sec = os.getenv("GIGACHAT_CLIENT_SECRET", "").strip()
    if cid and sec:
        import base64
        return base64.b64encode(f"{cid}:{sec}".encode()).decode()
    return AI_API_KEY  # на случай, если ключ положили в AI_API_KEY


def _giga_scope() -> str:
    return os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")


def get_giga_token(force: bool = False) -> str:
    """Возвращает действующий access_token, обновляя его при истечении."""
    import json as _json
    import time as _time
    import urllib.parse
    import urllib.request as _req
    import uuid

    now = _time.time()
    if not force and _giga_token["value"] and _giga_token["expires"] > now + 60:
        return _giga_token["value"]

    auth = _giga_credentials()
    if not auth:
        raise RuntimeError("no_credentials")

    data = urllib.parse.urlencode({"scope": _giga_scope()}).encode()
    request = _req.Request(
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {auth}",
        },
    )
    # ✅ Здесь добавлен context=_SSL_CTX — это исправляет SSL-ошибку
    with _req.urlopen(request, timeout=30, context=_SSL_CTX) as resp:
        payload = _json.loads(resp.read().decode())
    token = payload.get("access_token", "")
    if not token:
        raise RuntimeError("no_token")
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at > 1e12:
        expires_at = expires_at / 1000.0
    _giga_token["value"] = token
    _giga_token["expires"] = float(expires_at or (now + 1500))
    log.info("GigaChat: получен новый токен")
    return token


async def ai_reply(prompt: str, history: list[dict] | None = None) -> str:
    """Запрос к OpenAI-совместимому API или к GigaChat."""
    import json
    import urllib.error
    import urllib.request

    messages = [{"role": "system", "content": AI_SYSTEM}]
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": prompt})

    use_giga = _giga_is_used()

    if not AI_API_KEY and not use_giga:
        return ("ИИ не настроен: не задан AI_API_KEY. "
                "Откройте Render → Environment и добавьте ключ, либо поставьте AI_ENABLED=0.")

    payload = json.dumps({
        "model": AI_MODEL,
        "messages": messages,
        "max_tokens": 600,
    }).encode()

    def build_request(token: str):
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if use_giga:
            headers["Accept"] = "application/json"
        return urllib.request.Request(
            f"{AI_BASE_URL}/chat/completions",
            data=payload,
            headers=headers,
        )

    try:
        token = await asyncio.to_thread(get_giga_token) if use_giga else AI_API_KEY
    except Exception as e:
        log.exception("GigaChat auth error")
        # ✅ Теперь бот пришлёт понятную причину ошибки
        return f"Не удалось авторизоваться в GigaChat: {type(e).__name__}: {e}"

    try:
        req = build_request(token)
        try:
            # ✅ Первый запрос с context=_SSL_CTX
            with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # токен мог протухнуть раньше времени — обновляем один раз
            if use_giga and e.code in (401, 403):
                token = await asyncio.to_thread(get_giga_token, True)
                req = build_request(token)
                # ✅ Повторный запрос тоже с context=_SSL_CTX
                with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
                    data = json.loads(resp.read().decode())
            else:
                raise
        return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        log.error("AI HTTP %s: %s", e.code, body)
        hints = {
            400: "API не принял запрос — скорее всего неверное имя модели AI_MODEL.",
            401: "Ключ неверный или не подходит к этому AI_BASE_URL.",
            403: "Доступ запрещён: ключ без прав или модель недоступна для него.",
            404: "Не найдено: проверьте AI_BASE_URL (должен заканчиваться на /v1) и AI_MODEL.",
            429: "Лимит запросов исчерпан. Подождите или смените модель.",
            500: "Сбой на стороне сервиса ИИ. Попробуйте позже.",
            502: "Сервис ИИ недоступен. Попробуйте позже.",
            503: "Сервис ИИ перегружен. Попробуйте позже.",
        }
        hint = hints.get(e.code, "Проверьте AI_BASE_URL, AI_API_KEY и AI_MODEL в Environment.")
        return f"Ошибка ИИ ({e.code}). {hint}"
    except urllib.error.URLError as e:
        log.error("AI network error: %s", e)
        return "Нет связи с сервисом ИИ. Проверьте AI_BASE_URL в Environment."
    except Exception as e:
        log.exception("AI error")
        return f"Не получилось получить ответ: {type(e).__name__}"


# ─────────────────────────── команды ───────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            "Привет. Я бот-помощник.\n\n"
            "В личке просто пишите — отвечу.\n"
            "В группах: добавьте меня админом и включите режим командой /ai on."
        )
    else:
        await message.answer("Бот на связи. Команды: /rules, /ai, /id")


@router.message(Command("id"))
async def cmd_id(message: Message):
    uid = message.from_user.id if message.from_user else 0
    await message.answer(f"Ваш ID: <code>{uid}</code>\nChat ID: <code>{message.chat.id}</code>")


@router.message(Command("rules"))
async def cmd_rules(message: Message):
    await message.answer(
        "Правила чата:\n"
        "1. Уважение к участникам, без оскорблений.\n"
        "2. Без спама, рекламы и сторонних ссылок без согласования.\n"
        "3. Обсуждения — по теме чата.\n"
        "4. Нарушение → предупреждение, затем мут."
    )


@router.message(Command("ai"))
async def cmd_ai(message: Message, command: CommandObject):
    if message.chat.type != ChatType.PRIVATE and not await is_admin(message):
        await message.answer("Переключать режим может только админ.")
        return
    arg = (command.args or "").strip().lower()
    cur = chat_settings[message.chat.id]["ai"]
    if arg in ("on", "вкл", "1"):
        chat_settings[message.chat.id]["ai"] = True
        await message.answer("ИИ-ответы включены.")
    elif arg in ("off", "выкл", "0"):
        chat_settings[message.chat.id]["ai"] = False
        await message.answer("ИИ-ответы выключены.")
    else:
        await message.answer(f"ИИ-режим сейчас: {'вкл' if cur else 'выкл'}\nИспользуйте /ai on или /ai off")


@router.message(Command("mute"))
async def cmd_mute(message: Message,
