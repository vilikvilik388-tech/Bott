import asyncio
import logging
import os
import uuid
import time
import re
from datetime import datetime
from typing import Optional, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ===========================
# FIX FOR PYDROID 3 / JUPYTER
# ===========================
import nest_asyncio
nest_asyncio.apply()

# ===========================
# CONFIGURATION
# ===========================
BOT_TOKEN = "8521451025:AAH_aZm5AqYILnpcbLh05PwsfyujY365bNg"
ADMIN_IDS = [1232628862]
COMPLAINT_COOLDOWN_SECONDS = 120
DB_NAME = "complaints.db"

# ===========================
# LOGGING
# ===========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ===========================
# DATABASE (aiosqlite)
# ===========================
import aiosqlite

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                room TEXT NOT NULL,
                description TEXT NOT NULL,
                photo_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'Новая',
                timestamp INTEGER NOT NULL
            )
        """)
        await db.commit()

async def add_complaint(user_id: int, room: str, description: str, photo_file_id: Optional[str] = None) -> str:
    complaint_id = str(uuid.uuid4())[:8].upper()
    timestamp = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO complaints (id, user_id, room, description, photo_file_id, status, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (complaint_id, user_id, room, description, photo_file_id, "Новая", timestamp),
        )
        await db.commit()
    return complaint_id

async def get_user_complaints(user_id: int, limit: int = 5):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, room, status, timestamp FROM complaints WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_complaint_by_id(complaint_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM complaints WHERE id = ?", (complaint_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def update_complaint_status(complaint_id: str, status: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE complaints SET status = ? WHERE id = ?", (status, complaint_id))
        await db.commit()

async def get_stats():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT room, COUNT(*) as count FROM complaints GROUP BY room ORDER BY count DESC LIMIT 10"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_all_complaints(limit: int = 10):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM complaints ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

# ===========================
# ANTI-SPAM
# ===========================
last_complaint_time: dict[int, int] = {}

def can_send_complaint(user_id: int) -> bool:
    now = int(time.time())
    last = last_complaint_time.get(user_id, 0)
    if now - last < COMPLAINT_COOLDOWN_SECONDS:
        return False
    last_complaint_time[user_id] = now
    return True

def get_cooldown_remaining(user_id: int) -> int:
    now = int(time.time())
    last = last_complaint_time.get(user_id, 0)
    remaining = COMPLAINT_COOLDOWN_SECONDS - (now - last)
    return max(0, remaining)

# ===========================
# STATUS EMOJI MAP
# ===========================
STATUS_EMOJI = {
    "Новая": "🆕",
    "Принято": "✅",
    "В работе": "🛠",
    "Решено": "✔️",
}

STATUS_COLORS = {
    "Новая": "🔴",
    "Принято": "🟡",
    "В работе": "🔵",
    "Решено": "🟢",
}

# ===========================
# KEYBOARDS
# ===========================
def main_menu_keyboard(user_id: int):
    buttons = [
        ["📢 Пожаловаться"],
        ["📊 Мои жалобы", "ℹ️ Помощь"],
    ]
    if user_id in ADMIN_IDS:
        buttons.append(["🛠 Админ-панель"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def cancel_keyboard():
    return ReplyKeyboardMarkup([["❌ Отменить"]], resize_keyboard=True)

def photo_choice_keyboard():
    return ReplyKeyboardMarkup([["📷 Пропустить фото"], ["❌ Отменить"]], resize_keyboard=True)

def admin_panel_keyboard():
    return ReplyKeyboardMarkup(
        [["📈 Статистика", "📋 Все жалобы"], ["⬅️ Назад в меню"]],
        resize_keyboard=True,
    )

def status_inline_keyboard(complaint_id: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Принято", callback_data=f"status|{complaint_id}|Принято"),
                InlineKeyboardButton("🛠 В работе", callback_data=f"status|{complaint_id}|В работе"),
                InlineKeyboardButton("✔️ Решено", callback_data=f"status|{complaint_id}|Решено"),
            ]
        ]
    )

# ===========================
# FORMATTERS
# ===========================
def format_timestamp(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")

def format_complaint_message(complaint: dict, for_admin: bool = False) -> str:
    ts = format_timestamp(complaint["timestamp"])
    status = complaint["status"]
    emoji = STATUS_EMOJI.get(status, "📌")
    color = STATUS_COLORS.get(status, "⚪")

    text = (
        f"{color} <b>Жалоба #{complaint['id']}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🚪 <b>Комната/этаж:</b> {complaint['room']}\n"
        f"📝 <b>Описание:</b> {complaint['description']}\n"
        f"📅 <b>Время:</b> {ts}\n"
        f"{emoji} <b>Статус:</b> {status}"
    )

    if for_admin:
        text += "\n\n👤 <i>Пользователь скрыт</i>"

    return text

def format_cooldown_bar(remaining: int, total: int = COMPLAINT_COOLDOWN_SECONDS) -> str:
    filled = int((1 - remaining / total) * 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {remaining}с"

# ===========================
# CONVERSATION STATES
# ===========================
STATE_ROOM, STATE_DESCRIPTION, STATE_PHOTO = range(3)

# ===========================
# HANDLERS
# ===========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    welcome_text = (
        f"👋 <b>Привет, {user.first_name}!</b>\n\n"
        f"🌙 Добро пожаловать в <b>Службу тишины</b> — анонимный сервис для жителей общежития.\n\n"
        f"<b>Что ты можешь сделать:</b>\n"
        f"  📢 Пожаловаться на шум — быстро и анонимно\n"
        f"  📊 Отслеживать статус своих жалоб\n"
        f"  🔒 Быть уверенным в полной конфиденциальности\n\n"
        f"<i>Твоё имя и данные никогда не передаются администраторам.</i>\n\n"
        f"Выбери действие ниже 👇"
    )

    await update.message.reply_text(
        welcome_text,
        reply_markup=main_menu_keyboard(user_id),
        parse_mode="HTML",
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    help_text = (
        "ℹ️ <b>Как пользоваться ботом</b>\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"<b>🎯 Для жителей общежития:</b>\n\n"
        f"1️⃣ Нажми <b>📢 Пожаловаться</b>\n"
        f"2️⃣ Укажи номер комнаты или этаж\n"
        f"3️⃣ Опиши проблему (шум, музыка, крики...)\n"
        f"4️⃣ Приложи фото (или пропусти этот шаг)\n"
        f"5️⃣ Жди уведомления о смене статуса!\n\n"
        f"<b>📊 Мои жалобы</b> — посмотреть последние 5 жалоб и их статус\n\n"
        f"<b>⚡ Ограничения:</b>\n"
        f"• 1 жалоба в {COMPLAINT_COOLDOWN_SECONDS // 60} минут (анти-спам)\n"
        f"• Максимум 5 жалоб в истории\n\n"
        f"<b>🔒 Анонимность:</b>\n"
        f"Администратор видит только комнату и описание. Твой профиль скрыт."
    )

    if user_id in ADMIN_IDS:
        help_text += (
            "\n\n<b>🛠 Для администраторов:</b>\n"
            f"• <b>🛠 Админ-панель</b> — доступ к статистике\n"
            f"• Inline-кнопки под жалобами меняют статус\n"
            f"• Пользователь получает уведомление об изменении"
        )

    await update.message.reply_text(
        help_text,
        reply_markup=main_menu_keyboard(user_id),
        parse_mode="HTML",
    )

async def complain_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not can_send_complaint(user_id):
        remaining = get_cooldown_remaining(user_id)
        bar = format_cooldown_bar(remaining)

        await update.message.reply_text(
            f"⏳ <b>Подожди ещё немного!</b>\n\n"
            f"{bar}\n\n"
            f"Перед следующей жалобой осталось <b>{remaining} секунд</b>.\n"
            f"Это защита от спама — спасибо за понимание! 🙏",
            reply_markup=main_menu_keyboard(user_id),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    msg = await update.message.reply_text("📝 <b>Новая жалоба</b>\n\nЗагружаю форму...")
    await asyncio.sleep(0.5)

    await msg.edit_text(
        "📝 <b>Шаг 1 из 3</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🚪 <b>Укажи номер комнаты или этаж,</b>\n"
        f"где нарушается тишина:\n\n"
        f"<i>Например: 305, 4 этаж, кухня 2</i>",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML",
    )
    return STATE_ROOM

async def complain_room(update: Update, context: ContextTypes.DEFAULT_TYPE):
    room = update.message.text.strip()

    if room == "❌ Отменить":
        await update.message.reply_text(
            "❌ <b>Отменено.</b> Возвращаю в меню...",
            reply_markup=main_menu_keyboard(update.effective_user.id),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if not room or len(room) > 50:
        await update.message.reply_text(
            "⚠️ Номер комнаты слишком короткий или длинный. Попробуй ещё раз:",
            reply_markup=cancel_keyboard(),
        )
        return STATE_ROOM

    context.user_data["complaint_room"] = room

    await update.message.reply_text(
        "📝 <b>Шаг 2 из 3</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"✅ Комната: <b>{room}</b>\n\n"
        f"📝 <b>Опиши проблему подробно:</b>\n"
        f"<i>Например: Громкая музыка с 23:00, крики в коридоре, хлопанье дверьми...</i>",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML",
    )
    return STATE_DESCRIPTION

async def complain_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "❌ Отменить":
        await update.message.reply_text(
            "❌ <b>Отменено.</b> Возвращаю в меню...",
            reply_markup=main_menu_keyboard(update.effective_user.id),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if not text or len(text) < 5:
        await update.message.reply_text(
            "⚠️ Описание слишком короткое. Расскажи подробнее:",
            reply_markup=cancel_keyboard(),
        )
        return STATE_DESCRIPTION

    if len(text) > 500:
        await update.message.reply_text(
            "⚠️ Описание слишком длинное (макс. 500 символов). Сократи и попробуй снова:",
            reply_markup=cancel_keyboard(),
        )
        return STATE_DESCRIPTION

    context.user_data["complaint_description"] = text
    room = context.user_data["complaint_room"]

    await update.message.reply_text(
        "📝 <b>Шаг 3 из 3</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"✅ Комната: <b>{room}</b>\n"
        f"✅ Описание: <b>{text[:50]}{'...' if len(text) > 50 else ''}</b>\n\n"
        f"📷 <b>Отправь фото</b> (если есть)\n"
        f"или нажми <b>📷 Пропустить фото</b>",
        reply_markup=photo_choice_keyboard(),
        parse_mode="HTML",
    )
    return STATE_PHOTO

async def complain_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    room = context.user_data.get("complaint_room", "")
    description = context.user_data.get("complaint_description", "")
    photo_file_id = None

    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
    elif update.message.text and update.message.text.strip() == "📷 Пропустить фото":
        photo_file_id = None
    elif update.message.text and update.message.text.strip() == "❌ Отменить":
        await update.message.reply_text(
            "❌ <b>Отменено.</b> Возвращаю в меню...",
            reply_markup=main_menu_keyboard(user_id),
            parse_mode="HTML",
        )
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            "⚠️ Отправь фото или нажми <b>📷 Пропустить фото</b>",
            reply_markup=photo_choice_keyboard(),
            parse_mode="HTML",
        )
        return STATE_PHOTO

    complaint_id = await add_complaint(user_id, room, description, photo_file_id)

    text = format_complaint_message({
        "id": complaint_id,
        "room": room,
        "description": description,
        "status": "Новая",
        "timestamp": int(time.time()),
    }, for_admin=True)

    msg_map = []
    for admin_id in ADMIN_IDS:
        try:
            if photo_file_id:
                msg = await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=photo_file_id,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=status_inline_keyboard(complaint_id),
                )
            else:
                msg = await context.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=status_inline_keyboard(complaint_id),
                )
            msg_map.append((admin_id, msg.message_id))
        except Exception as e:
            logger.error(f"Failed to send to admin {admin_id}: {e}")

    key = f"msg_map_{complaint_id}"
    context.bot_data[key] = msg_map

    confirm_text = (
        f"✅ <b>Жалоба #{complaint_id} отправлена!</b>\n\n"
        f"🚪 Комната: {room}\n"
        f"📌 Статус: 🆕 Новая\n\n"
        f"<i>Ты получишь уведомление, когда администратор изменит статус.</i>\n\n"
        f"Спасибо за помощь в поддержании тишины! 🌙"
    )

    await update.message.reply_text(
        confirm_text,
        reply_markup=main_menu_keyboard(user_id),
        parse_mode="HTML",
    )

    context.user_data.clear()
    return ConversationHandler.END

async def my_complaints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    complaints = await get_user_complaints(user_id, limit=5)

    if not complaints:
        await update.message.reply_text(
            "📭 <b>У тебя пока нет жалоб</b>\n\n"
            f"Нажми <b>📢 Пожаловаться</b>, чтобы создать первую.",
            reply_markup=main_menu_keyboard(user_id),
            parse_mode="HTML",
        )
        return

    lines = ["📊 <b>Твои последние жалобы:</b>\n"]
    for c in complaints:
        ts = format_timestamp(c["timestamp"])
        emoji = STATUS_EMOJI.get(c["status"], "📌")
        lines.append(
            f"{emoji} <code>#{c['id']}</code>\n"
            f"   🚪 {c['room']} | {c['status']}\n"
            f"   📅 {ts}\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=main_menu_keyboard(user_id),
        parse_mode="HTML",
    )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        await update.message.reply_text(
            "🚫 <b>Доступ запрещён</b>\n\n"
            f"Этот раздел только для администраторов.",
            reply_markup=main_menu_keyboard(user_id),
            parse_mode="HTML",
        )
        return

    stats_data = await get_stats()
    total_complaints = sum(s["count"] for s in stats_data) if stats_data else 0

    panel_text = (
        "🛠 <b>Админ-панель</b>\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"📊 Всего жалоб: <b>{total_complaints}</b>\n"
        f"👤 Администраторов: <b>{len(ADMIN_IDS)}</b>\n\n"
        f"<b>Доступные команды:</b>\n"
        f"• 📈 Статистика — топ комнат\n"
        f"• 📋 Все жалобы — последние 10\n\n"
        f"Выбери действие ниже 👇"
    )

    await update.message.reply_text(
        panel_text,
        reply_markup=admin_panel_keyboard(),
        parse_mode="HTML",
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        return

    stats_data = await get_stats()

    if not stats_data:
        await update.message.reply_text(
            "📭 <b>Пока нет данных</b>\n\n"
            f"Жалобы ещё не поступали.",
            reply_markup=admin_panel_keyboard(),
            parse_mode="HTML",
        )
        return

    lines = ["📈 <b>Топ комнат по жалобам:</b>\n"]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    for i, s in enumerate(stats_data[:10], 1):
        medal = medals[i-1] if i <= 10 else f"{i}."
        bar = "█" * min(s["count"], 10)
        lines.append(f"{medal} <b>{s['room']}</b> — {s['count']} {bar}")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=admin_panel_keyboard(),
        parse_mode="HTML",
    )

async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        return

    complaints = await get_all_complaints(limit=10)

    if not complaints:
        await update.message.reply_text(
            "📭 <b>Пока нет жалоб</b>",
            reply_markup=admin_panel_keyboard(),
            parse_mode="HTML",
        )
        return

    lines = ["📋 <b>Последние 10 жалоб:</b>\n"]

    for c in complaints:
        ts = format_timestamp(c["timestamp"])
        emoji = STATUS_EMOJI.get(c["status"], "📌")
        lines.append(
            f"{emoji} <code>#{c['id']}</code>\n"
            f"   🚪 {c['room']} | {c['status']}\n"
            f"   📅 {ts}\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=admin_panel_keyboard(),
        parse_mode="HTML",
    )

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(
        "👋 <b>Главное меню</b>\n\n"
        f"Выбери действие ниже 👇",
        reply_markup=main_menu_keyboard(user_id),
        parse_mode="HTML",
    )

async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split("|")
    if len(data) != 3:
        await query.answer("❌ Ошибка данных", show_alert=True)
        return

    _, complaint_id, new_status = data

    complaint = await get_complaint_by_id(complaint_id)
    if not complaint:
        await query.edit_message_text("❌ Жалоба не найдена.")
        return

    await update_complaint_status(complaint_id, new_status)

    key = f"msg_map_{complaint_id}"
    msg_map = context.bot_data.get(key, [])
    text = format_complaint_message({**complaint, "status": new_status}, for_admin=True)

    for admin_chat_id, message_id in msg_map:
        try:
            if complaint.get("photo_file_id"):
                await context.bot.edit_message_caption(
                    chat_id=admin_chat_id,
                    message_id=message_id,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=status_inline_keyboard(complaint_id),
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=status_inline_keyboard(complaint_id),
                )
        except Exception as e:
            logger.warning(f"Could not edit admin message: {e}")

    user_id = complaint["user_id"]
    try:
        emoji = STATUS_EMOJI.get(new_status, "📌")
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"📢 <b>Обновление статуса!</b>\n\n"
                f"Жалоба <code>#{complaint_id}</code>\n"
                f"{emoji} Новый статус: <b>{new_status}</b>\n\n"
                f"Спасибо за терпение! 🙏"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Could not notify user {user_id}: {e}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ <b>Отменено.</b> Возвращаю в меню...",
        reply_markup=main_menu_keyboard(update.effective_user.id),
        parse_mode="HTML",
    )
    context.user_data.clear()
    return ConversationHandler.END

async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤔 <b>Не понял команду</b>\n\n"
        f"Используй кнопки меню ниже 👇",
        reply_markup=main_menu_keyboard(update.effective_user.id),
        parse_mode="HTML",
    )

# ===========================
# MAIN
# ===========================
async def main():
    await init_db()

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📢 Пожаловаться$"), complain_start)],
        states={
            STATE_ROOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, complain_room)],
            STATE_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, complain_description)],
            STATE_PHOTO: [
                MessageHandler(filters.PHOTO, complain_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, complain_photo),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^❌ Отменить$"), cancel),
        ],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.Regex("^📊 Мои жалобы$"), my_complaints))
    application.add_handler(MessageHandler(filters.Regex("^ℹ️ Помощь$"), help_command))
    application.add_handler(MessageHandler(filters.Regex("^🛠 Админ-панель$"), admin_panel))
    application.add_handler(MessageHandler(filters.Regex("^📈 Статистика$"), stats_command))
    application.add_handler(MessageHandler(filters.Regex("^📋 Все жалобы$"), all_command))
    application.add_handler(MessageHandler(filters.Regex("^⬅️ Назад в меню$"), back_to_menu))
    application.add_handler(CallbackQueryHandler(status_callback, pattern=r"^status\|"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))

    logger.info("🚀 Bot started successfully!")
    await application.run_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
