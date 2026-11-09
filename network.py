"""Multi-tenant Gudu Gaming custom file-share bots.

Each customer bot forwards uploaded Telegram messages into one private storage
channel. The customer bot must be an administrator of that channel. MongoDB
stores metadata only; file payloads stay in Telegram.
"""
import asyncio
import logging
import os
import re
import secrets
import string
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from config import BOT_TOKEN, DB_URI, DB_NAME

log = logging.getLogger(__name__)
client = AsyncIOMotorClient(DB_URI, tz_aware=True)
db = client[DB_NAME]
bots_col = db["network_bots"]
files_col = db["network_files"]
users_col = db["network_users"]
global_fsub_col = db["network_global_fsub"]

_raw_storage_id = int(os.environ.get("NETWORK_STORAGE_CHANNEL_ID", "3959825660"))
# Telegram channel IDs returned without the -100 prefix are normalized here.
STORAGE_CHANNEL_ID = int(f"-100{abs(_raw_storage_id)}") if _raw_storage_id > 0 else _raw_storage_id
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
# Hard-coded Fernet key. Keep this exact key to preserve access to encrypted bot tokens.
TOKEN_KEY = "vHnLKpEk6eC-NL5yi14tFLe4fGJL1Zw9PJYHB2NpqnA="
_main_bot = Bot(BOT_TOKEN)
_running = {}  # bot_id -> Application
_start_lock = asyncio.Lock()


def _cipher():
    try:
        return Fernet(TOKEN_KEY.encode())
    except Exception as exc:
        raise RuntimeError("BOT_TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc


def _encrypt(token):
    return _cipher().encrypt(token.encode()).decode()


def _decrypt(value):
    try:
        return _cipher().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Stored bot token cannot be decrypted; check BOT_TOKEN_ENCRYPTION_KEY") from exc


def _token():
    return "GGN_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(18))


def _fmt_size(n):
    if n >= 1024**3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024:.1f} KB"


def _fmt_time(n):
    if n >= 86400:
        return f"{n // 86400} day(s)"
    if n >= 3600:
        return f"{n // 3600} hour(s)"
    return f"{n // 60} minute(s)"


async def _stop_runtime(bot_id):
    app = _running.pop(int(bot_id), None)
    if not app:
        return
    try:
        if app.updater and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
    except Exception:
        log.error("Failed stopping customer bot %s", bot_id)


async def _start_runtime(doc):
    bot_id = int(doc["_id"])
    async with _start_lock:
        if bot_id in _running:
            return True
        token = _decrypt(doc["token_enc"])
        app = ApplicationBuilder().token(token).build()
        app.add_handler(CommandHandler("start", _bot_start))
        app.add_handler(CommandHandler("help", _bot_help))
        app.add_handler(CallbackQueryHandler(_bot_verify_callback, pattern=r"^netverify:"))
        app.add_handler(MessageHandler(
            filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO,
            _bot_upload,
        ))
        app.add_error_handler(_bot_error)
        await app.initialize()
        await app.start()
        try:
            await app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
        except Exception:
            await app.stop()
            await app.shutdown()
            raise
        _running[bot_id] = app
        await bots_col.update_one({"_id": bot_id}, {"$set": {"status": "running", "last_started_at": datetime.now(timezone.utc)}})
        return True


async def startup_network_bots():
    try:
        await _main_bot.initialize()
    except Exception:
        log.exception("Could not initialize main bot client for network membership checks")
    await bots_col.create_index("username", unique=True, sparse=True)
    await files_col.create_index([("bot_id", 1), ("owner_id", 1)])
    await users_col.create_index([("bot_id", 1), ("user_id", 1)], unique=True)
    async for doc in bots_col.find({"status": {"$ne": "stopped"}}):
        try:
            await _start_runtime(doc)
        except Exception:
            log.error("Could not start customer bot %s (%s)", doc.get("_id"), "startup error")
            await bots_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "error"}})


async def shutdown_network_bots():
    for bot_id in list(_running):
        await _stop_runtime(bot_id)
    try:
        await _main_bot.shutdown()
    except Exception:
        pass


