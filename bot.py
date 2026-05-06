import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "").strip()
PAYMENT_LINK = os.environ.get("PAYMENT_LINK", "").strip()

# Optional defaults from env
ENV_VIDEO_1_ID = os.environ.get("VIDEO_1_ID", "").strip()
ENV_VIDEO_2_ID = os.environ.get("VIDEO_2_ID", "").strip()

# File where saved IDs live
# Use /data/video_ids.json if you have a persistent volume mounted at /data
VIDEO_STORE_PATH = Path(os.environ.get("VIDEO_STORE_PATH", "video_ids.json"))

VIDEO_DELETE_DELAY = int(os.environ.get("VIDEO_DELETE_DELAY", "20"))
TEXT_DELETE_DELAY = int(os.environ.get("TEXT_DELETE_DELAY", "300"))

channel_name_cache = ""


def clean(value: str) -> str:
    return (value or "").strip()


def mask_file_id(file_id: str) -> str:
    file_id = clean(file_id)
    if not file_id:
        return "(empty)"
    if len(file_id) <= 40:
        return file_id
    return f"{file_id[:22]}...{file_id[-12:]}"


def load_video_ids() -> dict:
    data = {
        "VIDEO_1_ID": clean(ENV_VIDEO_1_ID),
        "VIDEO_2_ID": clean(ENV_VIDEO_2_ID),
    }

    if VIDEO_STORE_PATH.exists():
        try:
            raw = json.loads(VIDEO_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data["VIDEO_1_ID"] = clean(raw.get("VIDEO_1_ID", data["VIDEO_1_ID"]))
                data["VIDEO_2_ID"] = clean(raw.get("VIDEO_2_ID", data["VIDEO_2_ID"]))
                logger.info("Loaded saved video IDs from %s", VIDEO_STORE_PATH)
        except Exception as e:
            logger.warning("Could not load %s: %s", VIDEO_STORE_PATH, e)

    return data


def save_video_ids(data: dict) -> None:
    payload = {
        "VIDEO_1_ID": clean(data.get("VIDEO_1_ID", "")),
        "VIDEO_2_ID": clean(data.get("VIDEO_2_ID", "")),
    }
    VIDEO_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    VIDEO_STORE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Saved video IDs to %s", VIDEO_STORE_PATH)


VIDEO_IDS = load_video_ids()


def get_video_pairs():
    return [
        ("VIDEO_1_ID", clean(VIDEO_IDS.get("VIDEO_1_ID", ""))),
        ("VIDEO_2_ID", clean(VIDEO_IDS.get("VIDEO_2_ID", ""))),
    ]


async def get_channel_name(bot):
    global channel_name_cache
    if channel_name_cache:
        return channel_name_cache

    try:
        chat = await bot.get_chat(CHANNEL_ID)
        channel_name_cache = chat.title or "OUR CHANNEL"
    except Exception:
        channel_name_cache = "OUR CHANNEL"

    return channel_name_cache


def share_url() -> str:
    if not CHANNEL_LINK:
        return "https://t.me"
    return (
        "https://t.me/share/url?url="
        + quote(CHANNEL_LINK)
        + "&text="
        + quote("Join this channel")
    )


def make_buttons():
    rows = [
        [InlineKeyboardButton("SHARE 3 TIMES TO ACCESS", url=share_url())]
    ]

    if PAYMENT_LINK:
        rows.append([InlineKeyboardButton("INSTANT ACCESS", url=PAYMENT_LINK)])

    return InlineKeyboardMarkup(rows)


async def schedule_delete(bot, chat_id: int, message_ids: list[int], delay: int):
    await asyncio.sleep(delay)
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


async def send_content(bot, chat_id: int):
    channel_name = await get_channel_name(bot)

    sent_video_message_ids = []

    for label, vid_id in get_video_pairs():
        if not vid_id:
            logger.warning("%s is empty", label)
            continue

        try:
            msg = await bot.send_video(
                chat_id=chat_id,
                video=vid_id,
                supports_streaming=True,
                protect_content=True,
            )
            sent_video_message_ids.append(msg.message_id)
            logger.info("%s sent to %s", label, chat_id)
        except Exception as e:
            logger.error("%s failed: %s", label, e)

    text = (
        f"⚠️ *{channel_name.upper()} — CHANNEL IS PRIVATE*\n"
        f"──────────────────\n\n"
        f"📤 *Tap the button below to share*\n\n"
        f"📊 *0 / 3 SHARES COMPLETED*\n\n"
        f"Use the button below if you want quicker access.\n\n"
        f"──────────────────"
    )

    info = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=make_buttons(),
    )

    if sent_video_message_ids:
        asyncio.create_task(
            schedule_delete(bot, chat_id, sent_video_message_ids, VIDEO_DELETE_DELAY)
        )

    asyncio.create_task(
        schedule_delete(bot, chat_id, [info.message_id], TEXT_DELETE_DELAY)
    )


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    join_req = update.chat_join_request
    if not join_req:
        return

    if join_req.chat.id != CHANNEL_ID:
        return

    user = join_req.from_user
    logger.info("Join request from %s (%s)", user.id, user.full_name)

    try:
        await send_content(context.bot, user.id)
    except Exception as e:
        logger.error("Failed to send content to %s: %s", user.id, e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text(
        "Admin commands:\n"
        "/showvideos\n"
        "/testvideo\n"
        "/clearvideo1\n"
        "/clearvideo2\n"
        "/setvideo1  (reply to a video)\n"
        "/setvideo2  (reply to a video)\n\n"
        "You can also send a video with caption:\n"
        "/setvideo1\n"
        "or\n"
        "/setvideo2"
    )


async def show_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    msg = (
        "Current saved video IDs\n\n"
        f"VIDEO_1_ID:\n`{clean(VIDEO_IDS.get('VIDEO_1_ID', '')) or '(empty)'}`\n\n"
        f"VIDEO_2_ID:\n`{clean(VIDEO_IDS.get('VIDEO_2_ID', '')) or '(empty)'}`\n\n"
        f"Store path:\n`{VIDEO_STORE_PATH}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def clear_video1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    VIDEO_IDS["VIDEO_1_ID"] = ""
    save_video_ids(VIDEO_IDS)
    await update.message.reply_text("VIDEO_1_ID cleared.")


async def clear_video2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    VIDEO_IDS["VIDEO_2_ID"] = ""
    save_video_ids(VIDEO_IDS)
    await update.message.reply_text("VIDEO_2_ID cleared.")


async def test_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text("Testing saved videos...")

    for label, vid_id in get_video_pairs():
        if not vid_id:
            await update.message.reply_text(f"{label} EMPTY")
            continue

        try:
            await context.bot.send_video(
                chat_id=update.effective_chat.id,
                video=vid_id,
                supports_streaming=True,
                protect_content=True,
            )
            await update.message.reply_text(f"{label} OK")
        except Exception as e:
            await update.message.reply_text(f"{label} FAILED: {e}")


async def set_video1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message or not update.message.reply_to_message or not update.message.reply_to_message.video:
        await update.message.reply_text("Reply to a video with /setvideo1")
        return

    fid = clean(update.message.reply_to_message.video.file_id)
    VIDEO_IDS["VIDEO_1_ID"] = fid
    save_video_ids(VIDEO_IDS)

    await update.message.reply_text(
        f"VIDEO_1_ID saved.\n\n`{fid}`",
        parse_mode="Markdown",
    )


async def set_video2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message or not update.message.reply_to_message or not update.message.reply_to_message.video:
        await update.message.reply_text("Reply to a video with /setvideo2")
        return

    fid = clean(update.message.reply_to_message.video.file_id)
    VIDEO_IDS["VIDEO_2_ID"] = fid
    save_video_ids(VIDEO_IDS)

    await update.message.reply_text(
        f"VIDEO_2_ID saved.\n\n`{fid}`",
        parse_mode="Markdown",
    )


async def admin_video_receiver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not update.message or not update.message.video:
        return

    fid = clean(update.message.video.file_id)
    caption = clean(update.message.caption).lower()

    if caption == "/setvideo1":
        VIDEO_IDS["VIDEO_1_ID"] = fid
        save_video_ids(VIDEO_IDS)
        await update.message.reply_text(
            f"VIDEO_1_ID saved.\n\n`{fid}`",
            parse_mode="Markdown",
        )
        return

    if caption == "/setvideo2":
        VIDEO_IDS["VIDEO_2_ID"] = fid
        save_video_ids(VIDEO_IDS)
        await update.message.reply_text(
            f"VIDEO_2_ID saved.\n\n`{fid}`",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        "Video received.\n\n"
        f"`{fid}`\n\n"
        "To save it automatically, send the video with caption:\n"
        "/setvideo1\n"
        "or\n"
        "/setvideo2",
        parse_mode="Markdown",
    )


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("showvideos", show_videos))
    app.add_handler(CommandHandler("testvideo", test_video))
    app.add_handler(CommandHandler("clearvideo1", clear_video1))
    app.add_handler(CommandHandler("clearvideo2", clear_video2))
    app.add_handler(CommandHandler("setvideo1", set_video1))
    app.add_handler(CommandHandler("setvideo2", set_video2))

    app.add_handler(MessageHandler(filters.VIDEO & filters.User(ADMIN_ID), admin_video_receiver))
    app.add_handler(ChatJoinRequestHandler(handle_join_request, chat_id=CHANNEL_ID))

    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
