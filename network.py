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
verification_requests_col = db["network_bot_verification_requests"]
network_settings_col = db["network_settings"]
bot_managers_col = db["network_bot_managers"]
bot_bans_col = db["network_bot_bans"]
owner_fsub_col = db["network_owner_fsub"]

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



async def _validate_customer_bot_access(candidate, bot_id):
    """Verify the customer bot can access the configured destination."""
    try:
        chat = await candidate.get_chat(STORAGE_CHANNEL_ID)
    except TelegramError as exc:
        raise ValueError(
            "The bot cannot access the configured destination. Confirm the destination ID and add the bot to it."
        ) from exc
    try:
        member = await candidate.get_chat_member(chat.id, bot_id)
    except TelegramError as exc:
        raise ValueError(
            "Telegram could not verify the bot's administrator membership in the configured destination."
        ) from exc
    status = str(member.status).lower()
    if status not in ("administrator", "creator"):
        raise ValueError("The bot must be an administrator in the configured destination.")
    # Channel admins need the post permission for forwarded files to be accepted.
    if status == "administrator" and getattr(member, "can_post_messages", True) is False:
        raise ValueError("The bot administrator role is missing permission to post messages.")
    return chat, member

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
        app.add_handler(CommandHandler("myaccount", cmd_bot_myaccount))
        app.add_handler(CommandHandler("addmanager", cmd_bot_addmanager))
        app.add_handler(CommandHandler("removemanager", cmd_bot_removemanager))
        app.add_handler(CommandHandler("listmanagers", cmd_bot_listmanagers))
        app.add_handler(CommandHandler("ban", cmd_bot_ban))
        app.add_handler(CommandHandler("unban", cmd_bot_unban))
        app.add_handler(CommandHandler("addfjoin", cmd_bot_addfjoin))
        app.add_handler(CommandHandler("listfjoin", cmd_bot_listfjoin))
        app.add_handler(CommandHandler("removefjoin", cmd_bot_removefjoin))
        app.add_handler(CommandHandler("setwelcome", cmd_bot_setwelcome))
        app.add_handler(CommandHandler("setchannel", cmd_bot_setchannel))
        app.add_handler(CommandHandler("broadcast", cmd_bot_broadcast))
        app.add_handler(CallbackQueryHandler(_bot_verify_callback, pattern=r"^netverify:"))
        app.add_handler(CallbackQueryHandler(_bot_account_callback, pattern=r"^netaccount$"))
        app.add_handler(MessageHandler(
            filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO,
            _bot_upload,
        ))
        app.add_error_handler(_bot_error)
        # Follow PTB's documented manual lifecycle: initialize -> start polling -> start.
        # Starting the updater first also avoids a partially-started Application if
        # Telegram rejects polling because another process owns this bot's updates.
        updater_started = False
        app_started = False
        try:
            await app.initialize()
            try:
                from telegram import BotCommand
                await app.bot.set_my_commands([
                    BotCommand("start", "Open bot / receive a shared file"),
                    BotCommand("help", "Show bot help and owner commands"),
                    BotCommand("myaccount", "View your network account statistics"),
                    BotCommand("addmanager", "Add a manager (owner only)"),
                    BotCommand("removemanager", "Remove a manager (owner only)"),
                    BotCommand("listmanagers", "List managers (owner only)"),
                    BotCommand("addfjoin", "Add a force-join channel (owner only)"),
                    BotCommand("listfjoin", "List force-join channels (owner only)"),
                    BotCommand("removefjoin", "Remove your force-join channel (owner only)"),
                    BotCommand("setwelcome", "Set welcome text (owner only)"),
                    BotCommand("setchannel", "Set your channel button (owner only)"),
                    BotCommand("broadcast", "Broadcast to users (owner only)"),
                    BotCommand("ban", "Ban a user (owner only)"),
                    BotCommand("unban", "Unban a user (owner only)"),
                ])
            except Exception:
                log.warning("Could not set command menu for customer bot %s", bot_id)
            if app.updater is None:
                raise RuntimeError("Polling updater is unavailable")
            await app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )
            updater_started = True
            await app.start()
            app_started = True
        except Exception:
            if updater_started and app.updater and app.updater.running:
                try:
                    await app.updater.stop()
                except Exception:
                    log.exception("Failed to stop updater after startup failure for bot %s", bot_id)
            if app_started and app.running:
                try:
                    await app.stop()
                except Exception:
                    log.exception("Failed to stop application after startup failure for bot %s", bot_id)
            try:
                await app.shutdown()
            except Exception:
                log.exception("Failed to shut down application after startup failure for bot %s", bot_id)
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
    async for doc in bots_col.find({"status": {"$nin": ["stopped", "stopped_by_admin", "pending_verification", "verification_failed"]}}):
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
    if not doc or doc.get("status") in ("stopped", "stopped_by_admin"):
        await update.effective_message.reply_text("This file-sharing bot is currently unavailable.")
        return
    user = update.effective_user
    if await bot_bans_col.find_one({"bot_id": int(context.bot.id), "user_id": user.id}):
        await update.effective_message.reply_text("🚫 You are blocked from using this bot.")
        return
    await users_col.update_one(
        {"bot_id": int(context.bot.id), "user_id": user.id},
        {"$set": {"first_name": user.first_name, "username": user.username or "", "last_seen": datetime.now(timezone.utc)}},
        upsert=True,
    )
    if context.args:
        await _serve_file(update, context, context.args[0], doc)
        return
    welcome = doc.get("welcome_text") or (
        f"👋 Welcome to {doc.get('display_name') or context.bot.username}!\n\n"
        "Open a file link to receive a file."
    )
    rows = []
    if doc.get("channel_url"):
        rows.append([InlineKeyboardButton(doc.get("channel_button") or "📢 My Channel", url=doc["channel_url"])])
    if user.id == int(doc["owner_id"]):
        rows.append([InlineKeyboardButton("👤 My Account", callback_data="netaccount")])
    await update.effective_message.reply_text(welcome, reply_markup=InlineKeyboardMarkup(rows) if rows else None)


