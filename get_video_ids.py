"""
Run this script ONCE to get the Telegram file_id of your videos.
Send a video to your bot while this is running, and it prints the file_id.
Copy the file_id into Railway env vars (VIDEO_1_ID, VIDEO_2_ID, etc.)
"""
import os
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
logging.basicConfig(level=logging.INFO)


async def get_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.video:
        fid = update.message.video.file_id
        print(f"\n✅ VIDEO FILE ID:\n{fid}\n")
        await update.message.reply_text(f"file_id:\n`{fid}`", parse_mode="Markdown")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.VIDEO, get_file_id))
    print("Send a video to your bot now...")
    app.run_polling()


if __name__ == "__main__":
    main()