async def _lookup_bot(context):
    return await bots_col.find_one({"_id": int(context.bot.id)})


async def _bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = await _lookup_bot(context)
    if not doc or doc.get("status") == "stopped":
        await update.effective_message.reply_text("This file-sharing bot is currently unavailable.")
        return
    user = update.effective_user
    await users_col.update_one(
        {"bot_id": int(context.bot.id), "user_id": user.id},
        {"$set": {"first_name": user.first_name, "username": user.username or "", "last_seen": datetime.now(timezone.utc)}},
        upsert=True,
    )
    if context.args:
        await _serve_file(update, context, context.args[0], doc)
        return
    await update.effective_message.reply_text(
        f"👋 Welcome to {doc.get('display_name') or context.bot.username}!\n\n"
        "Send a file to this bot to create a share link, or open a file link to receive it."
    )


async def _bot_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Send a document, video, audio or photo to create a share link. Open a /start link to receive a file.")


async def _all_required_channels(doc):
    own = await db["network_owner_fsub"].find({"bot_id": int(doc["_id"])}).to_list(None)
    global_channels = await global_fsub_col.find().to_list(None)
    # Preserve both layers; do not let owners edit global entries.
    return global_channels + own


async def _check_membership(user_id, doc):
    not_joined, errors = [], []
    for ch in await _all_required_channels(doc):
        try:
            member = await _main_bot.get_chat_member(chat_id=ch["_id"], user_id=user_id)
            status = str(member.status).lower()
            if status in ("left", "kicked") or (status == "restricted" and getattr(member, "is_member", False) is False):
                not_joined.append(ch)
        except Exception as exc:
            log.warning("Network FSub check failed for %s: %s", ch.get("_id"), exc)
            errors.append(ch)
    return not_joined, errors


async def _send_gate(update, doc, token, channels, error=False):
    rows = []
    for ch in channels:
        url = ch.get("invite_link")
        if url:
            rows.append([InlineKeyboardButton(f"📢 Join {ch.get('title', 'Channel')[:45]}", url=url)])
    rows.append([InlineKeyboardButton("✅ I've Joined — Verify", callback_data=f"netverify:{token}")])
    text = "⚠️ Membership verification is temporarily unavailable. Please try later." if error else "🔒 Join all required channels, then tap Verify Membership."
    await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def _serve_file(update, context, token, doc):
    user = update.effective_user
    f = await files_col.find_one({"_id": token, "bot_id": int(context.bot.id), "owner_id": doc["owner_id"]})
    if not f:
        await update.effective_message.reply_text("❌ This file link is invalid or has been removed.")
        return
    missing, errors = await _check_membership(user.id, doc)
    if errors:
        await _send_gate(update, doc, token, missing + errors, error=True)
        return
    if missing:
        await _send_gate(update, doc, token, missing)
        return
    try:
        sent = await context.bot.copy_message(
            chat_id=update.effective_chat.id,
            from_chat_id=STORAGE_CHANNEL_ID,
            message_id=f["storage_message_id"],
            caption=f"📦 {f.get('file_name', 'File')}\n\nSave this file now. It will be deleted from this chat in {_fmt_time(int(doc.get('auto_del', 300)))}.",
        )
        delay = max(30, int(doc.get("auto_del", 300)))
        async def cleanup():
            await asyncio.sleep(delay)
            try:
                await context.bot.delete_message(update.effective_chat.id, sent.message_id)
            except Exception:
                pass
        asyncio.create_task(cleanup())
        await users_col.update_one({"bot_id": int(context.bot.id), "user_id": user.id}, {"$inc": {"downloads": 1}}, upsert=True)
    except Exception as exc:
        log.error("Network file delivery failed (%s)", type(exc).__name__)
        await update.effective_message.reply_text("❌ Could not deliver this file. Please contact the bot owner.")


