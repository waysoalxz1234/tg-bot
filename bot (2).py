"""
Telegram-бот: ИИ-ответчик на GigaChat + управление группами.

Конфиг (Render → Environment):
  BOT_TOKEN        — токен из BotFather
  OWNER_ID         — ваш ID (/id)
  GIGACHAT_KEY     — Authorization Key из кабинета GigaChat
  AI_MODEL         — по умолчанию GigaChat
  AI_ENABLED       — 1 или 0
"""

import asyncio
import base64
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, ChatPermissions, Message
from aiohttp import web as aio_web


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
AI_MODEL = os.getenv("AI_MODEL", "GigaChat").strip() or "GigaChat"
AI_SYSTEM = os.getenv(
    "AI_SYSTEM",
    "Ты — личный секретарь. Отвечай по-русски, кратко и по делу, обращайся на «вы». "
    "Помогай с планами, делами, заметками и напоминаниями.",
)

GIGA_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGA_API_URL = "https://api.giga.chat/v1"

BANNED_WORDS = [w.strip().lower() for w in os.getenv(
    "BANNED_WORDS", "мат1,мат2,казино,ставки",
).split(",") if w.strip()]

WELCOME = os.getenv("WELCOME", "Добро пожаловать, {name}! Прочитайте правила: /rules")
FLOOD_LIMIT = int(os.getenv("FLOOD_LIMIT", "7"))
FLOOD_WINDOW = int(os.getenv("FLOOD_WINDOW", "10"))
MUTE_MINUTES = int(os.getenv("MUTE_MINUTES", "10"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "16"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

chat_settings: dict[int, dict] = defaultdict(lambda: {"ai": True})
flood: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=50))
last_msg: dict[tuple[int, int], str] = {}
dialog_history: dict[int, list] = defaultdict(list)
notes: dict[int, list] = defaultdict(list)
reminders: list[dict] = []


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


# ─────────────────────────── GigaChat ───────────────────────────

_giga_token: dict = {"value": "", "expires": 0.0}
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def giga_credentials() -> str:
    key = os.getenv("GIGACHAT_KEY", "").strip()
    if key:
        return key
    cid = os.getenv("GIGACHAT_CLIENT_ID", "").strip()
    sec = os.getenv("GIGACHAT_CLIENT_SECRET", "").strip()
    if cid and sec:
        return base64.b64encode(f"{cid}:{sec}".encode()).decode()
    return os.getenv("AI_API_KEY", "").strip()


def giga_scope() -> str:
    return os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip() or "GIGACHAT_API_PERS"


def get_giga_token(force: bool = False) -> str:
    now = time.time()
    if not force and _giga_token["value"] and _giga_token["expires"] > now + 90:
        return _giga_token["value"]

    auth = giga_credentials()
    if not auth:
        raise RuntimeError("не задан GIGACHAT_KEY")

    body = urllib.parse.urlencode({"scope": giga_scope()}).encode()
    request = urllib.request.Request(
        GIGA_OAUTH_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {auth}",
        },
    )
    with urllib.request.urlopen(request, timeout=30, context=_ssl_ctx) as resp:
        payload = json.loads(resp.read().decode())

    token = payload.get("access_token", "")
    if not token:
        raise RuntimeError("GigaChat не выдал access_token")

    expires_at = payload.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at > 1e12:
        expires_at = expires_at / 1000.0
    _giga_token["value"] = token
    _giga_token["expires"] = float(expires_at or (now + 1500))
    log.info("GigaChat: получен новый токен")
    return token


