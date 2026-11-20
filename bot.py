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
    with _req.urlopen(request, timeout=30) as resp:
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
        return ("Не удалось авторизоваться в GigaChat. Проверьте GIGACHAT_KEY "
                "(или GIGACHAT_CLIENT_ID и GIGACHAT_CLIENT_SECRET) в Environment.")

    try:
        req = build_request(token)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # токен мог протухнуть раньше времени — обновляем один раз
            if use_giga and e.code in (401, 403):
                token = await asyncio.to_thread(get_giga_token, True)
                req = build_request(token)
                with urllib.request.urlopen(req, timeout=60) as resp:
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
async def cmd_mute(message: Message, command: CommandObject):
    if not await is_admin(message):
        return
    uid, name = target_user(message)
    args = (command.args or "").strip()
    if not uid:
        # в личке реплая нет: /mute <id> [минуты]
        parts = args.split()
        if message.chat.type == ChatType.PRIVATE and parts and parts[0].lstrip("-").isdigit():
            uid = int(parts[0])
            name = f"user {uid}"
            args = " ".join(parts[1:])
        else:
            await message.answer("Ответьте на сообщение нарушителя.")
            return
    minutes = int(args or MUTE_MINUTES)
    until = int(time.time()) + minutes * 60
    await bot.restrict_chat_member(
        message.chat.id, uid,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until,
    )
    await message.answer(f"{name} в муте на {minutes} мин.")


@router.message(Command("unmute"))
async def cmd_unmute(message: Message):
    if not await is_admin(message):
        return
    uid, name = target_user(message)
    if not uid:
        await message.answer("Ответьте на сообщение пользователя.")
        return
    await bot.restrict_chat_member(
        message.chat.id, uid,
        permissions=ChatPermissions(
            can_send_messages=True, can_send_media_messages=True,
            can_send_other_messages=True, can_add_web_page_previews=True,
        ),
    )
    await message.answer(f"С {name} мут снят.")


@router.message(Command("kick"))
async def cmd_kick(message: Message):
    if not await is_admin(message):
        return
    uid, name = target_user(message)
    if not uid:
        await message.answer("Ответьте на сообщение пользователя.")
        return
    await bot.ban_chat_member(message.chat.id, uid)
    await bot.unban_chat_member(message.chat.id, uid)
    await message.answer(f"{name} удалён из чата.")


@router.message(Command("ban"))
async def cmd_ban(message: Message):
    if not await is_admin(message):
        return
    uid, name = target_user(message)
    if not uid:
        await message.answer("Ответьте на сообщение пользователя.")
        return
    await bot.ban_chat_member(message.chat.id, uid)
    await message.answer(f"{name} забанен.")


@router.message(Command("pin"))
async def cmd_pin(message: Message, command_obj: CommandObject):
    if not await is_admin(message):
        return
    if not message.reply_to_message:
        if (command_obj.args or "").strip():
            sent = await message.answer(command_obj.args.strip())
            await bot.pin_chat_message(message.chat.id, sent.message_id)
            await message.answer("Закреплено.")
            return
        await message.answer(
            "Ответьте на сообщение, которое нужно закрепить, "
            "или напишите текст: <code>/pin важное объявление</code>"
        )
        return
    await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
    await message.answer("Закреплено.")


# ─────────────────────────── модерация ───────────────────────────

@router.message(F.new_chat_members)
async def on_join(message: Message):
    for u in message.new_chat_members:
        if u.is_bot:
            continue
        try:
            await message.answer(WELCOME.format(name=u.full_name))
        except Exception:
            pass


@router.message(F.left_chat_member)
async def on_leave(message: Message):
    pass


@router.message(F.text)
async def on_text(message: Message):
    if not message.text or not message.from_user or message.from_user.is_bot:
        return

    chat_id = message.chat.id
    uid = message.from_user.id
    key = (chat_id, uid)
    text = message.text.strip()

    # служебные команды уже отработали
    if text.startswith("/"):
        return

    # ── 1. стоп-слова в группах
    if message.chat.type != ChatType.PRIVATE:
        bad = contains_banned(text)
        if bad:
            if await is_admin(message):
                return
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer(
                f"{message.from_user.full_name}, такие сообщения здесь неуместны.",
                disable_notification=True,
            )
            return

    # ── 2. антифлуд и антиповтор (только группы)
    if message.chat.type != ChatType.PRIVATE and not await is_admin(message):
        now = time.time()
        q = flood[key]
        q.append(now)
        while q and now - q[0] > FLOOD_WINDOW:
            q.popleft()
        repeated = last_msg.get(key) == text
        last_msg[key] = text

        if len(q) >= FLOOD_LIMIT or (repeated and len(q) >= 3):
            try:
                await bot.restrict_chat_member(
                    chat_id, uid,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=int(now) + MUTE_MINUTES * 60,
                )
                await message.answer(
                    f"{message.from_user.full_name} — слишком часто. "
                    f"Мут на {MUTE_MINUTES} мин."
                )
            except Exception:
                log.warning("не удалось ограничить пользователя")
            flood[key].clear()
            return

    # ── 3. ответы ИИ
    if not AI_ENABLED:
        return

    if message.chat.type == ChatType.PRIVATE:
        if OWNER_ID and uid != OWNER_ID:
            return
        hist = dialog_history[chat_id]
        answer = await ai_reply(text, hist)
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": answer})
        if len(hist) > HISTORY_LIMIT * 2:
            del hist[: len(hist) - HISTORY_LIMIT * 2]
        await message.answer(answer)
        return

    if not chat_settings[chat_id]["ai"]:
        return

    me = await bot.me()
    addressed = False
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == me.id:
            addressed = True
    if f"@{me.username}" in text:
        addressed = True
        text = text.replace(f"@{me.username}", "").strip()

    if addressed and text:
        answer = await ai_reply(text)
        await message.reply(answer)


