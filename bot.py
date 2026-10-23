import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
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
logging.getLogger("httpx").setLevel(logging.WARNING)
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

# File where known users live (everyone who chatted / sent a join request)
# Use /data/users.json if you have a persistent volume mounted at /data
USER_STORE_PATH = Path(os.environ.get("USER_STORE_PATH", "users.json"))

VIDEO_DELETE_DELAY = int(os.environ.get("VIDEO_DELETE_DELAY", "20"))
TEXT_DELETE_DELAY = int(os.environ.get("TEXT_DELETE_DELAY", "300"))

# Broadcast pacing: Telegram allows ~30 msgs/sec globally.
# 0.05s sleep = ~20/sec, safe headroom.
BROADCAST_SLEEP = float(os.environ.get("BROADCAST_SLEEP", "0.05"))

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


def atomic_write_json(path: Path, payload) -> None:
    """Write JSON safely: temp file + rename, so a crash mid-write
    never corrupts the store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Video ID store
# ---------------------------------------------------------------------------

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
    atomic_write_json(VIDEO_STORE_PATH, payload)
    logger.info("Saved video IDs to %s", VIDEO_STORE_PATH)


VIDEO_IDS = load_video_ids()


# ---------------------------------------------------------------------------
# User store  (everyone who chatted with the bot or sent a join request)
# ---------------------------------------------------------------------------

def load_users() -> dict:
    """Returns {user_id_str: {"name": str, "username": str,
    "first_seen": int, "last_seen": int, "blocked": bool}}"""
    if USER_STORE_PATH.exists():
        try:
            raw = json.loads(USER_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                logger.info("Loaded %d users from %s", len(raw), USER_STORE_PATH)
                return raw
        except Exception as e:
            logger.warning("Could not load %s: %s", USER_STORE_PATH, e)
    return {}


USERS = load_users()
_users_dirty = False
_users_lock = asyncio.Lock()


def save_users() -> None:
    atomic_write_json(USER_STORE_PATH, USERS)


async def flush_users_loop():
    """Background task: writes users.json only when there are changes,
    every 5 seconds, instead of on every message. Mas mabilis ito
    kesa mag-save per user."""
    global _users_dirty
    while True:
        await asyncio.sleep(5)
        if _users_dirty:
            async with _users_lock:
                try:
                    save_users()
                    _users_dirty = False
                except Exception as e:
                    logger.error("Failed to save users: %s", e)


def track_user(user) -> None:
    """Record/refresh a user in the store. Cheap: in-memory only,
    flushed to disk by the background loop."""
    global _users_dirty
    if not user or user.is_bot:
        return

    uid = str(user.id)
    now = int(time.time())
    entry = USERS.get(uid)

    if entry is None:
        USERS[uid] = {
            "name": user.full_name or "",
            "username": user.username or "",
            "first_seen": now,
            "last_seen": now,
            "blocked": False,
        }
        logger.info("New user tracked: %s (%s)", uid, user.full_name)
    else:
        entry["name"] = user.full_name or entry.get("name", "")
        entry["username"] = user.username or entry.get("username", "")
        entry["last_seen"] = now
        entry["blocked"] = False  # they interacted again, so not blocked

    _users_dirty = True


def mark_blocked(uid: str) -> None:
    global _users_dirty
    if uid in USERS:
        USERS[uid]["blocked"] = True
        _users_dirty = True


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
        except RetryAfter as e:
            logger.warning("Rate limited, sleeping %ss", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
            try:
                msg = await bot.send_video(
                    chat_id=chat_id,
                    video=vid_id,
                    supports_streaming=True,
                    protect_content=True,
                )
                sent_video_message_ids.append(msg.message_id)
            except Exception as e2:
                logger.error("%s retry failed: %s", label, e2)
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
    track_user(user)
    logger.info("Join request from %s (%s)", user.id, user.full_name)

    try:
        await send_content(context.bot, user.id)
    except Forbidden:
        # user never pressed Start / blocked the bot
        mark_blocked(str(user.id))
        logger.info("Cannot DM %s (blocked / no start)", user.id)
    except Exception as e:
        logger.error("Failed to send content to %s: %s", user.id, e)


async def track_any_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Records every user who chats with the bot in private."""
    if update.effective_chat and update.effective_chat.type == "private":
        track_user(update.effective_user)


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user)

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
        "Broadcast:\n"
        "/broadcast  (reply to any message to send it to ALL users)\n"
        "/stats  (user counts)\n\n"
        "You can also send a video with caption:\n"
        "/setvideo1\n"
        "or\n"
        "/setvideo2"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    total = len(USERS)
    blocked = sum(1 for u in USERS.values() if u.get("blocked"))
    reachable = total - blocked

    await update.message.reply_text(
        "📊 *Bot Stats*\n\n"
        f"Total users tracked: *{total}*\n"
        f"Reachable: *{reachable}*\n"
        f"Blocked / unreachable: *{blocked}*\n\n"
        f"Store: `{USER_STORE_PATH}`",
        parse_mode="Markdown",
    )


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin replies to any message (text/photo/video/anything) with
    /broadcast — bot asks for confirmation, then copies it to all users."""
    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message or not update.message.reply_to_message:
        await update.message.reply_text(
            "Reply to the message you want to broadcast, then send /broadcast.\n\n"
            "Pwede text, photo, video, o kahit anong message — "
            "iko-copy ito sa lahat ng users."
        )
        return

    if context.bot_data.get("broadcast_running"):
        await update.message.reply_text("⚠️ May tumatakbong broadcast pa. Hintayin munang matapos.")
        return

    src = update.message.reply_to_message
    total = len(USERS)
    reachable = sum(1 for u in USERS.values() if not u.get("blocked"))

    # remember what to broadcast
    context.bot_data["broadcast_src"] = (src.chat_id, src.message_id)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ SEND NOW", callback_data="bc_confirm"),
            InlineKeyboardButton("❌ CANCEL", callback_data="bc_cancel"),
        ]
    ])

    await update.message.reply_text(
        "📢 *Broadcast Preview*\n\n"
        f"Target: *{reachable}* reachable users "
        f"(out of {total} tracked)\n\n"
        "Sigurado ka na ba?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if update.effective_user.id != ADMIN_ID:
        await query.answer("Not allowed.", show_alert=True)
        return

    await query.answer()

    if query.data == "bc_cancel":
        context.bot_data.pop("broadcast_src", None)
        await query.edit_message_text("❌ Broadcast cancelled.")
        return

    if query.data != "bc_confirm":
        return

    src = context.bot_data.pop("broadcast_src", None)
    if not src:
        await query.edit_message_text("⚠️ Walang naka-queue na broadcast. Ulitin ang /broadcast.")
        return

    if context.bot_data.get("broadcast_running"):
        await query.edit_message_text("⚠️ May tumatakbong broadcast pa.")
        return

    context.bot_data["broadcast_running"] = True
    await query.edit_message_text("🚀 Broadcasting... (magbibigay ako ng progress updates)")

    asyncio.create_task(
        run_broadcast(context, query.message.chat_id, src[0], src[1])
    )


async def run_broadcast(context, admin_chat_id: int, src_chat_id: int, src_message_id: int):
    global _users_dirty
    bot = context.bot

    targets = [uid for uid, u in USERS.items() if not u.get("blocked")]
    total = len(targets)
    sent = 0
    failed = 0
    started = time.time()

    progress_msg = None
    try:
        progress_msg = await bot.send_message(
            admin_chat_id, f"📤 0 / {total} sent..."
        )
    except Exception:
        pass

    for i, uid in enumerate(targets, start=1):
        try:
            await bot.copy_message(
                chat_id=int(uid),
                from_chat_id=src_chat_id,
                message_id=src_message_id,
            )
            sent += 1
        except RetryAfter as e:
            # Telegram told us to slow down — respect it, then retry once
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.copy_message(
                    chat_id=int(uid),
                    from_chat_id=src_chat_id,
                    message_id=src_message_id,
                )
                sent += 1
            except Exception:
                failed += 1
        except Forbidden:
            # user blocked the bot — mark them, skip forever after
            mark_blocked(uid)
            failed += 1
        except (TimedOut, NetworkError):
            await asyncio.sleep(2)
            failed += 1
        except Exception as e:
            logger.warning("Broadcast to %s failed: %s", uid, e)
            failed += 1

        await asyncio.sleep(BROADCAST_SLEEP)

        # progress update every 25 users
        if progress_msg and i % 25 == 0:
            try:
                await progress_msg.edit_text(f"📤 {i} / {total} processed...")
            except Exception:
                pass

    elapsed = int(time.time() - started)
    context.bot_data["broadcast_running"] = False
    _users_dirty = True  # persist any blocked flags

    summary = (
        "✅ *Broadcast finished!*\n\n"
        f"Sent: *{sent}*\n"
        f"Failed / blocked: *{failed}*\n"
        f"Time: *{elapsed}s*"
    )
    try:
        if progress_msg:
            await progress_msg.edit_text(summary, parse_mode="Markdown")
        else:
            await bot.send_message(admin_chat_id, summary, parse_mode="Markdown")
    except Exception:
        pass

    logger.info("Broadcast done: %d sent, %d failed, %ds", sent, failed, elapsed)


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


# ---------------------------------------------------------------------------
# Global error handler — hindi na babagsak ang bot sa unexpected errors
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception:", exc_info=context.error)


async def post_init(app):
    # start background flusher for users.json
    app.create_task(flush_users_loop())
    logger.info("User flush loop started. %d users loaded.", len(USERS))


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)          # process updates in parallel
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(30)
        .pool_timeout(10)
        .get_updates_read_timeout(40)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CallbackQueryHandler(broadcast_callback, pattern="^bc_"))
    app.add_handler(CommandHandler("showvideos", show_videos))
    app.add_handler(CommandHandler("testvideo", test_video))
    app.add_handler(CommandHandler("clearvideo1", clear_video1))
    app.add_handler(CommandHandler("clearvideo2", clear_video2))
    app.add_handler(CommandHandler("setvideo1", set_video1))
    app.add_handler(CommandHandler("setvideo2", set_video2))

    app.add_handler(MessageHandler(filters.VIDEO & filters.User(ADMIN_ID), admin_video_receiver))
    app.add_handler(ChatJoinRequestHandler(handle_join_request, chat_id=CHANNEL_ID))

    # catch-all tracker: kahit anong message sa private chat, ma-record ang user
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE, track_any_private_message),
        group=1,  # separate group para hindi ma-block ang ibang handlers
    )

    app.add_error_handler(error_handler)

    logger.info("Bot running...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