async def _bot_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = await _lookup_bot(context)
    owner = bool(doc and update.effective_user and update.effective_user.id == int(doc["owner_id"]))
    text = "Open a share link to receive a file. Only the bot owner and approved managers can upload files."
    if owner:
        text += ("\n\nOwner commands:\n/addmanager USER_ID · /removemanager USER_ID · /listmanagers\n"
                 "/ban USER_ID · /unban USER_ID\n/addfjoin CHANNEL_ID · /listfjoin · /removefjoin CHANNEL_ID\n"
                 "/setwelcome TEXT · /setchannel URL [Button text] · /broadcast TEXT · /myaccount")
    await update.effective_message.reply_text(text)


async def _all_required_channels(doc):
    own = await owner_fsub_col.find({"bot_id": int(doc["_id"])}).to_list(None)
    global_channels = await global_fsub_col.find().to_list(None)
    # Global channels are platform-mandated and always apply, even when owners add none.
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
    if await bot_bans_col.find_one({"bot_id": int(context.bot.id), "user_id": user.id}):
        await update.effective_message.reply_text("🚫 You are blocked from using this bot.")
        return
    await users_col.update_one({"bot_id": int(context.bot.id), "user_id": user.id}, {"$set": {"first_name": user.first_name, "username": user.username or "", "last_seen": datetime.now(timezone.utc)}}, upsert=True)
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
    if await bot_bans_col.find_one({"bot_id": int(context.bot.id), "user_id": user.id}):
        await msg.reply_text("🚫 You are blocked from using this bot.")
        return
    is_manager = await bot_managers_col.find_one({"bot_id": int(context.bot.id), "user_id": user.id})
    if user.id != int(doc["owner_id"]) and not is_manager:
        await msg.reply_text("Only this bot's owner and approved managers can upload files. Other users can only receive shared files.")
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
            f"✅ Your file is ready!\n\n📄 {fname}\n💾 {_fmt_size(size)}\n🔗 Share link:\n{link}"
        )
    except Exception:
        log.error("Could not forward custom bot upload to network storage channel")
        await msg.reply_text("❌ Upload failed. Please try again later or contact support.")



async def _is_bot_owner_or_manager(update, context, owner_only=False):
    doc = await _lookup_bot(context)
    if not doc or not update.effective_user:
        return None
    uid = update.effective_user.id
    if uid == int(doc["owner_id"]):
        return doc
    if not owner_only and await bot_managers_col.find_one({"bot_id": int(context.bot.id), "user_id": uid}):
        return doc
    return None