# ─────────────────────────── секретарь: заметки и напоминания ───────────────────────────

@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject):
    text = (command.args or "").strip()
    if not text:
        await message.answer("Использование: <code>/note купить хлеб</code>")
        return
    notes[message.chat.id].append(text)
    await message.answer(f"Записал. Всего заметок: {len(notes[message.chat.id])}")


@router.message(Command("notes"))
async def cmd_notes(message: Message):
    items = notes[message.chat.id]
    if not items:
        await message.answer("Заметок пока нет.")
        return
    body = "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))
    await message.answer(f"<b>Заметки</b>\n{body}")


@router.message(Command("delnote"))
async def cmd_delnote(message: Message, command: CommandObject):
    try:
        idx = int((command.args or "").strip())
    except ValueError:
        await message.answer("Укажите номер заметки: <code>/delnote 2</code>")
        return
    items = notes[message.chat.id]
    if 1 <= idx <= len(items):
        removed = items.pop(idx - 1)
        await message.answer(f"Удалил: {removed}")
    else:
        await message.answer("Такой заметки нет.")


def parse_when(arg: str) -> tuple[int, str] | None:
    """Понимает: 10m, 2h, 1d, 19:30, или 19:30 20.11"""
    arg = arg.strip()
    m = re.fullmatch(r"(\d+)([mhd])", arg)
    now = datetime.now()
    if m:
        num, unit = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=num), "h": timedelta(hours=num), "d": timedelta(days=num)}[unit]
        return int((now + delta).timestamp()), ""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?:\s+(\d{1,2})\.(\d{1,2}))?", arg)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if m.group(3):
            day, month = int(m.group(3)), int(m.group(4))
            target = now.replace(day=day, month=month, hour=hh, minute=mm, second=0, microsecond=0)
        else:
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
        return int(target.timestamp()), target.strftime("%d.%m %H:%M")
    return None


@router.message(Command("remind"))
async def cmd_remind(message: Message, command: CommandObject):
    args = (command.args or "").strip()
    if not args or " " not in args:
        await message.answer(
            "Формат: <code>/remind 30m позвонить маме</code>\n"
            "Можно так: <code>/remind 19:30 встреча</code> или <code>/remind 19:30 20.11 встреча</code>"
        )
        return
    when_raw, text = args.split(" ", 1)
    parsed = parse_when(when_raw)
    if not parsed:
        await message.answer("Не понял время. Примеры: 30m, 2h, 19:30, 19:30 20.11")
        return
    ts, human = parsed
    reminders.append({"when": ts, "chat_id": message.chat.id, "text": text.strip()})
    human = human or datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
    await message.answer(f"Напомню {human}: {text.strip()}")


@router.message(Command("reminders"))
async def cmd_reminders(message: Message):
    items = [r for r in reminders if r["chat_id"] == message.chat.id]
    if not items:
        await message.answer("Напоминаний нет.")
        return
    items.sort(key=lambda r: r["when"])
    body = "\n".join(
        f"• {datetime.fromtimestamp(r['when']).strftime('%d.%m %H:%M')} — {r['text']}" for r in items
    )
    await message.answer(f"<b>Напоминания</b>\n{body}")


async def reminder_loop():
    """Каждые 20 секунд проверяет, не пора ли что-то напомнить."""
    while True:
        try:
            now = time.time()
            due = [r for r in reminders if r["when"] <= now]
            for r in due:
                reminders.remove(r)
                try:
                    await bot.send_message(r["chat_id"], f"Напоминаю: {r['text']}")
                except Exception:
                    log.warning("не удалось отправить напоминание")
        except Exception:
            log.exception("reminder loop error")
        await asyncio.sleep(20)


# ─────────────────────────── меню команд ───────────────────────────


# ─────────────────────────── health-check для Render ───────────────────────────
# Render требует, чтобы Web Service слушал порт. Поднимаем минимальный HTTP-сервер,
# который отвечает 200 на любой запрос. Бот при этом работает через polling.

async def _health(request):
    return aio_web.Response(text="ok")

async def start_health_server():
    port = int(os.getenv("PORT", "0") or 0)
    if not port:
        return None
    app = aio_web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/healthz", _health)
    runner = aio_web.AppRunner(app)
    await runner.setup()
    site = aio_web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("health-сервер слушает порт %s", port)
    return runner


async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать"),
        BotCommand(command="rules", description="Правила чата"),
        BotCommand(command="ai", description="ИИ-ответы: on / off"),
        BotCommand(command="mute", description="Мут (реплай)"),
        BotCommand(command="unmute", description="Снять мут"),
        BotCommand(command="kick", description="Удалить из чата"),
        BotCommand(command="ban", description="Забанить"),
        BotCommand(command="pin", description="Закрепить сообщение"),
        BotCommand(command="id", description="Мои ID"),
        BotCommand(command="note", description="Записать заметку"),
        BotCommand(command="notes", description="Показать заметки"),
        BotCommand(command="remind", description="Напоминание: /remind 30m текст"),
        BotCommand(command="reminders", description="Список напоминаний"),
    ])


async def main():
    runner = await start_health_server()
    await set_commands()
    asyncio.create_task(reminder_loop())
    log.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        if runner:
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен")