def _post_chat(token: str, messages: list[dict]) -> str:
    payload = json.dumps({
        "model": AI_MODEL,
        "messages": messages,
        "max_tokens": 600,
    }).encode()

    request = urllib.request.Request(
        f"{GIGA_API_URL}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60, context=_ssl_ctx) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()


async def ai_reply(prompt: str, history: list[dict] | None = None) -> str:
    messages = [{"role": "system", "content": AI_SYSTEM}]
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": prompt})

    try:
        token = await asyncio.to_thread(get_giga_token)
    except Exception as e:
        log.exception("GigaChat auth error")
        return f"Не удалось авторизоваться в GigaChat: {e}. Проверьте GIGACHAT_KEY."

    try:
        return await asyncio.to_thread(_post_chat, token, messages)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            try:
                token = await asyncio.to_thread(get_giga_token, True)
                return await asyncio.to_thread(_post_chat, token, messages)
            except Exception:
                log.exception("GigaChat retry failed")
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        log.error("GigaChat HTTP %s: %s", e.code, body)
        hints = {
            400: "GigaChat не принял запрос — проверьте AI_MODEL.",
            401: "Ключ GIGACHAT_KEY неверный или истёк.",
            403: "Доступ запрещён: проверьте GIGACHAT_SCOPE.",
            404: "Модель не найдена — проверьте AI_MODEL.",
            429: "Слишком много запросов. Подождите минуту.",
            500: "Сбой на стороне GigaChat. Попробуйте позже.",
            503: "GigaChat перегружен. Попробуйте позже.",
        }
        return f"Ошибка ИИ ({e.code}). {hints.get(e.code, 'Проверьте настройки GigaChat.')}"
    except urllib.error.URLError as e:
        log.error("GigaChat network error: %s", e)
        return "Нет связи с GigaChat. Попробуйте позже."
    except Exception as e:
        log.exception("AI error")
        return f"Не получилось получить ответ: {type(e).__name__}"


@router.message(Command("start"))
async def cmd_start(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        dialog_history[(message.chat.id, message.from_user.id)].clear()
        await message.answer(
            "Привет. Я бот-помощник на GigaChat.\n\n"
            "Просто пишите — отвечу на любой вопрос.\n"
            "В группах: добавьте меня админом, и я буду следить за порядком.\n\n"
            "Команды: /rules /ai /note /remind /id"
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
        await message.answer(f"ИИ-режим: {'вкл' if cur else 'выкл'}\n/ai on или /ai off")


@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject):
    if not await is_admin(message):
        return
    uid, name = target_user(message)
    args = (command.args or "").strip()
    if not uid:
        parts = args.split()
        if parts and parts[0].lstrip("-").isdigit():
            uid = int(parts[0])
            name = f"user {uid}"
            args = " ".join(parts[1:])
        else:
            await message.answer("Ответьте на сообщение нарушителя.")
            return
    try:
        minutes = int(args or MUTE_MINUTES)
    except ValueError:
        minutes = MUTE_MINUTES
    await bot.restrict_chat_member(
        message.chat.id, uid,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=int(time.time()) + minutes * 60,
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
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
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
async def cmd_pin(message: Message, command: CommandObject):
    if not await is_admin(message):
        return
    if not message.reply_to_message:
        if (command.args or "").strip():
            sent = await message.answer(command.args.strip())
            await bot.pin_chat_message(message.chat.id, sent.message_id)
            await message.answer("Закреплено.")
            return
        await message.answer("Ответьте на сообщение или напишите: /pin текст")
        return
    await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
    await message.answer("Закреплено.")


@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject):
    text = (command.args or "").strip()
    if not text:
        await message.answer("Использование: /note купить хлеб")
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
        await message.answer("Укажите номер: /delnote 2")
        return
    items = notes[message.chat.id]
    if 1 <= idx <= len(items):
        await message.answer(f"Удалил: {items.pop(idx - 1)}")
    else:
        await message.answer("Такой заметки нет.")


def parse_when(arg: str) -> tuple[int, str] | None:
    arg = arg.strip()
    now = datetime.now()

    m = re.fullmatch(r"(\d+)([mhd])", arg)
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
            "Формат: /remind 30m позвонить маме\n"
            "Ещё: /remind 19:30 встреча или /remind 19:30 20.11 встреча"
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
    while True:
        try:
            now = time.time()
            for r in [x for x in reminders if x["when"] <= now]:
                reminders.remove(r)
                try:
                    await bot.send_message(r["chat_id"], f"Напоминаю: {r['text']}")
                except Exception:
                    log.warning("не удалось отправить напоминание")
        except Exception:
            log.exception("reminder loop error")
        await asyncio.sleep(20)


@router.message(F.new_chat_members)
async def on_join(message: Message):
    for u in message.new_chat_members:
        if u.is_bot:
            continue
        try:
            await message.answer(WELCOME.format(name=u.full_name))
        except Exception:
            pass


@router.message(F.text)
async def on_text(message: Message):
    if not message.text or not message.from_user or message.from_user.is_bot:
        return

    chat_id = message.chat.id
    uid = message.from_user.id
    text = message.text.strip()

    if text.startswith("/"):
        return

    if message.chat.type != ChatType.PRIVATE:
        if contains_banned(text) and not await is_admin(message):
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer(
                f"{message.from_user.full_name}, такие сообщения здесь неуместны.",
                disable_notification=True,
            )
            return

    if message.chat.type != ChatType.PRIVATE and not await is_admin(message):
        now = time.time()
        q = flood[(chat_id, uid)]
        q.append(now)
        while q and now - q[0] > FLOOD_WINDOW:
            q.popleft()
        repeated = last_msg.get((chat_id, uid)) == text
        last_msg[(chat_id, uid)] = text

        if len(q) >= FLOOD_LIMIT or (repeated and len(q) >= 3):
            try:
                await bot.restrict_chat_member(
                    chat_id, uid,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=int(now) + MUTE_MINUTES * 60,
                )
                await message.answer(
                    f"{message.from_user.full_name} — слишком часто. Мут на {MUTE_MINUTES} мин."
                )
            except Exception:
                log.warning("не удалось ограничить пользователя")
            q.clear()
            return

    if not AI_ENABLED:
        return

    if message.chat.type == ChatType.PRIVATE:
        hist = dialog_history[(chat_id, uid)]
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
    if me.username and f"@{me.username}" in text:
        addressed = True
        text = text.replace(f"@{me.username}", "").strip()

    if addressed and text:
        hist = dialog_history[(chat_id, uid)]
        answer = await ai_reply(text, hist)
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": answer})
        if len(hist) > HISTORY_LIMIT * 2:
            del hist[: len(hist) - HISTORY_LIMIT * 2]
        await message.reply(answer)


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