async def cmd_bot_addmanager(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=True)
    if not doc: return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /addmanager USER_ID")
        return
    uid = int(context.args[0])
    if uid == int(doc["owner_id"]):
        await update.effective_message.reply_text("The owner already has full access.")
        return
    await bot_managers_col.update_one({"bot_id": int(context.bot.id), "user_id": uid}, {"$set": {"bot_id": int(context.bot.id), "user_id": uid, "added_by": update.effective_user.id, "added_at": datetime.now(timezone.utc)}}, upsert=True)
    await update.effective_message.reply_text(f"✅ User {uid} is now a manager of @{context.bot.username}.")

async def cmd_bot_removemanager(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=True)
    if not doc: return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /removemanager USER_ID"); return
    result = await bot_managers_col.delete_one({"bot_id": int(context.bot.id), "user_id": int(context.args[0])})
    await update.effective_message.reply_text("✅ Manager removed." if result.deleted_count else "That user is not a manager.")

async def cmd_bot_listmanagers(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=True)
    if not doc: return
    items = await bot_managers_col.find({"bot_id": int(context.bot.id)}).to_list(None)
    await update.effective_message.reply_text("👥 Managers:\n" + ("\n".join(f"• {x['user_id']}" for x in items) if items else "No managers yet."))

async def cmd_bot_ban(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=True)
    if not doc: return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /ban USER_ID"); return
    uid = int(context.args[0])
    if uid == int(doc["owner_id"]):
        await update.effective_message.reply_text("You cannot ban the bot owner."); return
    await bot_bans_col.update_one({"bot_id": int(context.bot.id), "user_id": uid}, {"$set": {"bot_id": int(context.bot.id), "user_id": uid, "banned_at": datetime.now(timezone.utc)}}, upsert=True)
    await update.effective_message.reply_text(f"🚫 User {uid} banned from this bot.")

async def cmd_bot_unban(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=True)
    if not doc: return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /unban USER_ID"); return
    result = await bot_bans_col.delete_one({"bot_id": int(context.bot.id), "user_id": int(context.args[0])})
    await update.effective_message.reply_text("✅ User unbanned." if result.deleted_count else "User was not banned.")

async def cmd_bot_addfjoin(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=True)
    if not doc: return
    if not context.args or not context.args[0].lstrip('-').isdigit():
        await update.effective_message.reply_text("Usage: /addfjoin CHANNEL_ID"); return
    try:
        ch_id = int(context.args[0]); chat = await _main_bot.get_chat(ch_id)
        member = await _main_bot.get_chat_member(ch_id, _main_bot.id)
        if str(member.status).lower() not in ("administrator", "creator"):
            raise ValueError("Main bot is not admin")
        invite = f"https://t.me/{chat.username}" if chat.username else (await _main_bot.create_chat_invite_link(ch_id, name="Bot Force Join")).invite_link
        await owner_fsub_col.update_one({"bot_id": int(context.bot.id), "_id": ch_id}, {"$set": {"bot_id": int(context.bot.id), "owner_id": int(doc["owner_id"]), "title": chat.title, "invite_link": invite}}, upsert=True)
        await update.effective_message.reply_text(f"✅ Added {chat.title} to this bot's Force Join list.")
    except Exception:
        await update.effective_message.reply_text("❌ Cannot add channel. Make sure the main Gudu Gaming bot is an administrator there and send a valid channel ID.")

async def cmd_bot_listfjoin(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=True)
    if not doc: return
    own = await owner_fsub_col.find({"bot_id": int(context.bot.id)}).to_list(None)
    platform = await global_fsub_col.find().to_list(None)
    lines = ["📌 <b>Platform-required channels (set by Gudu admin)</b>"]
    lines += [f"• {c.get('title','Channel')} — <code>{c['_id']}</code>" for c in platform] or ["• None configured"]
    lines += ["\n📌 <b>Your channels</b>"]
    lines += [f"• {c.get('title','Channel')} — <code>{c['_id']}</code>" for c in own] or ["• No channels added"]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_bot_removefjoin(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=True)
    if not doc: return
    if not context.args or not context.args[0].lstrip('-').isdigit():
        await update.effective_message.reply_text("Usage: /removefjoin CHANNEL_ID"); return
    result = await owner_fsub_col.delete_one({"bot_id": int(context.bot.id), "_id": int(context.args[0]), "owner_id": int(doc["owner_id"])})
    await update.effective_message.reply_text("✅ Your Force Join channel was removed." if result.deleted_count else "Channel not found in your own list. Platform channels cannot be removed here.")