async def _bot_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = await _lookup_bot(context)
    if not doc or doc.get("status") != "running":
        return
    msg = update.effective_message
    user = update.effective_user
    if user.id != int(doc["owner_id"]):
        await msg.reply_text("Only this bot's owner can upload files. Share links can be opened by anyone.")
        return
    media = msg.document or msg.video or msg.audio or (msg.photo[-1] if msg.photo else None)
    if not media:
        return
    size = int(getattr(media, "file_size", 0) or 0)
    if size > MAX_FILE_BYTES:
        await msg.reply_text("❌ Maximum file size is 2 GB.")
        return
    fname = getattr(media, "file_name", None) or ("video.mp4" if msg.video else "audio.mp3" if msg.audio else "photo.jpg" if msg.photo else "file")
    try:
        # Server-side forwarding avoids downloading/re-uploading the payload.
        stored = await context.bot.forward_message(
            chat_id=STORAGE_CHANNEL_ID,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
        token = _token()
        await files_col.insert_one({
            "_id": token, "bot_id": int(context.bot.id), "owner_id": int(doc["owner_id"]),
            "storage_message_id": stored.message_id, "file_name": fname,
            "file_size": size, "media_type": "document" if msg.document else "video" if msg.video else "audio" if msg.audio else "photo",
            "created_at": datetime.now(timezone.utc), "uploader_id": user.id,
        })
        link = f"https://t.me/{context.bot.username}?start={token}"
        await msg.reply_text(
            f"✅ Upload saved to Gudu Gaming Network Storage\n\n📄 {fname}\n💾 {_fmt_size(size)}\n🔗 Share link:\n{link}"
        )
    except Exception:
        log.error("Could not forward custom bot upload to network storage channel")
        await msg.reply_text("❌ Upload failed. Please check that this bot is an administrator in the Gudu Gaming Network Storage channel with permission to post messages.")


async def _bot_error(update, context):
    log.error("Customer bot update failed (%s)", type(context.error).__name__)


async def handle_network_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message or not context.user_data.get("network_await_token"):
        return False
    uid = update.effective_user.id
    token = (update.message.text or "").strip()
    if re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,}", token):
        try:
            await update.message.delete()
        except Exception:
            pass
    if len(token) > 200 or not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,}", token):
        await update.message.reply_text("That doesn't look like a Telegram bot token. Send the token from @BotFather, or /cancel.")
        return True
    try:
        _cipher()  # Fail early if encryption is not configured.
        candidate = Bot(token)
        await candidate.initialize()
        me = await candidate.get_me()
        if not me.is_bot or not me.username:
            raise ValueError("Token does not identify a bot")
        # The bot itself must be able to post to and copy messages from storage.
        member = await candidate.get_chat_member(STORAGE_CHANNEL_ID, me.id)
        if str(member.status).lower() not in ("administrator", "creator"):
            raise ValueError("Add this bot as an administrator in the network storage channel")
        existing = await bots_col.find_one({"_id": me.id})
        if existing and int(existing.get("owner_id", 0)) != uid:
            await update.message.reply_text("❌ This bot is already registered to another Gudu Gaming account.")
            return True
        if existing:
            await update.message.reply_text("This bot is already connected to your account. Use My Bots to manage it or Replace Token.")
            return True
        # Atomic unique _id prevents the same Telegram bot being claimed twice.
        await bots_col.insert_one({
            "_id": me.id, "owner_id": uid, "username": me.username,
            "display_name": me.first_name, "token_enc": _encrypt(token),
            "status": "starting", "auto_del": 300, "created_at": datetime.now(timezone.utc),
        })
        doc = await bots_col.find_one({"_id": me.id})
        await _start_runtime(doc)
        context.user_data.pop("network_await_token", None)
        await update.message.reply_text(
            f"🎉 Your bot is connected!\n\n🤖 @{me.username}\n🟢 Status: Running\n\nOpen @{me.username} and send a file to test it."
        )
    except Exception as exc:
        log.warning("Custom bot registration failed for user %s (%s)", uid, type(exc).__name__)
        await update.message.reply_text(
            "❌ I couldn't connect this bot. Check that the token is valid, encryption is configured, "
            "and the bot is an administrator in the network storage channel. No token is shown in this error."
        )
    finally:
        if 'candidate' in locals():
            try:
                await candidate.shutdown()
            except Exception:
                pass
    return True


