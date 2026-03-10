import asyncio
import logging
import os
import random
from urllib.parse import quote
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ChatJoinRequestHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "0"))
CHANNEL_ID   = int(os.environ["CHANNEL_ID"])
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "")
PAYMENT_LINK = os.environ.get("PAYMENT_LINK", "")
VIDEO_1_ID   = os.environ.get("VIDEO_1_ID", "")
VIDEO_2_ID   = os.environ.get("VIDEO_2_ID", "")

VIDEO_DELETE_DELAY = 20
CHAT_DELETE_DELAY  = 300  # 5 minutes
HEART_EFFECT_ID    = "5159385139981059251"
BASE_VIDEO_COUNT   = 16568

# Per-user state: uid -> {"messages": [], "video_count": 16568}
channel_users = {}
channel_name_cache = ""

async def get_channel_name(bot):
    global channel_name_cache
    if channel_name_cache:
        return channel_name_cache
    try:
        chat = await bot.get_chat(CHANNEL_ID)
        channel_name_cache = chat.title or "OUR"
    except Exception:
        channel_name_cache = "OUR"
    return channel_name_cache

def get_user(uid):
    return channel_users.get(uid)

def upsert_user(uid):
    if uid not in channel_users:
        channel_users[uid] = {"messages": [], "video_count": BASE_VIDEO_COUNT}
    else:
        channel_users[uid]["video_count"] += random.randint(50, 200)
        channel_users[uid]["messages"] = []
    return channel_users[uid]

def share_url():
    return "https://t.me/share/url?url=" + quote(CHANNEL_LINK) + "&text=" + quote("join our exclusive group")

def make_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f4e4  SHARE FOR MORE", url=share_url())],
        [InlineKeyboardButton("\U0001f4b3  PAY FOR ACCESS \u2014 \u20b1499", url=PAYMENT_LINK)],
    ])

async def schedule_delete(bot, chat_id, message_ids, delay):
    await asyncio.sleep(delay)
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass

async def send_content(bot, chat_id, state):
    channel_name = await get_channel_name(bot)
    vid_count = f"{state['video_count']:,}"

    # Heart with effect
    heart_msg_id = None
    try:
        heart_msg = await bot.send_message(
            chat_id=chat_id,
            text="\u2764\ufe0f",
            message_effect_id=HEART_EFFECT_ID,
        )
        heart_msg_id = heart_msg.message_id
    except Exception as e:
        logger.warning("Heart effect error: " + str(e))

    # Videos
    video_msgs = []
    for label, vid_id in [
        ("VIDEO_1_ID", VIDEO_1_ID),
        ("VIDEO_2_ID", VIDEO_2_ID),
    ]:
        if not vid_id:
            logger.warning(label + " is empty!")
            continue
        try:
            msg = await bot.send_video(chat_id=chat_id, video=vid_id, protect_content=True, supports_streaming=True)
            video_msgs.append(msg.message_id)
            logger.info(label + " sent to " + str(chat_id))
        except Exception as e:
            logger.error(label + " error: " + str(e))

    # Promo text with personal video count
    text = (
        "\u26a0\ufe0f *" + channel_name.upper() + " \u2014 CHANNEL IS PRIVATE*\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
        "\U0001f34c\U0001f4a6 *SHARE = FREE CONTENT*\n\n"
        "\U0001f4ca *0 / 2 SHARES COMPLETED*\n\n"
        "\U0001f4e2 Share this channel to *2 groups* to unlock\n"
        "access to *" + vid_count + " exclusive videos*\n\n"
        "\u2705 Verification is *automatic*\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "\U0001f512 *Or unlock full access instantly below*"
    )

    info = await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=make_buttons())

    # Schedule deletions - Videos = fast delete (20 sec)
    if video_msgs:
        asyncio.create_task(schedule_delete(bot, chat_id, video_msgs, VIDEO_DELETE_DELAY))
    
    # Heart + Promo = 5 minutes delay
    other_msgs = []
    if heart_msg_id:
        other_msgs.append(heart_msg_id)
    other_msgs.append(info.message_id)
    asyncio.create_task(schedule_delete(bot, chat_id, other_msgs, CHAT_DELETE_DELAY))

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    join_req = update.chat_join_request
    user = join_req.from_user
    if join_req.chat.id != CHANNEL_ID:
        return
    logger.info("Join request: " + str(user.id) + " (" + str(user.full_name) + ")")
    state = upsert_user(user.id)
    await send_content(context.bot, user.id, state)

async def auto_reply_share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        return
    state = get_user(uid)
    if state is None:
        return
    # Bump their video count on every chat
    state["video_count"] += random.randint(10, 50)
    msg = await update.message.reply_text("SHARE!")
    
    # Schedule deletion for these messages
    messages_to_delete = [update.message.message_id, msg.message_id]
    asyncio.create_task(schedule_delete(context.bot, update.effective_chat.id, messages_to_delete, CHAT_DELETE_DELAY))

async def get_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends a video to bot → bot replies with the correct file_id."""
    if update.effective_user.id != ADMIN_ID:
        return
    if update.message and update.message.video:
        fid = update.message.video.file_id
        await update.message.reply_text(
            "✅ *file_id for this bot:*\n\n`" + fid + "`",
            parse_mode="Markdown"
        )

async def test_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("Testing videos...")
    for label, vid_id in [("VIDEO_1_ID", VIDEO_1_ID), ("VIDEO_2_ID", VIDEO_2_ID)]:
        if not vid_id:
            await update.message.reply_text(label + " EMPTY!")
            continue
        try:
            await context.bot.send_video(chat_id=update.effective_chat.id, video=vid_id, protect_content=True, supports_streaming=True)
            await update.message.reply_text(label + " OK!")
        except Exception as e:
            await update.message.reply_text(label + " FAILED: " + str(e))

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("testvideo", test_video))
    app.add_handler(MessageHandler(filters.VIDEO & filters.User(ADMIN_ID), get_file_id))
    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    app.add_handler(MessageHandler(filters.ALL, auto_reply_share))
    logger.info("Bot running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