async def cmd_bot_setwelcome(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=True)
    if not doc: return
    value = " ".join(context.args).strip()
    if not value:
        await update.effective_message.reply_text("Usage: /setwelcome Your welcome message"); return
    if len(value) > 3500:
        await update.effective_message.reply_text("Welcome text must be under 3500 characters."); return
    await bots_col.update_one({"_id": int(context.bot.id), "owner_id": int(doc["owner_id"])}, {"$set": {"welcome_text": value}})
    await update.effective_message.reply_text("✅ Welcome message updated.")

async def cmd_bot_setchannel(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=True)
    if not doc: return
    if len(context.args) < 1 or not context.args[0].startswith(("https://t.me/", "http://t.me/")):
        await update.effective_message.reply_text("Usage: /setchannel https://t.me/yourchannel [Button text]"); return
    url = context.args[0]
    if not re.match(r"^https?://t\.me/(?:[A-Za-z0-9_]{5,}|\+[^\s]+)$", url):
        await update.effective_message.reply_text("Please use a valid Telegram channel link."); return
    label = " ".join(context.args[1:])[:50] or "📢 My Channel"
    await bots_col.update_one({"_id": int(context.bot.id)}, {"$set": {"channel_url": url, "channel_button": label}})
    await update.effective_message.reply_text("✅ Channel button saved. It will appear on /start.")

async def cmd_bot_broadcast(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=True)
    if not doc: return
    source = update.effective_message.reply_to_message
    body = " ".join(context.args).strip()
    if not source and not body:
        await update.effective_message.reply_text("Usage: /broadcast message (or reply to a media/message with /broadcast)"); return
    targets = await users_col.find({"bot_id": int(context.bot.id)}, {"user_id": 1}).to_list(None)
    sent = failed = 0
    status = await update.effective_message.reply_text(f"📡 Broadcasting to {len(targets)} users...")
    for target in targets:
        try:
            if source:
                await context.bot.copy_message(target["user_id"], source.chat_id, source.message_id)
            else:
                await context.bot.send_message(target["user_id"], body)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)
    await status.edit_text(f"📡 Broadcast complete\n✅ Sent: {sent}\n❌ Failed: {failed}")

