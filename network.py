"""Multi-tenant Gudu Gaming custom file-share bots.

Each customer bot forwards uploaded Telegram messages into one private storage
channel. The customer bot must be an administrator of that channel. MongoDB
stores metadata only; file payloads stay in Telegram.
"""
import asyncio
import html
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
# /fsub is the Main Bot owner-gating list; /fsubmem is global_fsub_col.
main_fsub_col = db["fsub_channels"]
_owner_fsub_monitor_task = None

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


async def _check_owner_main_fsub(owner_id):
    """Check the member-bot owner's subscription to Main Bot /fsub channels.

    Returns (missing_channels, errors). An empty /fsub list imposes no owner gate.
    """
    missing, errors = [], []
    channels = await main_fsub_col.find().to_list(None)
    for channel in channels:
        ch_id = channel.get("_id")
        try:
            member = await _main_bot.get_chat_member(chat_id=int(ch_id), user_id=int(owner_id))
            status = _member_status_value(member)
            if status in ("creator", "administrator", "member"):
                continue
            if status == "restricted" and bool(getattr(member, "is_member", False)):
                continue
            if status in ("left", "kicked") or status == "restricted":
                missing.append(channel)
                continue
            raise ValueError(f"Unexpected membership status: {status or 'empty'}")
        except Exception as exc:
            log.warning("Main /fsub owner check failed (channel=%s owner=%s): %s", ch_id, owner_id, exc)
            errors.append(channel)
    return missing, errors


async def _owner_fsub_monitor():
    """Suspend only the bots owned by people who leave a Main Bot /fsub channel."""
    while True:
        try:
            async for doc in bots_col.find({"status": {"$nin": [
                "pending_verification", "verification_failed", "stopped_by_admin", "stopped"
            ]}}):
                bot_id = int(doc["_id"])
                owner_id = int(doc["owner_id"])
                missing, errors = await _check_owner_main_fsub(owner_id)
                if errors:
                    # Telegram/API uncertainty must not falsely suspend a member.
                    continue
                if missing:
                    if not doc.get("owner_fsub_blocked"):
                        await _stop_runtime(bot_id)
                        await bots_col.update_one({"_id": bot_id}, {"$set": {
                            "status": "stopped_by_owner_fsub",
                            "owner_fsub_blocked": True,
                            "owner_fsub_checked_at": datetime.now(timezone.utc),
                        }})
                        log.info("Suspended member bot %s: owner left Main Bot /fsub", bot_id)
                elif doc.get("owner_fsub_blocked") or doc.get("owner_fsub_check_error"):
                    try:
                        await bots_col.update_one({"_id": bot_id}, {"$set": {
                            "status": "starting", "owner_fsub_blocked": False,
                            "owner_fsub_check_error": False,
                            "owner_fsub_checked_at": datetime.now(timezone.utc),
                        }})
                        fresh = await bots_col.find_one({"_id": bot_id})
                        await _start_runtime(fresh)
                        log.info("Reactivated member bot %s: owner rejoined Main Bot /fsub", bot_id)
                    except Exception:
                        log.exception("Could not restart member bot %s after owner rejoined /fsub", bot_id)
                        await bots_col.update_one({"_id": bot_id}, {"$set": {"status": "error"}})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Member owner /fsub monitor iteration failed")
        await asyncio.sleep(60)