async def network_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    uid = q.from_user.id
    data = q.data
    if data == "network_create":
        context.user_data["network_await_token"] = True
        await q.message.reply_text("🤖 Create your bot with @BotFather, add it as an administrator to the Gudu Gaming Network Storage channel, then send its token here. Never share the token with anyone else. Send /cancel to stop.")
    elif data == "network_mybots":
        docs = await bots_col.find({"owner_id": uid}).to_list(None)
        if not docs:
            await q.message.reply_text("You haven't connected a custom bot yet.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Create a Bot", callback_data="network_create")]]))
            return
        rows = [[InlineKeyboardButton(f"@{b['username']} · {b.get('status','unknown')}", callback_data=f"network_manage_{b['_id']}")] for b in docs]
        rows.append([InlineKeyboardButton("➕ Add another bot", callback_data="network_create")])
        await q.message.reply_text("🤖 <b>Your File Share Bots</b>\nSelect a bot to manage:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("network_manage_"):
        bot_id = int(data.rsplit("_", 1)[1])
        b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b:
            await q.message.reply_text("Bot not found in your account.")
            return
        users = await users_col.count_documents({"bot_id": bot_id})
        files = await files_col.count_documents({"bot_id": bot_id, "owner_id": uid})
        rows = [
            [InlineKeyboardButton("📊 Statistics", callback_data=f"network_stats_{bot_id}")],
            [InlineKeyboardButton("🔒 Add Force Subscribe", callback_data=f"network_addfsub_{bot_id}"), InlineKeyboardButton("📋 My Channels", callback_data=f"network_fsub_{bot_id}")],
            [InlineKeyboardButton("⏳ Auto Delete", callback_data=f"network_autodel_{bot_id}")],
            [InlineKeyboardButton("🔄 Restart", callback_data=f"network_restart_{bot_id}"), InlineKeyboardButton("⏸ Stop", callback_data=f"network_stop_{bot_id}")],
            [InlineKeyboardButton("🔐 Replace Token", callback_data=f"network_replace_{bot_id}")],
            [InlineKeyboardButton("⬅️ My Bots", callback_data="network_mybots")],
        ]
        await q.message.reply_text(f"🤖 <b>@{b['username']}</b>\nStatus: <b>{b.get('status','unknown')}</b>\n\n👥 Users: {users}\n📦 Files: {files}\n⏳ Auto-delete: {_fmt_time(int(b.get('auto_del',300)))}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("network_stats_"):
        bot_id = int(data.rsplit("_", 1)[1]); b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b: return
        users = await users_col.count_documents({"bot_id": bot_id}); files = await files_col.count_documents({"bot_id": bot_id, "owner_id": uid})
        downloads = await users_col.aggregate([{"$match":{"bot_id":bot_id}},{"$group":{"_id":None,"n":{"$sum":"$downloads"}}}]).to_list(1)
        await q.message.reply_text(f"📊 @{b['username']}\n👥 Users: {users}\n📦 Files: {files}\n📥 Downloads: {downloads[0]['n'] if downloads else 0}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"network_manage_{bot_id}")]]))
    elif data.startswith("network_addfsub_"):
        bot_id = int(data.rsplit("_",1)[1]); b = await bots_col.find_one({"_id":bot_id,"owner_id":uid})
        if not b: return
        context.user_data["network_action"] = ("add_fsub", bot_id)
        await q.message.reply_text("Send the channel ID (for example -1001234567890). The main Gudu Gaming bot must be an administrator in that channel so it can verify membership.")
    elif data.startswith("network_fsub_"):
        bot_id = int(data.rsplit("_",1)[1]); b = await bots_col.find_one({"_id":bot_id,"owner_id":uid})
        if not b: return
        chs = await db["network_owner_fsub"].find({"bot_id":bot_id}).to_list(None)
        rows = [[InlineKeyboardButton(f"❌ Remove {c.get('title','channel')[:25]}", callback_data=f"network_delfsub_{bot_id}_{c['_id']}")] for c in chs]
        rows += [[InlineKeyboardButton("⬅️ Back", callback_data=f"network_manage_{bot_id}")]]
        await q.message.reply_text("Your bot's Force Subscribe channels:\n" + ("\n".join(f"• {c.get('title')} ({c['_id']})" for c in chs) if chs else "No owner channels added."), reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("network_delfsub_"):
        parts = data.split("_"); bot_id=int(parts[2]); ch_id=int(parts[3]); await db["network_owner_fsub"].delete_one({"bot_id":bot_id,"_id":ch_id,"owner_id":uid})
        await q.message.reply_text("Channel removed from your bot.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"network_manage_{bot_id}")]]))
    elif data.startswith("network_autodel_"):
        bot_id=int(data.rsplit("_",1)[1]); b=await bots_col.find_one({"_id":bot_id,"owner_id":uid})
        if not b: return
        rows=[[InlineKeyboardButton("5 minutes",callback_data=f"network_setdel_{bot_id}_300"),InlineKeyboardButton("10 minutes",callback_data=f"network_setdel_{bot_id}_600")],[InlineKeyboardButton("1 hour",callback_data=f"network_setdel_{bot_id}_3600"),InlineKeyboardButton("24 hours",callback_data=f"network_setdel_{bot_id}_86400")],[InlineKeyboardButton("⬅️ Back",callback_data=f"network_manage_{bot_id}")]]
        await q.message.reply_text(f"⏳ Current auto-delete: {_fmt_time(int(b.get('auto_del',300)))}",reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("network_setdel_"):
        _,_,bot_id,seconds=data.split("_"); bot_id=int(bot_id); seconds=int(seconds)
        await bots_col.update_one({"_id":bot_id,"owner_id":uid},{"$set":{"auto_del":seconds}})
        await q.message.reply_text("Auto-delete updated.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back",callback_data=f"network_manage_{bot_id}")]]))
    elif data.startswith("network_restart_"):
        bot_id=int(data.rsplit("_",1)[1]); b=await bots_col.find_one({"_id":bot_id,"owner_id":uid})
        if not b: return
        try:
            await _stop_runtime(bot_id); b["status"]="starting"; await _start_runtime(b)
            await q.message.reply_text("✅ Bot restarted.")
        except Exception:
            await q.message.reply_text("❌ Restart failed. Check the bot token and server logs.")
    elif data.startswith("network_stop_"):
        bot_id=int(data.rsplit("_",1)[1]); b=await bots_col.find_one({"_id":bot_id,"owner_id":uid})
        if not b: return
        await _stop_runtime(bot_id); await bots_col.update_one({"_id":bot_id},{"$set":{"status":"stopped"}})
        await q.message.reply_text("⏸ Your bot is stopped. You can restart it from this panel.")
    elif data.startswith("network_replace_"):
        bot_id=int(data.rsplit("_",1)[1]); b=await bots_col.find_one({"_id":bot_id,"owner_id":uid})
        if not b: return
        context.user_data["network_action"]=("replace_token",bot_id)
        await q.message.reply_text("Send the new token generated by @BotFather for this SAME bot. Its Telegram bot ID must remain unchanged.")


async def network_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return False
    action = context.user_data.get("network_action")
    if not action:
        return await handle_network_token(update, context)
    kind, bot_id = action
    uid = update.effective_user.id
    if kind == "add_fsub":
        raw = (update.message.text or "").strip()
        try:
            ch_id = int(raw)
            chat = await _main_bot.get_chat(ch_id)
            member = await _main_bot.get_chat_member(ch_id, _main_bot.id)
            if str(member.status).lower() not in ("administrator", "creator"):
                raise ValueError("Main bot must be admin in that channel")
            invite = f"https://t.me/{chat.username}" if chat.username else None
            if not invite:
                link = await _main_bot.create_chat_invite_link(ch_id, name="Gudu Gaming Network FSub")
                invite = link.invite_link
            await db["network_owner_fsub"].update_one({"bot_id":bot_id,"_id":ch_id},{"$set":{"owner_id":uid,"title":chat.title,"invite_link":invite}},upsert=True)
            await update.message.reply_text(f"✅ Added {chat.title} to this bot's Force Subscribe list.")
        except Exception:
            await update.message.reply_text("❌ Couldn't add that channel. Send a valid channel ID and ensure the main Gudu Gaming bot is an administrator there.")
        context.user_data.pop("network_action",None)
        return True
    if kind == "replace_token":
        token=(update.message.text or "").strip()
        if re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,}", token):
            try: await update.message.delete()
            except Exception: pass
        try:
            candidate=Bot(token); await candidate.initialize(); me=await candidate.get_me()
            if me.id != int(bot_id):
                raise ValueError("Token is for a different bot")
            member=await candidate.get_chat_member(STORAGE_CHANNEL_ID,me.id)
            if str(member.status).lower() not in ("administrator","creator"):
                raise ValueError("Bot is not admin in storage")
            await bots_col.update_one({"_id":bot_id,"owner_id":uid},{"$set":{"token_enc":_encrypt(token),"username":me.username,"display_name":me.first_name,"status":"starting"}})
            await _stop_runtime(bot_id)
            doc=await bots_col.find_one({"_id":bot_id}); await _start_runtime(doc)
            await update.message.reply_text("✅ Token replaced and bot restarted. Delete the old token from any place where it was exposed.")
        except Exception:
            await update.message.reply_text("❌ Token replacement failed. Use a valid new token for the same bot and keep it admin in the storage channel.")
        finally:
            if 'candidate' in locals():
                try: await candidate.shutdown()
                except Exception: pass
            context.user_data.pop("network_action",None)
        return True
    return False


async def network_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("network_await_token",None)
    context.user_data.pop("network_action",None)
    await update.effective_message.reply_text("Cancelled.")


async def _bot_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    token=q.data.split(":",1)[1]; doc=await _lookup_bot(context)
    if not doc: return
    missing,errors=await _check_membership(q.from_user.id,doc)
    if errors:
        await q.answer("Membership check unavailable. Contact the bot owner.",show_alert=True); return
    if missing:
        await q.answer("Join every required channel first.",show_alert=True); return
    await q.answer("Verified. Sending file…")
    await _serve_file(update,context,token,doc)


async def register_network_handlers(app):
    app.add_handler(CallbackQueryHandler(network_callback, pattern=r"^network_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, network_text_message), group=0)
    app.add_handler(CommandHandler("cancel", network_cancel))


async def cmd_network_fsub_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from config import OWNER_ID
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /networkfsubadd CHANNEL_ID")
        return
    try:
        ch_id = int(context.args[0])
        chat = await context.bot.get_chat(ch_id)
        member = await context.bot.get_chat_member(ch_id, context.bot.id)
        if str(member.status).lower() not in ("administrator", "creator"):
            raise ValueError("Main bot must be admin")
        invite = f"https://t.me/{chat.username}" if chat.username else (await context.bot.create_chat_invite_link(ch_id, name="Gudu Gaming mandatory channel")).invite_link
        await global_fsub_col.update_one({"_id": ch_id}, {"$set": {"title": chat.title, "invite_link": invite}}, upsert=True)
        await update.effective_message.reply_text(f"✅ Added mandatory network channel: {chat.title}")
    except Exception:
        await update.effective_message.reply_text("❌ Could not add channel. Ensure the main Gudu Gaming bot is an administrator and use a valid channel ID.")


async def cmd_network_fsub_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from config import OWNER_ID
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /networkfsubremove CHANNEL_ID")
        return
    try:
        ch_id = int(context.args[0])
        result = await global_fsub_col.delete_one({"_id": ch_id})
        await update.effective_message.reply_text("✅ Mandatory network channel removed." if result.deleted_count else "Channel not found.")
    except ValueError:
        await update.effective_message.reply_text("Invalid channel ID.")


async def cmd_network_fsub_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from config import OWNER_ID
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    channels = await global_fsub_col.find().to_list(None)
    if not channels:
        await update.effective_message.reply_text("No Gudu Gaming mandatory network channels configured.")
        return
    text = "📢 Gudu Gaming mandatory network channels:\n\n" + "\n".join(f"• {c.get('title')} — <code>{c['_id']}</code>" for c in channels)
    await update.effective_message.reply_text(text, parse_mode="HTML")