async def cmd_bot_myaccount(update, context):
    doc = await _lookup_bot(context)
    if not doc or update.effective_user.id != int(doc["owner_id"]):
        return
    owned = await bots_col.find({"owner_id": int(doc["owner_id"])}).to_list(None)
    bot_ids = [int(x["_id"]) for x in owned]
    users = await users_col.count_documents({"bot_id": {"$in": bot_ids}}) if bot_ids else 0
    files = await files_col.count_documents({"bot_id": {"$in": bot_ids}, "owner_id": int(doc["owner_id"])}) if bot_ids else 0
    managers = await bot_managers_col.count_documents({"bot_id": {"$in": bot_ids}}) if bot_ids else 0
    downloads = await users_col.aggregate([{"$match": {"bot_id": {"$in": bot_ids}}}, {"$group": {"_id": None, "n": {"$sum": "$downloads"}}}]).to_list(1) if bot_ids else []
    await update.effective_message.reply_text(
        f"👤 <b>My Account — @{context.bot.username}</b>\n\n"
        f"📦 Files: <b>{files}</b>\n👥 Users: <b>{users}</b>\n📥 Downloads: <b>{downloads[0]['n'] if downloads else 0}</b>\n"
        f"🤖 Bots: <b>{len(bot_ids)}</b>\n🛠 Managers: <b>{managers}</b>", parse_mode="HTML")

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
        # Step 3 is deliberately deferred: the user first submits the bot username
        # to this bot. A configured verifier can then collect it from the queue and
        # manually add the customer bot to the storage channel.
        existing = await bots_col.find_one({"_id": me.id})
        if existing and int(existing.get("owner_id", 0)) != uid:
            await update.message.reply_text("❌ This bot is already registered to another Gudu Gaming account.")
            return True
        if existing and existing.get("status") not in ("pending_verification", "verification_failed"):
            await update.message.reply_text("This bot is already connected to your account. Use My Bots to manage it or Replace Token.")
            return True
        await bots_col.update_one(
            {"_id": me.id},
            {"$set": {
                "owner_id": uid, "username": me.username,
                "display_name": me.first_name, "token_enc": _encrypt(token),
                "status": "pending_verification", "auto_del": 300,
                "created_at": datetime.now(timezone.utc),
            }}, upsert=True,
        )
        context.user_data.pop("network_await_token", None)
        context.user_data["network_pending_submit_bot"] = me.id
        await update.message.reply_text(
            f"✅ Token verified!\n\n🤖 Bot: @{me.username}\n\n"
            "Next, submit this bot username to the Gudu Gaming verification queue. "
            "A verifier will collect it here and manually add your bot to the storage channel. "
            "No personal message to a verifier is needed.\n\n"
            "Tap the button, then send the bot username exactly as shown.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📩 Submit bot username", callback_data=f"network_submitbot_{me.id}")]])
        )
    except Exception as exc:
        # Never log the submitted token. Log a sanitized diagnostic for the operator.
        safe_error = re.sub(r"\d{5,15}:[A-Za-z0-9_-]{20,}", "[REDACTED_TOKEN]", str(exc))
        log.error(
            "Custom bot registration failed for user %s; error_type=%s; detail=%s",
            uid, type(exc).__name__, safe_error[:500],
        )
        # Give a useful, safe reason without exposing the submitted token or internals.
        if isinstance(exc, ValueError):
            public_reason = "The bot's required access could not be verified."
        elif isinstance(exc, TelegramError):
            public_reason = "Telegram could not complete the setup check."
        else:
            public_reason = "The service could not start the bot after verification."
        await update.message.reply_text(
            "❌ I couldn't connect your bot. Your token was verified, but setup could not be completed.\n\n"
            f"Reason: {public_reason}\n\n"
            "Please confirm the bot has the required administrator permissions, then try again. "
            "Your token is never shown here."
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
    if data.startswith("network_submitbot_"):
        try:
            bot_id = int(data.rsplit("_", 1)[1])
        except ValueError:
            return
        pending = await bots_col.find_one({"_id": bot_id, "owner_id": uid, "status": "pending_verification"})
        if not pending:
            return
        context.user_data["network_await_submit_username"] = bot_id
        await q.message.reply_text(f"Send the username of your bot now: @{pending['username']}\n\nSend it as @username. Send /cancel to stop.")
    elif data.startswith("network_continueverify_"):
        try:
            bot_id = int(data.rsplit("_", 1)[1])
        except ValueError:
            return
        doc = await bots_col.find_one({"_id": bot_id, "owner_id": uid, "status": "pending_verification"})
        if not doc or not await verification_requests_col.find_one({"_id": bot_id, "owner_id": uid, "submitted": True}):
            return
        try:
            candidate = Bot(_decrypt(doc["token_enc"]))
            await candidate.initialize()
            me = await candidate.get_me()
            await _validate_customer_bot_access(candidate, me.id)
            await bots_col.update_one({"_id": bot_id, "owner_id": uid}, {"$set": {"status": "starting", "verified_at": datetime.now(timezone.utc)}})
            await _start_runtime(await bots_col.find_one({"_id": bot_id}))
            await verification_requests_col.update_one({"_id": bot_id}, {"$set": {"status": "completed", "completed_at": datetime.now(timezone.utc)}})
            await q.message.reply_text(f"🎉 Your bot is connected!\n\n🤖 @{doc['username']}\n🟢 Status: Running\n\nOpen @{doc['username']} and send a file to test it.")
        except Exception:
            await q.message.reply_text("❌ Verification failed. Ask your verifier to add the bot as an administrator with permission to post messages, then try again.")
        finally:
            if 'candidate' in locals():
                try: await candidate.shutdown()
                except Exception: pass
    elif data == "network_create":
        context.user_data["network_await_token"] = True
        await q.message.reply_text(
            "🤖 Connect Your Telegram Bot\n\n"
            "Step 1: Open @BotFather.\n"
            "Step 2: Send /newbot and follow the instructions.\n"
            "Step 3: Choose a name and username for your bot.\n"
            "Step 4: Copy the API token provided by BotFather.\n"
            "Step 5: Send the token here to complete setup.\n\n"
            "🔒 Never share your bot token with anyone else.\n"
            "❌ Send /cancel to stop."
        )
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
            [InlineKeyboardButton("👥 Managers", callback_data=f"network_managers_{bot_id}"), InlineKeyboardButton("🚫 Ban / Unban", callback_data=f"network_banmenu_{bot_id}")],
            [InlineKeyboardButton("✏️ Welcome Text", callback_data=f"network_welcome_{bot_id}"), InlineKeyboardButton("📢 My Channel", callback_data=f"network_channel_{bot_id}")],
            [InlineKeyboardButton("📡 Broadcast", callback_data=f"network_broadcast_{bot_id}")],
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
    elif data.startswith("network_managers_"):
        bot_id = int(data.rsplit("_", 1)[1]); b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b: return
        managers = await bot_managers_col.find({"bot_id": bot_id}).to_list(None)
        rows = [[InlineKeyboardButton(f"❌ Remove {m['user_id']}", callback_data=f"network_delmanager_{bot_id}_{m['user_id']}")] for m in managers]
        rows += [[InlineKeyboardButton("➕ Add Manager", callback_data=f"network_addmanager_{bot_id}")], [InlineKeyboardButton("⬅️ Back", callback_data=f"network_manage_{bot_id}")]]
        text = "👥 <b>Bot Managers</b>\n\n" + ("\n".join(f"• <code>{m['user_id']}</code>" for m in managers) if managers else "No managers added.")
        await q.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("network_addmanager_"):
        bot_id = int(data.rsplit("_", 1)[1]); b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b: return
        context.user_data["network_action"] = ("add_manager", bot_id)
        await q.message.reply_text("Send the Telegram user ID to add as a manager. They must have started your bot at least once to upload files.")
    elif data.startswith("network_delmanager_"):
        parts = data.split("_"); bot_id, manager_id = int(parts[2]), int(parts[3])
        b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b: return
        await bot_managers_col.delete_one({"bot_id": bot_id, "user_id": manager_id})
        await q.message.reply_text("✅ Manager removed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Managers", callback_data=f"network_managers_{bot_id}")]]))
    elif data.startswith("network_banmenu_"):
        bot_id = int(data.rsplit("_", 1)[1]); b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b: return
        await q.message.reply_text("🚫 <b>User Management</b>\n\nUse the buttons or commands to ban/unban by Telegram ID.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Ban User", callback_data=f"network_banuser_{bot_id}")], [InlineKeyboardButton("✅ Unban User", callback_data=f"network_unbanuser_{bot_id}")], [InlineKeyboardButton("⬅️ Back", callback_data=f"network_manage_{bot_id}")]]))
    elif data.startswith("network_banuser_") or data.startswith("network_unbanuser_"):
        kind = "ban_user" if data.startswith("network_banuser_") else "unban_user"
        bot_id = int(data.rsplit("_", 1)[1]); b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b: return
        context.user_data["network_action"] = (kind, bot_id)
        await q.message.reply_text("Send the Telegram user ID.")
    elif data.startswith("network_welcome_"):
        bot_id = int(data.rsplit("_", 1)[1]); b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b: return
        context.user_data["network_action"] = ("set_welcome", bot_id)
        await q.message.reply_text("Send the new welcome text for your bot (up to 3500 characters).")
    elif data.startswith("network_channel_"):
        bot_id = int(data.rsplit("_", 1)[1]); b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b: return
        context.user_data["network_action"] = ("set_channel", bot_id)
        await q.message.reply_text("Send your Telegram channel URL and optional button label. Example:\nhttps://t.me/mychannel My Channel")
    elif data.startswith("network_broadcast_"):
        bot_id = int(data.rsplit("_", 1)[1]); b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b: return
        context.user_data["network_action"] = ("broadcast_text", bot_id)
        await q.message.reply_text("Send the broadcast text now. For photo/video/document broadcasts, send the media to your bot and reply to it with /broadcast.")
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
    submit_bot_id = context.user_data.get("network_await_submit_username")
    if submit_bot_id:
        raw_username = (update.message.text or "").strip()
        username = raw_username.lstrip("@").strip()
        pending = await bots_col.find_one({"_id": int(submit_bot_id), "owner_id": update.effective_user.id, "status": "pending_verification"})
        if not pending or username.lower() != str(pending.get("username", "")).lower():
            await update.message.reply_text(f"❌ Username doesn't match. Please send @{pending['username'] if pending else 'the registered bot username'}.")
            return True
        await verification_requests_col.update_one(
            {"_id": int(submit_bot_id)},
            {"$set": {"owner_id": update.effective_user.id, "username": pending["username"], "status": "pending", "submitted": True, "submitted_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        context.user_data.pop("network_await_submit_username", None)
        await update.message.reply_text(
            f"✅ @{pending['username']} was added to the verification queue.\n\n"
            "A verifier will collect the request from the Gudu Gaming bot and manually add it to the storage channel. "
            "After they finish, tap Continue Verification below.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Continue Verification", callback_data=f"network_continueverify_{submit_bot_id}")]])
        )
        return True
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
    if kind in ("add_manager", "ban_user", "unban_user"):
        raw = (update.message.text or "").strip()
        if not raw.isdigit():
            await update.message.reply_text("Please send a numeric Telegram user ID."); return True
        target = int(raw)
        if kind == "add_manager":
            if target == uid:
                await update.message.reply_text("You are already the owner."); return True
            await bot_managers_col.update_one({"bot_id": bot_id, "user_id": target}, {"$set": {"bot_id": bot_id, "user_id": target, "added_by": uid, "added_at": datetime.now(timezone.utc)}}, upsert=True)
            result = f"✅ User {target} added as a manager."
        elif kind == "ban_user":
            if target == uid:
                await update.message.reply_text("You cannot ban the bot owner."); return True
            await bot_bans_col.update_one({"bot_id": bot_id, "user_id": target}, {"$set": {"bot_id": bot_id, "user_id": target, "banned_at": datetime.now(timezone.utc)}}, upsert=True)
            result = f"🚫 User {target} banned from this bot."
        else:
            res = await bot_bans_col.delete_one({"bot_id": bot_id, "user_id": target})
            result = "✅ User unbanned." if res.deleted_count else "User was not banned."
        context.user_data.pop("network_action", None)
        await update.message.reply_text(result)
        return True
    if kind == "set_welcome":
        value = (update.message.text or "").strip()
        if not value or len(value) > 3500:
            await update.message.reply_text("Welcome text must be between 1 and 3500 characters."); return True
        await bots_col.update_one({"_id": bot_id, "owner_id": uid}, {"$set": {"welcome_text": value}})
        context.user_data.pop("network_action", None)
        await update.message.reply_text("✅ Welcome text updated."); return True
    if kind == "set_channel":
        parts = (update.message.text or "").strip().split(maxsplit=1)
        url = parts[0] if parts else ""
        if not re.match(r"^https?://t\.me/(?:[A-Za-z0-9_]{5,}|\+[^\s]+)$", url):
            await update.message.reply_text("Send a valid Telegram channel URL, optionally followed by button text."); return True
        label = parts[1][:50] if len(parts) > 1 else "📢 My Channel"
        await bots_col.update_one({"_id": bot_id, "owner_id": uid}, {"$set": {"channel_url": url, "channel_button": label}})
        context.user_data.pop("network_action", None)
        await update.message.reply_text("✅ Your channel button has been saved."); return True
    if kind == "broadcast_text":
        body = (update.message.text or "").strip()
        if not body:
            await update.message.reply_text("Please send a text message to broadcast."); return True
        targets = await users_col.find({"bot_id": bot_id}, {"user_id": 1}).to_list(None)
        sent = failed = 0
        for target in targets:
            try:
                await context.bot.send_message(target["user_id"], body); sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.04)
        context.user_data.pop("network_action", None)
        await update.message.reply_text(f"📡 Broadcast complete\n✅ Sent: {sent}\n❌ Failed: {failed}"); return True
    if kind == "replace_token":
        token=(update.message.text or "").strip()
        if re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,}", token):
            try: await update.message.delete()
            except Exception: pass
        try:
            candidate=Bot(token); await candidate.initialize(); me=await candidate.get_me()
            if me.id != int(bot_id):
                raise ValueError("Token is for a different bot")
            await _validate_customer_bot_access(candidate, me.id)
            await bots_col.update_one({"_id":bot_id,"owner_id":uid},{"$set":{"token_enc":_encrypt(token),"username":me.username,"display_name":me.first_name,"status":"starting"}})
            await _stop_runtime(bot_id)
            doc=await bots_col.find_one({"_id":bot_id}); await _start_runtime(doc)
            await update.message.reply_text("✅ Token replaced and bot restarted. Delete the old token from any place where it was exposed.")
        except Exception:
            await update.message.reply_text("❌ Token replacement failed. Check the token and the bot's required permissions, then try again.")
        finally:
            if 'candidate' in locals():
                try: await candidate.shutdown()
                except Exception: pass
            context.user_data.pop("network_action",None)
        return True
    return False