async def _start_runtime(doc):
    bot_id = int(doc["_id"])
    if doc.get("status") == "stopped_by_admin":
        return False
    missing, errors = await _check_owner_main_fsub(int(doc["owner_id"]))
    if errors:
        await bots_col.update_one({"_id": bot_id}, {"$set": {
            "status": "owner_fsub_check_error", "owner_fsub_check_error": True,
        }})
        return False
    if missing:
        await _stop_runtime(bot_id)
        await bots_col.update_one({"_id": bot_id}, {"$set": {
            "status": "stopped_by_owner_fsub", "owner_fsub_blocked": True,
            "owner_fsub_checked_at": datetime.now(timezone.utc),
        }})
        return False
    async with _start_lock:
        if bot_id in _running:
            return True
        token = _decrypt(doc["token_enc"])
        app = ApplicationBuilder().token(token).build()
        app.add_handler(CommandHandler("start", _bot_start))
        app.add_handler(CommandHandler("help", _bot_help))
        app.add_handler(CommandHandler("myaccount", cmd_bot_myaccount))
        app.add_handler(CommandHandler("panel", cmd_bot_panel))
        app.add_handler(CommandHandler("dashboard", cmd_bot_panel))
        app.add_handler(CommandHandler("addmanager", cmd_bot_addmanager))
        app.add_handler(CommandHandler("removemanager", cmd_bot_removemanager))
        app.add_handler(CommandHandler("listmanagers", cmd_bot_listmanagers))
        app.add_handler(CommandHandler("ban", cmd_bot_ban))
        app.add_handler(CommandHandler("unban", cmd_bot_unban))
        app.add_handler(CommandHandler("addfjoin", cmd_bot_addfjoin))
        app.add_handler(CommandHandler("addchannel", cmd_bot_addchannel))
        app.add_handler(CommandHandler("listfjoin", cmd_bot_listfjoin))
        app.add_handler(CommandHandler("removefjoin", cmd_bot_removefjoin))
        app.add_handler(CommandHandler("setwelcome", cmd_bot_setwelcome))
        app.add_handler(CommandHandler("setchannel", cmd_bot_setchannel))
        app.add_handler(CommandHandler("broadcast", cmd_bot_broadcast))
        app.add_handler(CallbackQueryHandler(_bot_verify_callback, pattern=r"^netverify:"))
        app.add_handler(CallbackQueryHandler(_bot_account_callback, pattern=r"^netaccount$"))
        app.add_handler(CallbackQueryHandler(_bot_panel_callback, pattern=r"^netpanel:"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _bot_panel_text))
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
                # Keep the public Telegram command menu minimal. Owner tools remain
                # available through the private inline Control Center, not exposed
                # as a long command list to ordinary bot users.
                await app.bot.set_my_commands([
                    BotCommand("start", "Start the bot / open shared files"),
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
        await bots_col.update_one({"_id": bot_id}, {"$set": {
            "status": "running", "owner_fsub_blocked": False,
            "owner_fsub_check_error": False,
            "last_started_at": datetime.now(timezone.utc),
        }})
        return True


async def startup_network_bots():
    global _owner_fsub_monitor_task
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
    if _owner_fsub_monitor_task is None or _owner_fsub_monitor_task.done():
        _owner_fsub_monitor_task = asyncio.create_task(_owner_fsub_monitor())


async def shutdown_network_bots():
    global _owner_fsub_monitor_task
    if _owner_fsub_monitor_task and not _owner_fsub_monitor_task.done():
        _owner_fsub_monitor_task.cancel()
        try:
            await _owner_fsub_monitor_task
        except asyncio.CancelledError:
            pass
        _owner_fsub_monitor_task = None
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
        rows.append([InlineKeyboardButton("🎛 Control Center", callback_data="netpanel:home")])
        rows.append([InlineKeyboardButton("👤 My Account", callback_data="netaccount")])
    await update.effective_message.reply_text(welcome, reply_markup=InlineKeyboardMarkup(rows) if rows else None)


async def _bot_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = await _lookup_bot(context)
    owner = bool(doc and update.effective_user and update.effective_user.id == int(doc["owner_id"]))
    text = "Open a share link to receive a file. Only the bot owner and approved managers can upload files."
    if owner:
        text += "\n\nUse the 🎛 Control Center button in /start to manage your bot."
    await update.effective_message.reply_text(text)


async def _all_required_channels(doc):
    """Return platform-required and bot-owner channels, de-duplicated by chat ID."""
    own = await owner_fsub_col.find({"bot_id": int(doc["_id"])}).to_list(None)
    global_channels = await global_fsub_col.find().to_list(None)
    # Platform channels are mandatory for every member bot, even if its owner
    # has not configured any personal Force Join channels.
    result, seen = [], set()
    for raw in global_channels + own:
        try:
            ch_id = int(raw.get("_id", raw.get("chat_id")))
        except (TypeError, ValueError):
            # Keep malformed configured entries so the check fails closed below.
            result.append(raw)
            continue
        if ch_id in seen:
            continue
        seen.add(ch_id)
        ch = dict(raw)
        ch["_id"] = ch_id
        result.append(ch)
    return result


def _member_status_value(member):
    """Normalize PTB status values without relying on Enum string formatting."""
    value = getattr(member, "status", None)
    value = getattr(value, "value", value)
    if value is None:
        return ""
    value = str(value).lower()
    return value.rsplit(".", 1)[-1]


async def _check_membership(user_id, doc):
    """Fail closed: a file is released only when every required channel is verified."""
    not_joined, errors = [], []
    for ch in await _all_required_channels(doc):
        ch_id = ch.get("_id")
        try:
            if ch_id is None:
                raise ValueError("Configured Force Join entry has no channel ID")
            member = await _main_bot.get_chat_member(chat_id=int(ch_id), user_id=int(user_id))
            status = _member_status_value(member)
            # Only explicit Telegram membership states count as joined. Unknown
            # states are verification errors, never an accidental pass.
            if status in ("creator", "administrator", "member"):
                continue
            if status == "restricted":
                if bool(getattr(member, "is_member", False)):
                    continue
                not_joined.append(ch)
                continue
            if status in ("left", "kicked"):
                not_joined.append(ch)
                continue
            raise ValueError(f"Unexpected Telegram membership status: {status or 'empty'}")
        except Exception as exc:
            log.warning("Network FSub check failed for channel %s: %s", ch_id, exc)
            errors.append(ch)
    return not_joined, errors


async def _send_gate(update, doc, token, channels, error=False):
    rows = []
    for ch in channels:
        url = ch.get("invite_link")
        title = str(ch.get("title") or "Required channel")
        if url and str(url).startswith(("https://t.me/", "http://t.me/")):
            rows.append([InlineKeyboardButton(f"📢 Join {title[:45]}", url=url)])
    rows.append([InlineKeyboardButton("🔄 I've Joined — Verify Again", callback_data=f"netverify:{token}")])
    if error:
        text = (
            "⚠️ I couldn't verify membership in every required channel. "
            "File delivery is paused for your safety. Please try again later or contact the bot owner."
        )
    else:
        text = "🔒 Join every required channel below, then tap Verify Again to receive the file."
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
    # In panel media-broadcast mode, media is copied to users rather than stored as a file.
    if context.user_data.get("netpanel_action") == "broadcast_media":
        await _bot_panel_broadcast_media(update, context)
        return
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
    if uid in await _bot_privileged_ids(context.bot.id, doc["owner_id"]):
        await update.effective_message.reply_text("You cannot ban the bot owner or a manager."); return
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
    doc = await _is_bot_owner_or_manager(update, context, owner_only=False)
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

async def cmd_bot_addchannel(update, context):
    """Add a member-bot subscription channel with its explicit invitation URL.

    Usage: /addchannel -1001234567890 https://t.me/+INVITATION_CODE
    The channel is stored only against the current customer bot.
    """
    doc = await _is_bot_owner_or_manager(update, context, owner_only=False)
    if not doc:
        return
    if len(context.args) != 2 or not context.args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text(
            "Usage: /addchannel -100XXXXXXXXXX https://t.me/+INVITATION_CODE"
        )
        return
    ch_id = int(context.args[0])
    invite_link = context.args[1].strip()
    if not re.match(r"^https?://t\.me/(?:\+[A-Za-z0-9_-]+|[A-Za-z0-9_]{5,})$", invite_link):
        await update.effective_message.reply_text("Please provide a valid Telegram channel invitation link.")
        return
    try:
        chat = await _main_bot.get_chat(ch_id)
        member = await _main_bot.get_chat_member(ch_id, _main_bot.id)
        if _member_status_value(member) not in ("administrator", "creator"):
            raise ValueError("Main bot is not an administrator")
        await owner_fsub_col.update_one(
            {"bot_id": int(context.bot.id), "_id": ch_id},
            {"$set": {
                "bot_id": int(context.bot.id),
                "owner_id": int(doc["owner_id"]),
                "title": chat.title or str(ch_id),
                "invite_link": invite_link,
            }},
            upsert=True,
        )
        await update.effective_message.reply_text(
            f"✅ Added {chat.title or ch_id} to @{context.bot.username}'s subscription channels."
        )
    except Exception as exc:
        log.warning("Could not add member-bot channel %s: %s", ch_id, type(exc).__name__)
        await update.effective_message.reply_text(
            "❌ Could not add this channel. Check the channel ID, invitation link, and that the main Gudu bot is an administrator."
        )


async def cmd_bot_listfjoin(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=False)
    if not doc: return
    own = await owner_fsub_col.find({"bot_id": int(context.bot.id)}).to_list(None)
    platform = await global_fsub_col.find().to_list(None)
    lines = ["📌 <b>Platform-required channels (set by Gudu admin)</b>"]
    lines += [f"• {c.get('title','Channel')} — <code>{c['_id']}</code>" for c in platform] or ["• None configured"]
    lines += ["\n📌 <b>Your channels</b>"]
    lines += [f"• {c.get('title','Channel')} — <code>{c['_id']}</code>" for c in own] or ["• No channels added"]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_bot_removefjoin(update, context):
    doc = await _is_bot_owner_or_manager(update, context, owner_only=False)
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
    privileged_ids = await _bot_privileged_ids(context.bot.id, doc["owner_id"])
    targets = await users_col.find({"bot_id": int(context.bot.id), "user_id": {"$nin": privileged_ids}}, {"user_id": 1}).to_list(None)
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
    users = downloads_total = 0
    for owned_bot in owned:
        bid = int(owned_bot["_id"])
        normal_filter = await _normal_bot_user_filter(bid, owned_bot["owner_id"])
        users += await users_col.count_documents(normal_filter)
        per_bot_downloads = await users_col.aggregate([
            {"$match": normal_filter},
            {"$group": {"_id": None, "n": {"$sum": {"$ifNull": ["$downloads", 0]}}}},
        ]).to_list(1)
        downloads_total += per_bot_downloads[0]["n"] if per_bot_downloads else 0
    files = await files_col.count_documents({"bot_id": {"$in": bot_ids}, "owner_id": int(doc["owner_id"])}) if bot_ids else 0
    managers = await bot_managers_col.count_documents({"bot_id": {"$in": bot_ids}}) if bot_ids else 0
    downloads = [{"n": downloads_total}] if bot_ids else []
    await update.effective_message.reply_text(
        f"👤 <b>My Account — @{context.bot.username}</b>\n\n"
        f"📦 Files: <b>{files}</b>\n👥 Users: <b>{users}</b>\n📥 Downloads: <b>{downloads[0]['n'] if downloads else 0}</b>\n"
        f"🤖 Bots: <b>{len(bot_ids)}</b>\n🛠 Managers: <b>{managers}</b>", parse_mode="HTML")

def _panel_home_markup(is_owner=True):
    rows = [
        [InlineKeyboardButton("📊 My Account", callback_data="netpanel:account"), InlineKeyboardButton("📁 Files", callback_data="netpanel:files")],
        [InlineKeyboardButton("👥 Users", callback_data="netpanel:users")],
        [InlineKeyboardButton("🔒 Force Join", callback_data="netpanel:fjoin"), InlineKeyboardButton("✏️ Welcome", callback_data="netpanel:welcome")],
        [InlineKeyboardButton("📢 My Channel", callback_data="netpanel:channel"), InlineKeyboardButton("📡 Broadcast", callback_data="netpanel:broadcast")],
        [InlineKeyboardButton("⏳ Auto Delete", callback_data="netpanel:autodel")],
    ]
    # Only the owner can appoint/remove managers. Managers get the operational controls.
    if is_owner:
        rows[1].append(InlineKeyboardButton("🛠 Managers", callback_data="netpanel:managers"))
    return InlineKeyboardMarkup(rows)


async def cmd_bot_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = await _lookup_bot(context)
    if not doc or not update.effective_user:
        return
    is_owner = update.effective_user.id == int(doc["owner_id"])
    is_manager = await _is_bot_manager(context.bot.id, update.effective_user.id)
    if not (is_owner or is_manager):
        await update.effective_message.reply_text("This control center is available only to this bot's owner and approved managers.")
        return
    context.user_data.pop("netpanel_action", None)
    title = "Owner Control Center" if is_owner else "Manager Control Center"
    await update.effective_message.reply_text(
        f"🎛 <b>{title}</b>\nManage @{context.bot.username} using the buttons below.",
        reply_markup=_panel_home_markup(is_owner=is_owner), parse_mode="HTML",
    )


async def _panel_account_text(context, doc):
    bot_id = int(context.bot.id)
    privileged_ids = await _bot_privileged_ids(bot_id, doc["owner_id"])
    users = await users_col.count_documents({"bot_id": bot_id, "user_id": {"$nin": privileged_ids}})
    files = await files_col.count_documents({"bot_id": bot_id, "owner_id": int(doc["owner_id"])})
    managers = await bot_managers_col.count_documents({"bot_id": bot_id})
    agg = await users_col.aggregate([
        {"$match": {"bot_id": bot_id, "user_id": {"$nin": privileged_ids}}},
        {"$group": {"_id": None, "n": {"$sum": {"$ifNull": ["$downloads", 0]}}}},
    ]).to_list(1)
    return (f"📊 <b>Bot Account — @{context.bot.username}</b>\n\n"
            f"📦 Files: <b>{files}</b>\n👥 Users: <b>{users}</b>\n"
            f"📥 Downloads: <b>{agg[0]['n'] if agg else 0}</b>\n🛠 Managers: <b>{managers}</b>")


async def _bot_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    doc = await _lookup_bot(context)
    if not doc:
        return
    is_owner = q.from_user.id == int(doc["owner_id"])
    is_manager = await _is_bot_manager(context.bot.id, q.from_user.id)
    if not (is_owner or is_manager):
        return
    data = q.data.split(":")
    action = data[1] if len(data) > 1 else "home"
    bot_id = int(context.bot.id)
    context.user_data.pop("netpanel_action", None)

    if (action in ("managers", "managerdel") or (action == "prompt" and len(data) == 3 and data[2] == "addmanager")) and not is_owner:
        return
    if action == "home":
        title = "Owner Control Center" if is_owner else "Manager Control Center"
        await q.edit_message_text(f"🎛 <b>{title}</b>\n@{context.bot.username}", reply_markup=_panel_home_markup(is_owner=is_owner), parse_mode="HTML")
    elif action == "account":
        await q.edit_message_text(await _panel_account_text(context, doc), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="netpanel:home")]]))
    elif action == "files":
        page = int(data[2]) if len(data) == 3 and data[2].isdigit() else 0
        per_page = 8
        query = {"bot_id": bot_id, "owner_id": int(doc["owner_id"])}
        total = await files_col.count_documents(query)
        items = await files_col.find(query).sort("created_at", -1).skip(page * per_page).limit(per_page).to_list(per_page)
        rows = []
        for item in items:
            label = (item.get("file_name") or "File")[:24]
            rows.append([InlineKeyboardButton(f"📄 {label}", callback_data=f"netpanel:fileinfo:{item['_id']}"), InlineKeyboardButton("🗑", callback_data=f"netpanel:filedel:{item['_id']}:{page}")])
        nav = []
        if page > 0: nav.append(InlineKeyboardButton("◀️ Previous", callback_data=f"netpanel:files:{page-1}"))
        if (page + 1) * per_page < total: nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"netpanel:files:{page+1}"))
        if nav: rows.append(nav)
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="netpanel:home")])
        body = f"📁 <b>File Manager</b>\nShowing {page*per_page+1 if total else 0}–{min((page+1)*per_page,total)} of {total} files.\nChoose a file for details or 🗑 to delete."
        if not items: body += "\n\nNo files uploaded yet."
        await q.edit_message_text(body, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif action == "fileinfo" and len(data) == 3:
        item = await files_col.find_one({"_id": data[2], "bot_id": bot_id, "owner_id": int(doc["owner_id"])})
        if not item:
            await q.edit_message_text("File not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 Back to files", callback_data="netpanel:files")]])); return
        created = item.get("created_at")
        created_text = created.strftime("%Y-%m-%d %H:%M UTC") if hasattr(created, "strftime") else str(created or "Unknown")
        size = item.get("file_size") or item.get("file_size_bytes")
        size_text = _fmt_size(int(size)) if isinstance(size, (int, float)) and size >= 0 else "Not recorded"
        await q.edit_message_text(f"📄 <b>File details</b>\n\n<b>Name:</b> {html.escape(str(item.get('file_name') or 'File')[:180])}\n<b>Size:</b> {size_text}\n<b>Added:</b> {created_text}\n<b>File ID:</b> <code>{item['_id']}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Delete file", callback_data=f"netpanel:filedel:{item['_id']}:0")], [InlineKeyboardButton("⬅️ Back to files", callback_data="netpanel:files")]]))
    elif action == "filedel" and len(data) in (3, 4):
        token = data[2]
        page = int(data[3]) if len(data) == 4 and data[3].isdigit() else 0
        item = await files_col.find_one({"_id": token, "bot_id": bot_id, "owner_id": int(doc["owner_id"])})
        if item:
            try: await _main_bot.delete_message(STORAGE_CHANNEL_ID, int(item["storage_message_id"]))
            except Exception: log.warning("Could not delete storage message for network file %s", token)
            await files_col.delete_one({"_id": token, "bot_id": bot_id, "owner_id": int(doc["owner_id"])})
        await q.edit_message_text("✅ File deleted." if item else "File was not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 Back to files", callback_data=f"netpanel:files:{page}")], [InlineKeyboardButton("🏠 Main menu", callback_data="netpanel:home")]]))
    elif action == "users":
        privileged_ids = await _bot_privileged_ids(bot_id, doc["owner_id"])
        items = await users_col.find({"bot_id": bot_id, "user_id": {"$nin": privileged_ids}}).sort("last_seen", -1).limit(10).to_list(10)
        rows = []
        for item in items:
            uid = int(item["user_id"])
            banned = bool(await bot_bans_col.find_one({"bot_id": bot_id, "user_id": uid}))
            name = (item.get("first_name") or str(uid))[:22]
            rows.append([InlineKeyboardButton(("🔓 Unban " if banned else "🚫 Ban ") + name, callback_data=f"netpanel:{'unban' if banned else 'ban'}:{uid}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="netpanel:home")])
        await q.edit_message_text("👥 <b>Recent users</b>\nTap a user to toggle their access.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif action in ("ban", "unban") and len(data) == 3 and data[2].isdigit():
        uid = int(data[2])
        if uid in await _bot_privileged_ids(bot_id, doc["owner_id"]):
            await q.edit_message_text("You cannot ban the owner or a manager.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👥 Back to users", callback_data="netpanel:users")]])); return
        if action == "ban":
            await bot_bans_col.update_one({"bot_id": bot_id, "user_id": uid}, {"$set": {"bot_id": bot_id, "user_id": uid, "banned_at": datetime.now(timezone.utc)}}, upsert=True)
            msg = f"🚫 User {uid} banned."
        else:
            await bot_bans_col.delete_one({"bot_id": bot_id, "user_id": uid})
            msg = f"✅ User {uid} unbanned."
        await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👥 Back to users", callback_data="netpanel:users")], [InlineKeyboardButton("🏠 Main menu", callback_data="netpanel:home")]]))
    elif action == "managers":
        items = await bot_managers_col.find({"bot_id": bot_id}).to_list(50)
        rows = [[InlineKeyboardButton(f"➖ Remove {x['user_id']}", callback_data=f"netpanel:managerdel:{x['user_id']}")] for x in items]
        rows.extend([[InlineKeyboardButton("➕ Add manager", callback_data="netpanel:prompt:addmanager")], [InlineKeyboardButton("⬅️ Back", callback_data="netpanel:home")]])
        await q.edit_message_text("🛠 <b>Manager management</b>\nRemove an existing manager or add a new one.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif action == "managerdel" and len(data) == 3 and data[2].isdigit():
        await bot_managers_col.delete_one({"bot_id": bot_id, "user_id": int(data[2])})
        await q.edit_message_text("✅ Manager removed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛠 Managers", callback_data="netpanel:managers")], [InlineKeyboardButton("🏠 Main menu", callback_data="netpanel:home")]]))
    elif action == "fjoin":
        own = await owner_fsub_col.find({"bot_id": bot_id}).to_list(50)
        platform = await global_fsub_col.find().to_list(50)
        rows = [[InlineKeyboardButton(f"➖ Remove {str(x.get('title') or x['_id'])[:24]}", callback_data=f"netpanel:fjoindel:{x['_id']}")] for x in own]
        rows.extend([[InlineKeyboardButton("➕ Add channel", callback_data="netpanel:prompt:addfjoin")], [InlineKeyboardButton("⬅️ Back", callback_data="netpanel:home")]])
        body = "🔒 <b>Force Join channels</b>\n\n<b>Platform-required (cannot remove):</b>\n" + ("\n".join(f"• {x.get('title','Channel')}" for x in platform) if platform else "• None configured")
        body += "\n\n<b>Your channels:</b>\n" + ("\n".join(f"• {x.get('title','Channel')}" for x in own) if own else "• None added")
        await q.edit_message_text(body, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif action == "fjoindel" and len(data) == 3:
        try: ch_id = int(data[2])
        except ValueError: return
        await owner_fsub_col.delete_one({"bot_id": bot_id, "_id": ch_id, "owner_id": int(doc["owner_id"])})
        await q.edit_message_text("✅ Your channel was removed (platform channels are protected).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔒 Force Join", callback_data="netpanel:fjoin")], [InlineKeyboardButton("🏠 Main menu", callback_data="netpanel:home")]]))
    elif action == "welcome":
        context.user_data["netpanel_action"] = "welcome"
        await q.edit_message_text("✏️ Send the new welcome message as your next text message. Send /cancel to stop.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="netpanel:home")]]))
    elif action == "channel":
        context.user_data["netpanel_action"] = "channel"
        await q.edit_message_text("📢 Send your channel link, optionally followed by | and button text.\nExample: https://t.me/mychannel | 📢 Join my channel\nSend /cancel to stop.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="netpanel:home")]]))
    elif action == "autodel":
        context.user_data["netpanel_action"] = "autodel"
        await q.edit_message_text(f"⏳ Current auto-delete: {_fmt_time(int(doc.get('auto_del', 300)))}\nSend the new delay in seconds (minimum 30, maximum 86400).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="netpanel:home")]]))
    elif action == "broadcast":
        rows = [[InlineKeyboardButton("📝 Broadcast text", callback_data="netpanel:prompt:broadcast")], [InlineKeyboardButton("🖼 Broadcast media", callback_data="netpanel:prompt:broadcast_media")], [InlineKeyboardButton("⬅️ Back", callback_data="netpanel:home")]]
        await q.edit_message_text("📡 <b>Broadcast</b>\nChoose text or media broadcast. Only users who started this bot are targeted.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif action == "prompt" and len(data) == 3:
        context.user_data["netpanel_action"] = data[2]
        prompts = {
            "addmanager": "Send the manager's numeric Telegram user ID.",
            "addfjoin": "Send the channel ID (example: -1001234567890). The main Gudu bot must be an admin there.",
            "broadcast": "Send the broadcast text now.",
            "broadcast_media": "Now send the photo, video, audio or document to broadcast.",
        }
        await q.edit_message_text(prompts.get(data[2], "Send the requested value now.") + "\nSend /cancel to stop.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="netpanel:home")]]))
    else:
        await q.edit_message_text("🎛 <b>Owner Control Center</b>", parse_mode="HTML", reply_markup=_panel_home_markup())


async def _bot_panel_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("netpanel_action")
    if not action or not update.effective_user:
        return
    doc = await _lookup_bot(context)
    if not doc or not (update.effective_user.id == int(doc["owner_id"]) or await _is_bot_manager(context.bot.id, update.effective_user.id)):
        context.user_data.pop("netpanel_action", None)
        return
    msg = update.effective_message
    value = (msg.text or "").strip()
    bot_id = int(context.bot.id)
    if action == "addmanager" and update.effective_user.id != int(doc["owner_id"]):
        context.user_data.pop("netpanel_action", None)
        await msg.reply_text("Only this bot's owner can add managers.")
        return
    if action == "autodel":
        if not value.isdigit() or not 30 <= int(value) <= 86400:
            await msg.reply_text("Send a whole number of seconds between 30 and 86400."); return
        await bots_col.update_one({"_id": bot_id, "owner_id": int(doc["owner_id"])}, {"$set": {"auto_del": int(value)}})
        result = f"✅ Auto-delete updated to {_fmt_time(int(value))}."
    elif action == "welcome":
        if not value or len(value) > 3500:
            await msg.reply_text("Please send welcome text between 1 and 3500 characters."); return
        await bots_col.update_one({"_id": bot_id, "owner_id": int(doc["owner_id"])}, {"$set": {"welcome_text": value}})
        result = "✅ Welcome message updated."
    elif action == "channel":
        parts = [x.strip() for x in value.split("|", 1)]
        url = parts[0]
        if not re.match(r"^https?://t\.me/(?:[A-Za-z0-9_]{5,}|\+[^\s]+)$", url):
            await msg.reply_text("Invalid Telegram channel link. Example: https://t.me/mychannel | 📢 Join my channel"); return
        label = parts[1][:50] if len(parts) > 1 and parts[1] else "📢 My Channel"
        await bots_col.update_one({"_id": bot_id}, {"$set": {"channel_url": url, "channel_button": label}})
        result = "✅ Your channel button has been saved."
    elif action == "addmanager":
        if not value.isdigit(): await msg.reply_text("Send a numeric Telegram user ID."); return
        uid = int(value)
        if uid == int(doc["owner_id"]): await msg.reply_text("The owner already has full access."); return
        await bot_managers_col.update_one({"bot_id": bot_id, "user_id": uid}, {"$set": {"bot_id": bot_id, "user_id": uid, "added_by": update.effective_user.id, "added_at": datetime.now(timezone.utc)}}, upsert=True)
        result = f"✅ User {uid} is now a manager."
    elif action == "addfjoin":
        if not value.lstrip("-").isdigit(): await msg.reply_text("Send a valid numeric channel ID."); return
        ch_id = int(value)
        try:
            chat = await _main_bot.get_chat(ch_id)
            member = await _main_bot.get_chat_member(ch_id, _main_bot.id)
            if str(member.status).lower() not in ("administrator", "creator"): raise ValueError("Main bot is not admin")
            invite = f"https://t.me/{chat.username}" if chat.username else (await _main_bot.create_chat_invite_link(ch_id, name="Bot Force Join")).invite_link
            await owner_fsub_col.update_one({"bot_id": bot_id, "_id": ch_id}, {"$set": {"bot_id": bot_id, "owner_id": int(doc["owner_id"]), "title": chat.title, "invite_link": invite}}, upsert=True)
            result = f"✅ Added {chat.title} to your Force Join list."
        except Exception:
            await msg.reply_text("❌ Could not add channel. Ensure the main Gudu bot is an admin and the channel ID is correct."); return
    elif action == "broadcast":
        privileged_ids = await _bot_privileged_ids(bot_id, doc["owner_id"])
        targets = await users_col.find({"bot_id": bot_id, "user_id": {"$nin": privileged_ids}}, {"user_id": 1}).to_list(None)
        sent = failed = 0
        status = await msg.reply_text(f"📡 Broadcasting to {len(targets)} users…")
        for target in targets:
            try: await context.bot.send_message(int(target["user_id"]), value); sent += 1
            except Exception: failed += 1
            await asyncio.sleep(0.04)
        result = f"📡 Broadcast complete\n✅ Sent: {sent}\n❌ Failed: {failed}"
        await status.edit_text(result)
        context.user_data.pop("netpanel_action", None)
        return
    else:
        return
    context.user_data.pop("netpanel_action", None)
    await msg.reply_text(result, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎛 Open Control Center", callback_data="netpanel:home")]]))


async def _bot_panel_broadcast_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = await _lookup_bot(context)
    msg = update.effective_message
    if not doc or not update.effective_user or not (update.effective_user.id == int(doc["owner_id"]) or await _is_bot_manager(context.bot.id, update.effective_user.id)):
        context.user_data.pop("netpanel_action", None); return
    privileged_ids = await _bot_privileged_ids(context.bot.id, doc["owner_id"])
    targets = await users_col.find({"bot_id": int(context.bot.id), "user_id": {"$nin": privileged_ids}}, {"user_id": 1}).to_list(None)
    sent = failed = 0
    status = await msg.reply_text(f"📡 Broadcasting media to {len(targets)} users…")
    for target in targets:
        try:
            await context.bot.copy_message(int(target["user_id"]), msg.chat_id, msg.message_id); sent += 1
        except Exception: failed += 1
        await asyncio.sleep(0.04)
    context.user_data.pop("netpanel_action", None)
    await status.edit_text(f"📡 Media broadcast complete\n✅ Sent: {sent}\n❌ Failed: {failed}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎛 Open Control Center", callback_data="netpanel:home")]]))


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
            started = await _start_runtime(await bots_col.find_one({"_id": bot_id}))
            if not started:
                await q.message.reply_text(
                    "⚠️ Your bot passed technical verification, but it cannot start until you join every Main Bot /fsub channel. "
                    "Join the required channels and try Continue Verification again."
                )
                return
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
    elif data in ("network_member", "network_mybots"):
        # The Member button is the single entry point for all owner bot controls.
        docs = await bots_col.find({"owner_id": uid}).sort("created_at", -1).to_list(None)
        if not docs:
            await q.message.reply_text("You haven't connected a custom bot yet.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Create a Bot", callback_data="network_create")]]))
            return
        rows = [[InlineKeyboardButton(f"@{b['username']} · {b.get('status','unknown')}", callback_data=f"network_manage_{b['_id']}")] for b in docs]
        rows.append([InlineKeyboardButton("➕ Add another bot", callback_data="network_create")])
        await q.message.reply_text("👤 <b>Member Control Center</b>\n\nSelect one of your bots to open its full management controls:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("network_manage_"):
        bot_id = int(data.rsplit("_", 1)[1])
        b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b:
            await q.message.reply_text("Bot not found in your account.")
            return
        privileged_ids = await _bot_privileged_ids(bot_id, b["owner_id"])
        users = await users_col.count_documents({"bot_id": bot_id, "user_id": {"$nin": privileged_ids}})
        files = await files_col.count_documents({"bot_id": bot_id, "owner_id": uid})
        rows = [
            [InlineKeyboardButton("📊 Statistics", callback_data=f"network_stats_{bot_id}"), InlineKeyboardButton("📁 Manage Files", callback_data=f"network_files_{bot_id}")],
            [InlineKeyboardButton("🔒 Add Force Subscribe", callback_data=f"network_addfsub_{bot_id}"), InlineKeyboardButton("📋 My Channels", callback_data=f"network_fsub_{bot_id}")],
            [InlineKeyboardButton("👥 Managers", callback_data=f"network_managers_{bot_id}"), InlineKeyboardButton("🚫 Ban / Unban", callback_data=f"network_banmenu_{bot_id}")],
            [InlineKeyboardButton("✏️ Welcome Text", callback_data=f"network_welcome_{bot_id}"), InlineKeyboardButton("📢 My Channel", callback_data=f"network_channel_{bot_id}")],
            [InlineKeyboardButton("📡 Broadcast", callback_data=f"network_broadcast_{bot_id}")],
            [InlineKeyboardButton("⏳ Auto Delete", callback_data=f"network_autodel_{bot_id}")],
            [InlineKeyboardButton("🔄 Restart", callback_data=f"network_restart_{bot_id}"), InlineKeyboardButton("⏸ Stop", callback_data=f"network_stop_{bot_id}")],
            [InlineKeyboardButton("🔐 Replace Token", callback_data=f"network_replace_{bot_id}")],
            [InlineKeyboardButton("⬅️ Member", callback_data="network_member")],
        ]
        await q.message.reply_text(f"🤖 <b>@{b['username']}</b>\nStatus: <b>{b.get('status','unknown')}</b>\n\n👥 Users: {users}\n📦 Files: {files}\n⏳ Auto-delete: {_fmt_time(int(b.get('auto_del',300)))}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("network_files_"):
        bot_id = int(data.rsplit("_", 1)[1]); b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b: return
        items = await files_col.find({"bot_id": bot_id, "owner_id": uid}).sort("created_at", -1).limit(15).to_list(15)
        rows = [[InlineKeyboardButton(f"🗑 {(x.get('file_name') or 'File')[:28]}", callback_data=f"network_delfile_{bot_id}_{x['_id']}")] for x in items]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"network_manage_{bot_id}")])
        body = f"📁 <b>Files for @{b['username']}</b>\nTap a file to delete it. Showing up to 15 newest files."
        if not items: body += "\n\nNo files uploaded yet."
        await q.message.reply_text(body, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("network_delfile_"):
        parts = data.split("_", 3)
        if len(parts) != 4 or not parts[2].isdigit(): return
        bot_id, token = int(parts[2]), parts[3]
        b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        item = await files_col.find_one({"_id": token, "bot_id": bot_id, "owner_id": uid}) if b else None
        if not item:
            await q.message.reply_text("File not found or you don't own this bot.")
            return
        try:
            await _main_bot.delete_message(STORAGE_CHANNEL_ID, int(item["storage_message_id"]))
        except Exception:
            log.warning("Could not delete storage message for network file %s", token)
        await files_col.delete_one({"_id": token, "bot_id": bot_id, "owner_id": uid})
        await q.message.reply_text("✅ File deleted.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 Back to Files", callback_data=f"network_files_{bot_id}")], [InlineKeyboardButton("⬅️ Bot Controls", callback_data=f"network_manage_{bot_id}")]]))
    elif data.startswith("network_stats_"):
        bot_id = int(data.rsplit("_", 1)[1]); b = await bots_col.find_one({"_id": bot_id, "owner_id": uid})
        if not b: return
        privileged_ids = await _bot_privileged_ids(bot_id, b["owner_id"])
        users = await users_col.count_documents({"bot_id": bot_id, "user_id": {"$nin": privileged_ids}}); files = await files_col.count_documents({"bot_id": bot_id, "owner_id": uid})
        downloads = await users_col.aggregate([{"$match":{"bot_id":bot_id,"user_id":{"$nin":privileged_ids}}},{"$group":{"_id":None,"n":{"$sum":"$downloads"}}}]).to_list(1)
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
        privileged_ids = await _bot_privileged_ids(bot_id, b["owner_id"])
        targets = await users_col.find({"bot_id": bot_id, "user_id": {"$nin": privileged_ids}}, {"user_id": 1}).to_list(None)
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
    context.user_data.pop("netpanel_action",None)
    await update.effective_message.reply_text("Cancelled.")


async def _bot_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    doc = await _lookup_bot(context)
    if not doc or q.from_user.id != int(doc["owner_id"]):
        return
    owned = await bots_col.find({"owner_id": int(doc["owner_id"])}).to_list(None)
    bot_ids = [int(x["_id"]) for x in owned]
    users = downloads_total = 0
    for owned_bot in owned:
        bid = int(owned_bot["_id"])
        normal_filter = await _normal_bot_user_filter(bid, owned_bot["owner_id"])
        users += await users_col.count_documents(normal_filter)
        per_bot_downloads = await users_col.aggregate([
            {"$match": normal_filter},
            {"$group": {"_id": None, "n": {"$sum": {"$ifNull": ["$downloads", 0]}}}},
        ]).to_list(1)
        downloads_total += per_bot_downloads[0]["n"] if per_bot_downloads else 0
    files = await files_col.count_documents({"bot_id": {"$in": bot_ids}, "owner_id": int(doc["owner_id"])}) if bot_ids else 0
    managers = await bot_managers_col.count_documents({"bot_id": {"$in": bot_ids}}) if bot_ids else 0
    downloads = [{"n": downloads_total}] if bot_ids else []
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
