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
import logging
import os
import re
import time
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
    "Ты — дружелюбный и краткий ассистент в Telegram. Отвечай по-русски, "
    "по делу, без лишних вступлений. Если вопрос требует данных, которых нет, — "
    "честно скажи об этом.",
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


async def ai_reply(prompt: str, history: list[dict] | None = None) -> str:
    """Запрос к любому OpenAI-совместимому API."""
    import json
    import urllib.request

    messages = [{"role": "system", "content": AI_SYSTEM}]
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": prompt})

    payload = json.dumps({
        "model": AI_MODEL,
        "messages": messages,
        "max_tokens": 600,
        "temperature": 0.7,
    }).encode()

    req = urllib.request.Request(
        f"{AI_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()
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
    if not uid:
        await message.answer("Ответьте на сообщение нарушителя.")
        return
    minutes = int((command.args or str(MUTE_MINUTES)).strip() or MUTE_MINUTES)
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
async def cmd_pin(message: Message):
    if not await is_admin(message):
        return
    if not message.reply_to_message:
        await message.answer("Ответьте на сообщение, которое нужно закрепить.")
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
        answer = await ai_reply(text)
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
    ])


async def main():
    runner = await start_health_server()
    await set_commands()
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