async def network_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("network_await_token",None)
    context.user_data.pop("network_await_submit_username",None)
    context.user_data.pop("network_action",None)
    await update.effective_message.reply_text("Cancelled.")


async def _bot_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    doc = await _lookup_bot(context)
    if not doc or q.from_user.id != int(doc["owner_id"]):
        return
    owned = await bots_col.find({"owner_id": int(doc["owner_id"])}).to_list(None)
    bot_ids = [int(x["_id"]) for x in owned]
    users = await users_col.count_documents({"bot_id": {"$in": bot_ids}}) if bot_ids else 0
    files = await files_col.count_documents({"bot_id": {"$in": bot_ids}, "owner_id": int(doc["owner_id"])}) if bot_ids else 0
    managers = await bot_managers_col.count_documents({"bot_id": {"$in": bot_ids}}) if bot_ids else 0
    downloads = await users_col.aggregate([{"$match": {"bot_id": {"$in": bot_ids}}}, {"$group": {"_id": None, "n": {"$sum": "$downloads"}}}]).to_list(1) if bot_ids else []
    await q.edit_message_text(
        f"👤 <b>My Account — @{context.bot.username}</b>\n\n📦 Files: <b>{files}</b>\n"
        f"👥 Users: <b>{users}</b>\n📥 Downloads: <b>{downloads[0]['n'] if downloads else 0}</b>\n"
        f"🤖 Bots: <b>{len(bot_ids)}</b>\n🛠 Managers: <b>{managers}</b>", parse_mode="HTML")

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


