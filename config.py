from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ["BOT_USERNAME"]
BOT_NAME = os.environ.get("BOT_NAME", "Gudu Gaming")
CHANNEL_URL = os.environ.get("CHANNEL_URL", "https://t.me/nexdropbot")
CHANNEL_BTN = os.environ.get("CHANNEL_BTN", f"📢 {BOT_NAME} Channel")
OWNER_ID = int(os.environ["OWNER_ID"])

DB_URI = os.environ["DB_URI"]
DB_NAME = os.environ.get("DB_NAME", "nexdrop")
DB_CHANNEL = int(os.environ["DB_CHANNEL"])

AUTO_DEL = int(os.environ.get("AUTO_DEL", 300))

START_PHOTO = os.environ.get("START_PHOTO", "")
FSUB_PHOTO = os.environ.get("FSUB_PHOTO", "")

DEFAULT_MESSAGES = {
    "START": (
        "☁️ <b>Welcome to Gudu Gaming!</b>\n\n"
        "📦 Get your files via secure links\n"
        "⚡ Fast & auto-cleaned delivery\n"
        "🔒 Protected access\n\n"
        "<i>Any issue? Contact • @subro98 / @gudugaming8069</i>"
    ),
    "FSUB": (
        "🔒 <b>Access Denied, {first}!</b>\n\n"
        "You must join our channel(s) to receive files.\n\n"
        "👇 Join below then tap <b>Try Again</b>."
    ),
    "FILE_CAPTION": (
        "📦 <b>{file_name}</b>\n\n"
        "⚠️ <b>Save this file now!</b> It will be auto-deleted in <b>{auto_del}</b>."
    ),
    "FILE_EXPIRED": (
        "⏰ <b>File deleted!</b>\n\n"
        "Click the link again to get a fresh copy."
    ),
    "NO_FILE": "❌ Invalid or expired link. Get a fresh one.",
    "BANNED": "🚫 You're banned from this bot.",
    "BROADCAST_DONE": "✅ Broadcast delivered to {count} users.",
}