async def cmd_botverifyqueue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending bot usernames to the owner and configured verifiers (no user-to-verifier DMs)."""
    from config import OWNER_ID
    uid = update.effective_user.id if update.effective_user else 0
    verifier_doc = await network_settings_col.find_one({"_id": "bot_verifiers"}) or {}
    verifier_ids = [int(x) for x in verifier_doc.get("ids", [])]
    if uid != OWNER_ID and uid not in verifier_ids:
        return
    requests = await verification_requests_col.find({"status": "pending", "submitted": True}).sort("submitted_at", 1).to_list(100)
    if not requests:
        await update.effective_message.reply_text("📭 No pending bot verification requests.")
        return
    body = "📥 <b>Bot verification queue</b>\n\n" + "\n\n".join(
        f"🤖 <b>@{r.get('username','unknown')}</b>\nOwner ID: <code>{r.get('owner_id')}</code>\nRequest ID: <code>{r['_id']}</code>"
        for r in requests
    )
    await update.effective_message.reply_text(body, parse_mode="HTML")


async def cmd_setbotverifier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner command: /setbotverifier 1 USER_ID or /setbotverifier 2 USER_ID; use 0 to clear."""
    from config import OWNER_ID
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    if len(context.args) != 2 or context.args[0] not in ("1", "2"):
        await update.effective_message.reply_text("Usage: /setbotverifier 1 USER_ID\n/setbotverifier 2 USER_ID\nUse 0 as USER_ID to clear a slot.")
        return
    try:
        slot, verifier_id = int(context.args[0]), int(context.args[1])
        doc = await network_settings_col.find_one({"_id": "bot_verifiers"}) or {"ids": [None, None]}
        ids = list(doc.get("ids", [None, None]))
        while len(ids) < 2: ids.append(None)
        ids[slot - 1] = verifier_id or None
        await network_settings_col.update_one({"_id": "bot_verifiers"}, {"$set": {"ids": ids}}, upsert=True)
        await update.effective_message.reply_text(f"✅ Bot verifier {slot} updated: {verifier_id or 'cleared'}\nUse /botverifyqueue to collect submitted usernames.")
    except ValueError:
        await update.effective_message.reply_text("USER_ID must be a number.")
