import asyncio
import logging
import re
import uuid
import secrets
import string
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from config import (
    BOT_TOKEN, BOT_USERNAME, BOT_NAME, CHANNEL_URL, CHANNEL_BTN, OWNER_ID,
    DB_URI, DB_NAME, DB_CHANNEL, AUTO_DEL,
    START_PHOTO, FSUB_PHOTO, DEFAULT_MESSAGES,
)

logging.basicConfig(format="%(asctime)s — %(levelname)s — %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

_client = AsyncIOMotorClient(DB_URI, tz_aware=True)
_db = _client[DB_NAME]
files_col = _db["files"]
users_col = _db["users"]
settings_col = _db["settings"]
fsub_col = _db["fsub_channels"]
banned_col = _db["banned"]
managers_col = _db["managers"]
keys_col = _db["user_keys"]


def _make_token(fname):
    base = re.sub(r'\.[^.]+$', '', fname)
    slug = re.sub(r'[^\w]', '_', base).strip('_')
    slug = re.sub(r'_+', '_', slug)[:40]
    return f"{slug}_{uuid.uuid4().hex[:5]}" if slug else uuid.uuid4().hex[:12]


def _fmt_size(b):
    if b >= 1073741824: return f"{b/1073741824:.1f} GB"
    if b >= 1048576: return f"{b/1048576:.1f} MB"
    if b >= 1024: return f"{b/1024:.1f} KB"
    return f"{b} B"


def _fmt_del(s):
    if s >= 3600: return f"{s//3600}h"
    if s >= 60: return f"{s//60}m"
    return f"{s}s"


def _back_btn():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm_back")]])


async def db_store_file(file_id, file_type, file_name, message_id, token):
    await files_col.insert_one({"_id": token, "file_id": file_id, "file_type": file_type, "file_name": file_name, "message_id": message_id})


async def db_get_file(token):
    return await files_col.find_one({"_id": token})


async def db_del_file(token):
    await files_col.delete_one({"_id": token})


async def db_list_files(limit=15):
    return await files_col.find().sort("_id", -1).limit(limit).to_list(None)


async def db_add_user(uid, first, username):
    await users_col.update_one({"_id": uid}, {"$set": {"first_name": first, "username": username}}, upsert=True)


async def db_all_users():
    return await users_col.find({}, {"_id": 1}).to_list(None)


async def db_user_count():
    return await users_col.count_documents({})


async def db_file_count():
    return await files_col.count_documents({})


async def db_is_banned(uid):
    return await banned_col.find_one({"_id": uid}) is not None


async def db_ban(uid):
    await banned_col.update_one({"_id": uid}, {"$set": {"_id": uid}}, upsert=True)


async def db_unban(uid):
    await banned_col.delete_one({"_id": uid})


async def db_get_setting(key, default=None):
    doc = await settings_col.find_one({"_id": key})
    return doc["value"] if doc else default


async def db_set_setting(key, value):
    await settings_col.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)


async def db_get_msg(key):
    val = await db_get_setting(f"msg_{key}")
    return val if val is not None else DEFAULT_MESSAGES.get(key, "")


async def db_save_msg(key, value):
    await db_set_setting(f"msg_{key}", value)


async def db_get_auto_del():
    return int(await db_get_setting("auto_del", AUTO_DEL))


async def db_add_fsub(ch_id, title, invite_link=None):
    await fsub_col.update_one(
        {"_id": ch_id},
        {"$set": {"title": title, "invite_link": invite_link}},
        upsert=True,
    )


async def db_remove_fsub(ch_id):
    await fsub_col.delete_one({"_id": ch_id})


async def db_get_fsub():
    return await fsub_col.find().to_list(None)


async def get_channel_invite(context, ch_id):
    """Return a usable join URL for a public or private channel.

    For private channels, create a separate invite link instead of exporting the
    primary link (which can invalidate a previously shared primary link).
    The bot must be an administrator with invite-user permission.
    """
    try:
        chat = await context.bot.get_chat(ch_id)
        if chat.username:
            return f"https://t.me/{chat.username}", chat.title

        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=ch_id,
                name="Gudu Gaming Force Subscribe",
            )
            return invite.invite_link, chat.title
        except Exception:
            # Use an existing primary invite link if Telegram exposes one.
            if chat.invite_link:
                return chat.invite_link, chat.title
            raise
    except Exception as e:
        logging.warning("Could not get invite link for force-sub channel %s: %s", ch_id, e)
        return None, str(ch_id)


async def check_fsub(uid, context):
    """Return (channels_not_joined, verification_errors).

    API/configuration failures are deliberately kept separate from a genuine
    non-membership result, so users are not incorrectly told they did not join.
    """
    not_joined, errors = [], []
    for ch in await db_get_fsub():
        ch_id = ch["_id"]
        try:
            member = await context.bot.get_chat_member(chat_id=ch_id, user_id=uid)
            status = str(member.status).lower()
            is_member = getattr(member, "is_member", None)
            if status in ("left", "kicked") or (status == "restricted" and is_member is False):
                not_joined.append(ch)
        except Exception as exc:
            logging.exception("Force-sub API check failed (channel=%s user=%s)", ch_id, uid)
            errors.append((ch, str(exc)))
    return not_joined, errors


def _utcnow():
    return datetime.now(timezone.utc)


def _new_free_key():
    suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(9))
    return f"GG_FREE_1D_{suffix}"


async def _get_user_key(uid):
    docs = await keys_col.find({"user_id": uid, "revoked": {"$ne": True}}).sort("created_at", -1).limit(1).to_list(length=1)
    doc = docs[0] if docs else None
    if doc and doc.get("expires_at") and doc["expires_at"] <= _utcnow():
        await keys_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "expired"}})
        return None
    return doc


async def _issue_free_key(update, context):
    uid = update.effective_user.id
    existing = await _get_user_key(uid)
    if existing:
        await update.effective_message.reply_text(
            "⚠️ You already have an active key. Use /mykey to view it. Delete your current key before generating another one."
        )
        return
    now = _utcnow()
    key = _new_free_key()
    await keys_col.insert_one({
        "_id": key, "user_id": uid, "key": key, "type": "FREE",
        "created_at": now, "expires_at": now + timedelta(days=1),
        "status": "active", "revoked": False, "devices": [], "device_limit": 1,
        "note": "-",
    })
    await update.effective_message.reply_text(
        "🎉 <b>Free key generated successfully!</b>\n\n"
        f"🔑 <code>{key}</code>\n"
        "⏰ Valid for 1 day from now.\n"
        "📱 Device limit: 1\n\n"
        "Use /mykey to view your key details.", parse_mode="HTML"
    )


async def getkey_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if await db_is_banned(user.id):
        await update.effective_message.reply_text(await db_get_msg("BANNED"))
        return
    await db_add_user(user.id, user.first_name, user.username or "")
    required_channels = await db_get_fsub()
    if not required_channels:
        await update.effective_message.reply_text(
            "⚠️ Free key generation is temporarily unavailable. The bot owner must configure Force Subscribe channels first."
        )
        return
    not_joined, errors = await check_fsub(user.id, context)
    if errors:
        await update.effective_message.reply_text(
            "⚠️ I couldn't verify channel membership. Please contact @subro98 / @gudugaming8069."
        )
        return
    if not_joined:
        await send_fsub(update, context, not_joined, "KEY", user.first_name)
        return
    await _issue_free_key(update, context)


async def _send_no_key(message, *, edit_query=None):
    text = "❌ You have no Key\nUse /getkey to get one free"
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Get Free Key", callback_data="key_get")],
    ])
    if edit_query is not None:
        await _edit_callback_message(edit_query, text, markup)
    else:
        await message.reply_text(text, reply_markup=markup)


async def _key_info_text(uid, doc):
    created = doc["created_at"].strftime("%Y-%m-%d")
    expires = doc["expires_at"].strftime("%Y-%m-%d %H:%M UTC")
    devices = len(doc.get("devices", []))
    return (
        "✅ <b>Active</b>\n\n🔑 <b>Key Info</b>\n\n"
        f"🗝 Key: <code>{doc['key']}</code>\n"
        f"📋 Type: {doc.get('type', 'FREE')}\n"
        f"⏰ Expires: {expires}\n"
        f"📱 Devices: {devices}/{doc.get('device_limit', 1)}\n"
        f"👤 UID: <code>{uid}</code>\n"
        f"📅 Created: {created}\n"
        "📊 Status: ✅ Active\n"
        f"📝 Note: {doc.get('note', '-')}"
    )


def _key_info_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Delete Key", callback_data="key_delete"),
         InlineKeyboardButton("🔄 Reset Devices", callback_data="key_reset")]
    ])


async def mykey_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = await _get_user_key(user.id)
    if not doc:
        await _send_no_key(update.effective_message)
        return
    await update.effective_message.reply_text(
        await _key_info_text(user.id, doc), parse_mode="HTML",
        reply_markup=_key_info_markup()
    )

async def resetkey_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = await _get_user_key(update.effective_user.id)
    if not doc:
        await _send_no_key(update.effective_message)
        return
    text = f"⚠️ <b>Confirm Reset Devices?</b>\n\n<code>{doc['key']}</code>"
    await update.effective_message.reply_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data="key_reset_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="key_reset_cancel"),
        ]])
    )

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    user = update.effective_user
    if await db_is_banned(user.id):
        await update.message.reply_text(await db_get_msg("BANNED"))
        return
    await db_add_user(user.id, user.first_name, user.username or "")
    if context.args:
        token = context.args[0]
        not_joined, errors = await check_fsub(user.id, context)
        if errors:
            await update.message.reply_text(
                "⚠️ I couldn't verify channel membership right now. The bot admin must "
                "check that the bot is an administrator in every force-sub channel and "
                "that the configured channel ID is correct. Please contact "
                "@subro98 / @gudugaming8069."
            )
            return
        if not_joined:
            await send_fsub(update, context, not_joined, token, user.first_name)
            return
        await deliver_file(update, context, token)
        return
    msg = (
        f"👋 <b>Hello {user.first_name}!</b>\n"
        "Welcome to GUDU GAMING Bot 🎮\n"
        "Type /help for commands\n\n"
        "<i>Any issue? Contact • @subro98 / @gudugaming8069</i>"
    )
    btn = [
        [InlineKeyboardButton("🔑 Get Free Key", callback_data="key_get"),
         InlineKeyboardButton("📋 My Key", callback_data="key_my")],
        [InlineKeyboardButton("🔗 Get a File", callback_data="user_getfile"),
         InlineKeyboardButton("📖 How It Works", callback_data="user_help")],
        [InlineKeyboardButton("👤 My Account", callback_data="user_account"),
         InlineKeyboardButton("📢 Official Channel", url=CHANNEL_URL)],
    ]
    if _is_admin(user.id):
        btn.append([InlineKeyboardButton("👑 Admin Dashboard", callback_data="user_admin")])
    elif await _is_manager(user.id):
        btn.append([InlineKeyboardButton("🛠 AdminGG", callback_data="user_manager")])
    markup = InlineKeyboardMarkup(btn)
    try:
        if START_PHOTO:
            await update.message.reply_photo(photo=START_PHOTO, caption=msg, parse_mode="HTML", reply_markup=markup)
        else:
            await update.message.reply_text(msg, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=markup)


async def user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    if q.data.startswith("fsub_verify:"):
        token = q.data.split(":", 1)[1]
        not_joined, errors = await check_fsub(q.from_user.id, context)
        if errors:
            await q.answer("Membership check failed. Please contact the bot admin.", show_alert=True)
            try:
                await q.message.reply_text(
                    "⚠️ Telegram could not verify this channel. The bot must be an "
                    "administrator in the force-sub channel, and its saved channel ID "
                    "must be correct. Contact @subro98 / @gudugaming8069."
                )
            except Exception:
                pass
            return
        if not_joined:
            await q.answer("You haven't joined all required channels yet. Join them and try again.", show_alert=True)
            await send_fsub(update, context, not_joined, token, q.from_user.first_name)
            return
        if token == "KEY":
            await q.answer("Membership verified! Generating your key…")
            await _issue_free_key(update, context)
            return
        await q.answer("Membership verified! Sending your file…")
        try:
            await q.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await deliver_file(update, context, token)
        return
    if not q.data.startswith("key_"):
        await q.answer()
    if q.data == "key_get":
        required_channels = await db_get_fsub()
        if not required_channels:
            await _edit_callback_message(
                q,
                "⚠️ Free key generation is temporarily unavailable. The bot owner must configure Force Subscribe channels first."
            )
            return
        not_joined, errors = await check_fsub(q.from_user.id, context)
        if errors:
            await q.answer("Membership check unavailable. Contact support.", show_alert=True)
        elif not_joined:
            await q.answer("Join all required channels first.", show_alert=True)
            await send_fsub(update, context, not_joined, "KEY", q.from_user.first_name)
        else:
            await q.answer()
            await _issue_free_key(update, context)
    elif q.data == "key_my":
        await q.answer()
        doc = await _get_user_key(q.from_user.id)
        if not doc:
            await _send_no_key(q.message, edit_query=q)
        else:
            await _edit_callback_message(q, await _key_info_text(q.from_user.id, doc), _key_info_markup())
    elif q.data == "key_delete":
        await q.answer()
        doc = await _get_user_key(q.from_user.id)
        if not doc:
            await _send_no_key(q.message, edit_query=q)
        else:
            text = f"⚠️ <b>Confirm delete key?</b>\n\n<code>{doc['key']}</code>"
            await _edit_callback_message(q, text, InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="key_delete_cancel"),
                InlineKeyboardButton("✅ Confirm", callback_data="key_delete_confirm"),
            ]]))
    elif q.data == "key_delete_confirm":
        await q.answer()
        doc = await _get_user_key(q.from_user.id)
        if not doc:
            await _send_no_key(q.message, edit_query=q)
        else:
            await keys_col.update_one(
                {"_id": doc["_id"], "user_id": q.from_user.id},
                {"$set": {"revoked": True, "status": "deleted", "deleted_at": _utcnow()}}
            )
            await _send_no_key(q.message, edit_query=q)
    elif q.data == "key_delete_cancel":
        await q.answer()
        doc = await _get_user_key(q.from_user.id)
        if not doc:
            await _send_no_key(q.message, edit_query=q)
        else:
            await _edit_callback_message(q, await _key_info_text(q.from_user.id, doc), _key_info_markup())
    elif q.data == "key_reset":
        await q.answer()
        doc = await _get_user_key(q.from_user.id)
        if not doc:
            await _send_no_key(q.message, edit_query=q)
        else:
            text = f"⚠️ <b>Confirm Reset Devices?</b>\n\n<code>{doc['key']}</code>"
            await _edit_callback_message(q, text, InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirm", callback_data="key_reset_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="key_reset_cancel"),
            ]]))
    elif q.data == "key_reset_confirm":
        await q.answer("Devices reset successfully.")
        doc = await _get_user_key(q.from_user.id)
        if not doc:
            await _send_no_key(q.message, edit_query=q)
        else:
            await keys_col.update_one({"_id": doc["_id"], "user_id": q.from_user.id}, {"$set": {"devices": []}})
            refreshed = await _get_user_key(q.from_user.id)
            if refreshed:
                await _edit_callback_message(q, "✅ Devices Reset\n\n" + await _key_info_text(q.from_user.id, refreshed), _key_info_markup())
            else:
                await _send_no_key(q.message, edit_query=q)
    elif q.data == "key_reset_cancel":
        await q.answer()
        doc = await _get_user_key(q.from_user.id)
        if not doc:
            await _send_no_key(q.message, edit_query=q)
        else:
            await _edit_callback_message(q, await _key_info_text(q.from_user.id, doc), _key_info_markup())
    elif q.data == "user_getfile":
        text = (
            "🔗 <b>Get a file with Gudu Gaming</b>\n\n"
            "1. Open a Gudu Gaming share link (it starts with <code>https://t.me/</code>).\n"
            "2. Press <b>Start</b> in Telegram if prompted.\n"
            "3. Join any channels shown, then tap <b>Verify Membership</b>.\n"
            "4. Your file will arrive in this chat.\n\n"
            "<i>Files may expire automatically, so save them promptly.</i>\n\n"
            "<i>Any issue? Contact • @subro98 / @gudugaming8069</i>"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="user_home")]])
        await _edit_callback_message(q, text, markup)
    elif q.data == "user_account":
        text = (
            "👤 <b>Your Gudu Gaming Account</b>\n\n"
            f"🆔 Telegram ID: <code>{q.from_user.id}</code>\n"
            f"📛 Name: {q.from_user.first_name}\n\n"
            "Your account is ready to receive shared files."
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="user_home")]])
        await _edit_callback_message(q, text, markup)
    elif q.data == "user_admin":
        if _is_admin(q.from_user.id):
            await admin_panel(update, context)
        else:
            await q.answer("This section is for the bot owner only.", show_alert=True)
    elif q.data == "user_manager":
        if await _is_manager(q.from_user.id):
            await _edit_callback_message(
                q,
                "🛠 <b>AdminGG Upload Panel</b>\n\n"
                "Send a document, photo, video, or audio here. "
                "I'll save it and return a shareable link.\n\n"
                "<i>AdminGG accounts can only upload files and generate links.</i>",
                InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="user_home")]])
            )
        else:
            await q.answer("AdminGG access is for approved managers only.", show_alert=True)
    elif q.data == "user_help":
        text = (
            f"✨ <b>How {BOT_NAME} works</b>\n\n"
            "<b>For recipients</b>\n"
            "① Open the shared Telegram link.\n"
            "② Join every required channel.\n"
            "③ Tap <b>Verify Membership</b>.\n"
            "④ Receive and save your file.\n\n"
            "<b>For the owner</b>\n"
            "Send a supported file to the bot to upload it and generate a share link.\n\n"
            "🔐 Only open links from sources you trust.\n\n"
            "<i>Any issue? Contact • @subro98 / @gudugaming8069</i>"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Get a File", callback_data="user_getfile")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="user_home")],
        ])
        await q.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=markup) if q.message.caption is not None else await _edit_callback_message(q, text, markup)
    elif q.data == "user_home":
        msg = (
            f"☁️ <b>Welcome to {BOT_NAME}, {q.from_user.first_name}!</b>\n\n"
            "<i>File to Link. Made Simple.</i> 🚀\n\n"
            "📤 Uploads become shareable Telegram links.\n"
            "🔗 Open a link to receive a file instantly.\n"
            "🔐 Membership checks protect shared files.\n"
            "⏳ Files are automatically cleaned up.\n\n"
            "Choose an option below to get started.\n\n"
            "<i>Any issue? Contact • @subro98 / @gudugaming8069</i>"
        )
        rows = [
            [InlineKeyboardButton("🔗 Get a File", callback_data="user_getfile"),
             InlineKeyboardButton("📖 How It Works", callback_data="user_help")],
            [InlineKeyboardButton("👤 My Account", callback_data="user_account"),
             InlineKeyboardButton("📢 Official Channel", url=CHANNEL_URL)],
        ]
        if _is_admin(q.from_user.id):
            rows.append([InlineKeyboardButton("👑 Admin Dashboard", callback_data="user_admin")])
        elif await _is_manager(q.from_user.id):
            rows.append([InlineKeyboardButton("🛠 AdminGG", callback_data="user_manager")])
        markup = InlineKeyboardMarkup(rows)
        if q.message.caption is not None:
            await q.edit_message_caption(caption=msg, parse_mode="HTML", reply_markup=markup)
        else:
            await q.edit_message_text(msg, parse_mode="HTML", reply_markup=markup)


async def send_fsub(update, context, channels, token, first_name):
    if token == "KEY":
        msg = (
            "🔑 <b>Get Free Key</b>\n\n"
            "📊 <b>Progress:</b>\n"
            "📌 Join all channels...\n\n"
            "👇 Tap below to unlock"
        )
    else:
        msg = (
            f"🔐 <b>One quick step, {first_name}!</b>\n\n"
            "To unlock this file, please join the channel(s) below.\n"
            "Once you've joined all of them, tap <b>Verify Membership</b>.\n\n"
            "<i>Any issue? Contact • @subro98 / @gudugaming8069</i>"
        )
    btns = []
    for ch in channels:
        invite = ch.get("invite_link")
        title = ch.get("title") or str(ch["_id"])

        # Refresh the Telegram chat title even when an invite URL is already
        # stored. Older records may have saved the numeric channel ID as title.
        try:
            chat = await context.bot.get_chat(ch["_id"])
            title = chat.title or title
            if chat.username:
                invite = f"https://t.me/{chat.username}"
            elif not invite:
                invite, _ = await get_channel_invite(context, ch["_id"])
            await fsub_col.update_one(
                {"_id": ch["_id"]},
                {"$set": {"title": title, "invite_link": invite}},
            )
        except Exception as exc:
            logging.warning("Could not refresh force-sub channel title (%s): %s", ch["_id"], exc)
            if not invite:
                invite, fetched_title = await get_channel_invite(context, ch["_id"])
                if fetched_title and not fetched_title.lstrip("-").isdigit():
                    title = fetched_title
                if invite:
                    await db_add_fsub(ch["_id"], title, invite)

        if invite:
            btns.append([InlineKeyboardButton(f"📢 Join {title}", url=invite)])
    if not btns:
        msg += "\n\n⚠️ A channel invite link is unavailable. Please contact the bot administrator."
    btns.append([InlineKeyboardButton("✅ Verify Membership", callback_data=f"fsub_verify:{token}")])
    btns.append([InlineKeyboardButton("❓ Why am I seeing this?", callback_data="user_help")])
    markup = InlineKeyboardMarkup(btns)
    message = update.effective_message
    if not message:
        return
    try:
        await message.reply_photo(photo=FSUB_PHOTO, caption=msg, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await message.reply_text(msg, parse_mode="HTML", reply_markup=markup)


async def deliver_file(update: Update, context: ContextTypes.DEFAULT_TYPE, token):
    # effective_message works for both /start commands and inline-button callbacks.
    message = update.effective_message
    if not message:
        return
    doc = await db_get_file(token)
    if not doc:
        await message.reply_text(await db_get_msg("NO_FILE"))
        return
    auto_del = await db_get_auto_del()
    caption = (
        (await db_get_msg("FILE_CAPTION"))
        .replace("{file_name}", doc.get("file_name", "File"))
        .replace("{auto_del}", _fmt_del(auto_del))
    )
    ftype, fid = doc["file_type"], doc["file_id"]
    try:
        if ftype == "document":
            sent = await message.reply_document(fid, caption=caption, parse_mode="HTML")
        elif ftype == "video":
            sent = await message.reply_video(fid, caption=caption, parse_mode="HTML")
        elif ftype == "audio":
            sent = await message.reply_audio(fid, caption=caption, parse_mode="HTML")
        elif ftype == "photo":
            sent = await message.reply_photo(fid, caption=caption, parse_mode="HTML")
        else:
            sent = await message.reply_document(fid, caption=caption, parse_mode="HTML")

        chat_id = update.effective_chat.id
        msg_id = sent.message_id

        async def _auto_del():
            await asyncio.sleep(auto_del)
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            try:
                await context.bot.send_message(chat_id, await db_get_msg("FILE_EXPIRED"), parse_mode="HTML")
            except Exception:
                pass

        asyncio.create_task(_auto_del())
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Commands</b>\n\n"
        "/getkey — Get Free Key\n"
        "/mykey — My Key Info\n"
        "/resetkey — Reset Devices (one key can be used on one device)\n"
        "/help — Help\n\n"
        "Any issue? Contact • @subro98 / @gudugaming8069"
    )
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Get Free Key", callback_data="key_get"), InlineKeyboardButton("📋 My Key", callback_data="key_my")],
        [InlineKeyboardButton("🏠 Main Menu", url=f"https://t.me/{BOT_USERNAME}")],
    ]))


async def _edit_callback_message(q, text, reply_markup=None):
    """Edit either a text message or the caption of a photo-based start screen."""
    if q.message and q.message.caption is not None:
        return await q.edit_message_caption(
            caption=text, parse_mode="HTML", reply_markup=reply_markup
        )
    return await q.edit_message_text(
        text, parse_mode="HTML", reply_markup=reply_markup
    )


def _is_admin(uid):
    return uid == OWNER_ID


async def _is_manager(uid):
    if _is_admin(uid):
        return False
    return await managers_col.find_one({"_id": uid}) is not None


async def _can_upload(uid):
    return _is_admin(uid) or await _is_manager(uid)


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    if not _is_admin(update.effective_user.id):
        return
    auto_del = await db_get_auto_del()
    fsub = await db_get_fsub()
    users = await db_user_count()
    files = await db_file_count()
    manager_count = await managers_col.count_documents({})
    text = (
        f"👑 <b>{BOT_NAME} Control Center</b>\n"
        "<i>Manage your bot from one place.</i>\n\n"
        f"👥 <b>{users}</b> users     📦 <b>{files}</b> files\n"
        f"🔒 <b>{len(fsub)}</b> force-sub channels     ⏳ <b>{_fmt_del(auto_del)}</b> auto-delete\n"
        f"🛠 <b>{manager_count}</b> AdminGG managers\n\n"
        "Select a management section:"
    )
    btns = [
        [InlineKeyboardButton("📊 Overview & Stats", callback_data="adm_stats")],
        [InlineKeyboardButton("🛠 Manager Management (AdminGG)", callback_data="adm_managers")],
        [InlineKeyboardButton("🔒 Force Sub Channels", callback_data="adm_listfsub")],
        [InlineKeyboardButton("📝 Edit Messages", callback_data="adm_listmsgs"),
         InlineKeyboardButton("📦 List Files", callback_data="adm_listfiles")],
        [InlineKeyboardButton("➕ Add FSub", callback_data="adm_addfsub_help"),
         InlineKeyboardButton("➖ Remove FSub", callback_data="adm_removefsub_help")],
        [InlineKeyboardButton("⏳ Auto Delete", callback_data="adm_autodel_help"),
         InlineKeyboardButton("📡 Broadcast", callback_data="adm_broadcast_help")],
        [InlineKeyboardButton("🚫 Ban", callback_data="adm_ban_help"),
         InlineKeyboardButton("✅ Unban", callback_data="adm_unban_help")],
        [InlineKeyboardButton("🗑 Delete File", callback_data="adm_delfile_help"),
         InlineKeyboardButton("🍃 Database", callback_data="adm_mongo")],
        [InlineKeyboardButton("🏠 User Menu", callback_data="user_home")],
    ]
    markup = InlineKeyboardMarkup(btns)
    if update.callback_query:
        await _edit_callback_message(update.callback_query, text, markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return
    await q.answer()
    if not _is_admin(q.from_user.id):
        return
    d = q.data

    if d == "adm_managers":
        managers = await managers_col.find({}, {"_id": 1}).to_list(None)
        text = (
            "🛠 <b>AdminGG Manager Management</b>\n\n"
            f"👥 Total managers: <b>{len(managers)}</b>\n\n"
            "AdminGG permissions: upload files and generate share links only. "
            "They cannot access the owner dashboard or manage channels, users, broadcasts, or settings.\n\n"
            "➕ Add manager: <code>/addmanager USER_ID</code>\n"
            "➖ Remove manager: use the button below.\n"
            "📋 List managers: <code>/listmanagers</code>"
        )
        rows = [[InlineKeyboardButton(f"❌ Remove {m['_id']}", callback_data=f"adm_mgr_remove_{m['_id']}")] for m in managers]
        rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="adm_managers")])
        rows.append([InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="adm_back")])
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))

    elif d.startswith("adm_mgr_remove_"):
        try:
            uid = int(d[len("adm_mgr_remove_"):])
        except ValueError:
            await q.answer("Invalid manager ID", show_alert=True)
            return
        exists = await managers_col.find_one({"_id": uid})
        if not exists:
            await q.answer("Manager not found", show_alert=True)
            return
        await q.edit_message_text(
            f"⚠️ <b>Remove AdminGG manager?</b>\n\nUser ID: <code>{uid}</code>\n\nThis user will lose upload and share-link access.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Remove", callback_data=f"adm_mgr_confirm_{uid}"),
                 InlineKeyboardButton("❌ Cancel", callback_data="adm_managers")],
                [InlineKeyboardButton("⬅️ Dashboard", callback_data="adm_back")],
            ])
        )

    elif d.startswith("adm_mgr_confirm_"):
        try:
            uid = int(d[len("adm_mgr_confirm_"):])
        except ValueError:
            await q.answer("Invalid manager ID", show_alert=True)
            return
        result = await managers_col.delete_one({"_id": uid})
        await q.answer("Manager removed" if result.deleted_count else "Manager not found")
        managers = await managers_col.find({}, {"_id": 1}).to_list(None)
        text = "🛠 <b>AdminGG Manager Management</b>\n\n" + (f"👥 Total managers: <b>{len(managers)}</b>\n\n" if managers else "No managers currently added.\n\n")
        text += "➕ Add manager: <code>/addmanager USER_ID</code>\n➖ Remove managers with the buttons below."
        rows = [[InlineKeyboardButton(f"❌ Remove {m['_id']}", callback_data=f"adm_mgr_remove_{m['_id']}")] for m in managers]
        rows += [[InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="adm_back")]]
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))

    elif d == "adm_stats":
        text = (
            f"📊 <b>Stats</b>\n\n"
            f"👥 Users: <b>{await db_user_count()}</b>\n"
            f"📦 Files: <b>{await db_file_count()}</b>\n"
            f"🔒 FSub: <b>{len(await db_get_fsub())}</b>\n"
            f"⏳ Auto Del: <b>{_fmt_del(await db_get_auto_del())}</b>"
        )
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_mongo":
        try:
            await _client.admin.command("ping")
            users = await db_user_count()
            files = await db_file_count()
            text = (
                f"🍃 <b>MongoDB Status</b>\n\n"
                f"✅ <b>Connected</b>\n"
                f"🗄 DB: <code>{DB_NAME}</code>\n"
                f"👥 Users: <b>{users}</b>\n"
                f"📦 Files: <b>{files}</b>\n\n"
                f"<i>All data persists across deployments</i>"
            )
        except Exception as e:
            text = f"🍃 <b>MongoDB Status</b>\n\n❌ <b>Error:</b>\n<code>{e}</code>"
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_listfsub":
        channels = await db_get_fsub()
        text = "📋 <b>Force Sub Channels:</b>\n\n" if channels else "No force sub channels.\n\nUse /addfsub &lt;id1&gt; &lt;id2&gt; ..."
        for ch in channels:
            text += f"• <b>{ch.get('title','?')}</b>\n  <code>{ch['_id']}</code>\n\n"
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_listmsgs":
        text = "📝 <b>Editable Keys:</b>\n\n"
        for key in DEFAULT_MESSAGES:
            text += f"<code>/setmsg {key} text</code>\n"
        text += "\n<b>Placeholders:</b> <code>{first}</code> <code>{auto_del}</code> <code>{file_name}</code> <code>{count}</code>"
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_listfiles" or d.startswith("adm_listfiles_p"):
        page = int(d.split("_p")[1]) if "_p" in d else 0
        per_page = 8
        all_files = await db_list_files(200)
        total = len(all_files)
        chunk = all_files[page * per_page:(page + 1) * per_page]
        if not chunk:
            await q.edit_message_text("No files stored.", reply_markup=_back_btn())
            return
        text = f"📦 <b>Files</b> ({page * per_page + 1}–{min((page + 1) * per_page, total)} of {total})\n\n"
        text += "<i>🔗 = Get Link  🗑 = Delete</i>\n"
        btns = []
        for f in chunk:
            fname = f.get("file_name", "?")
            short = fname[:26] + "…" if len(fname) > 26 else fname
            token = f["_id"]
            link = f"https://t.me/{BOT_USERNAME}?start={token}"
            btns.append([
                InlineKeyboardButton(f"📄 {short}", callback_data=f"adm_fileinfo_{token}"),
                InlineKeyboardButton("🔗", url=link),
                InlineKeyboardButton("🗑", callback_data=f"adm_qdel_{token}|{page}"),
            ])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"adm_listfiles_p{page - 1}"))
        if (page + 1) * per_page < total:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"adm_listfiles_p{page + 1}"))
        if nav:
            btns.append(nav)
        btns.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

    elif d.startswith("adm_fileinfo_"):
        token = d[len("adm_fileinfo_"):]
        doc = await db_get_file(token)
        if not doc:
            await q.edit_message_text("❌ File not found.", reply_markup=_back_btn())
            return
        link = f"https://t.me/{BOT_USERNAME}?start={token}"
        text = (
            f"📄 <b>File Info</b>\n\n"
            f"📦 <b>Name:</b> {doc.get('file_name','?')}\n"
            f"🗂 <b>Token:</b> <code>{token}</code>\n"
            f"🔗 <b>Link:</b>\n<code>{link}</code>"
        )
        btns = [
            [InlineKeyboardButton("🔗 Open Link", url=link)],
            [InlineKeyboardButton("🗑 Delete File", callback_data=f"adm_qdel_{token}|0")],
            [InlineKeyboardButton("⬅️ Back to Files", callback_data="adm_listfiles")],
        ]
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

    elif d.startswith("adm_qdel_"):
        payload = d[len("adm_qdel_"):]
        token, page_str = payload.rsplit("|", 1)
        page = int(page_str)
        doc = await db_get_file(token)
        if doc:
            try:
                await context.bot.delete_message(DB_CHANNEL, doc["message_id"])
            except Exception:
                pass
            await db_del_file(token)
        await q.answer("🗑 Deleted!", show_alert=False)
        per_page = 8
        all_files = await db_list_files(200)
        total = len(all_files)
        chunk = all_files[page * per_page:(page + 1) * per_page]
        if not chunk and page > 0:
            page = max(0, page - 1)
            chunk = all_files[page * per_page:(page + 1) * per_page]
        if not chunk:
            await q.edit_message_text("No files stored.", reply_markup=_back_btn())
            return
        text = f"📦 <b>Files</b> ({page * per_page + 1}–{min((page + 1) * per_page, total)} of {total})\n\n<i>🔗 = Get Link  🗑 = Delete</i>\n"
        btns = []
        for f in chunk:
            fname = f.get("file_name", "?")
            short = fname[:26] + "…" if len(fname) > 26 else fname
            tok = f["_id"]
            link = f"https://t.me/{BOT_USERNAME}?start={tok}"
            btns.append([
                InlineKeyboardButton(f"📄 {short}", callback_data=f"adm_fileinfo_{tok}"),
                InlineKeyboardButton("🔗", url=link),
                InlineKeyboardButton("🗑", callback_data=f"adm_qdel_{tok}|{page}"),
            ])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"adm_listfiles_p{page - 1}"))
        if (page + 1) * per_page < total:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"adm_listfiles_p{page + 1}"))
        if nav:
            btns.append(nav)
        btns.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_back")])
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

    elif d == "adm_addfsub_help":
        await q.edit_message_text(
            "➕ <b>Add Force Sub</b>\n\n"
            "1. Add bot as <b>admin</b> to channel\n"
            "2. Run (supports multiple):\n\n"
            "<code>/addfsub -100xxx</code>\n"
            "<code>/addfsub -100xxx -100yyy -100zzz</code>\n\n"
            "<i>Get ID via @userinfobot</i>",
            parse_mode="HTML", reply_markup=_back_btn()
        )

    elif d == "adm_removefsub_help":
        channels = await db_get_fsub()
        if not channels:
            text = "No channels to remove."
        else:
            text = "➖ <b>Remove FSub — copy & run:</b>\n\n"
            for ch in channels:
                text += f"<code>/removefsub {ch['_id']}</code> — {ch.get('title','?')}\n"
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_autodel_help":
        s = await db_get_auto_del()
        await q.edit_message_text(
            f"⏳ <b>Auto Delete</b>\n\nCurrent: <b>{_fmt_del(s)}</b> ({s}s)\n\n"
            f"<code>/setautodel 300</code> — 5m\n"
            f"<code>/setautodel 3600</code> — 1h\n"
            f"<code>/setautodel 86400</code> — 24h",
            parse_mode="HTML", reply_markup=_back_btn()
        )

    elif d == "adm_broadcast_help":
        await q.edit_message_text(
            f"📡 <b>Broadcast</b>\n\n{await db_user_count()} users will receive it.\n\n"
            "<b>Text:</b> <code>/broadcast Your message</code>\n"
            "<b>Photo/video/document/audio:</b> send the media to this chat, reply to it with <code>/broadcast</code>.\n"
            "Captions and supported Telegram media are copied without a forward header.",
            parse_mode="HTML", reply_markup=_back_btn()
        )

    elif d == "adm_ban_help":
        await q.edit_message_text("🚫 <b>Ban</b>\n\n<code>/ban 123456789</code>", parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_unban_help":
        await q.edit_message_text("✅ <b>Unban</b>\n\n<code>/unban 123456789</code>", parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_delfile_help":
        await q.edit_message_text(
            "🗑 <b>Delete File</b>\n\n<code>/delfile token</code>\n\nDeletes from channel + DB.",
            parse_mode="HTML", reply_markup=_back_btn()
        )

    elif d == "adm_back":
        await admin_panel(update, context)


async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    if not await _can_upload(update.effective_user.id):
        return
    msg = update.effective_message
    if msg.document:
        fid, ftype, fname, fsize = msg.document.file_id, "document", msg.document.file_name or "file", msg.document.file_size or 0
    elif msg.video:
        fid, ftype, fname, fsize = msg.video.file_id, "video", f"video_{msg.video.file_unique_id}.mp4", msg.video.file_size or 0
    elif msg.audio:
        fid, ftype, fname, fsize = msg.audio.file_id, "audio", msg.audio.file_name or "audio.mp3", msg.audio.file_size or 0
    elif msg.photo:
        fid, ftype, fname, fsize = msg.photo[-1].file_id, "photo", "photo.jpg", msg.photo[-1].file_size or 0
    else:
        return
    try:
        if ftype == "document":
            fwd = await context.bot.send_document(chat_id=DB_CHANNEL, document=fid)
        elif ftype == "video":
            fwd = await context.bot.send_video(chat_id=DB_CHANNEL, video=fid)
        elif ftype == "audio":
            fwd = await context.bot.send_audio(chat_id=DB_CHANNEL, audio=fid)
        elif ftype == "photo":
            fwd = await context.bot.send_photo(chat_id=DB_CHANNEL, photo=fid)
        token = _make_token(fname)
        await db_store_file(fid, ftype, fname, fwd.message_id, token)
        link = f"https://t.me/{BOT_USERNAME}?start={token}"
        auto_del = await db_get_auto_del()
        text = (
            "🎉 <b>Upload complete!</b>\n\n"
            f"📄 <b>{fname}</b>\n"
            f"💾 Size: <b>{_fmt_size(fsize)}</b>\n"
            f"⏳ Auto-delete: <b>{_fmt_del(auto_del)}</b>\n\n"
            "🔗 <b>Your share link is ready</b>\n"
            f"<code>{link}</code>\n\n"
            "Share this link with anyone who needs the file."
        )
        await msg.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Open Share Link", url=link)],
            [InlineKeyboardButton("📦 Upload Another File", callback_data="user_home")],
        ]))
    except Exception as e:
        await msg.reply_text(f"❌ Error: {e}")


async def cmd_addmanager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /addmanager <telegram_user_id>\nThe manager must start the bot once before uploading.")
        return
    uid = int(context.args[0])
    if uid == OWNER_ID:
        await update.message.reply_text("The owner already has full admin access.")
        return
    await managers_col.update_one({"_id": uid}, {"$set": {"_id": uid, "added_by": OWNER_ID}}, upsert=True)
    await update.message.reply_text(f"✅ <code>{uid}</code> added as AdminGG manager. They can upload files and receive share links only.", parse_mode="HTML")


async def cmd_removemanager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /removemanager <telegram_user_id>")
        return
    uid = int(context.args[0])
    result = await managers_col.delete_one({"_id": uid})
    await update.message.reply_text("✅ Manager access removed." if result.deleted_count else "That user is not an AdminGG manager.")


async def cmd_listmanagers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    managers = await managers_col.find({}, {"_id": 1}).to_list(None)
    if not managers:
        await update.message.reply_text("No AdminGG managers added yet.")
        return
    lines = [f"• <code>{item['_id']}</code>" for item in managers]
    await update.message.reply_text("🛠 <b>AdminGG Managers</b>\n\n" + "\n".join(lines), parse_mode="HTML")


async def cmd_testfsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnose Force Subscribe channel access and bot admin status (owner only)."""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    channels = await db_get_fsub()
    if not channels:
        await update.message.reply_text("No Force Subscribe channels are configured. Add one with /addfsub <channel_id>.")
        return
    me = await context.bot.get_me()
    lines = ["🔎 <b>Force Subscribe Diagnostic</b>", f"Bot: @{me.username or me.id}", ""]
    for ch in channels:
        ch_id = ch.get("_id")
        try:
            chat = await context.bot.get_chat(ch_id)
            bot_member = await context.bot.get_chat_member(chat_id=ch_id, user_id=me.id)
            status = str(bot_member.status).lower()
            admin_ok = status in ("administrator", "creator", "owner")
            lines.append(
                f"{'✅' if admin_ok else '⚠️'} <b>{chat.title or ch.get('title') or ch_id}</b> "
                f"<code>{ch_id}</code>\nBot status: <code>{status}</code>\n"
                + ("Membership checks should be available." if admin_ok else "Make the bot an administrator in this channel.")
            )
        except Exception as exc:
            logging.exception("Force-sub diagnostic failed for channel %s", ch_id)
            lines.append(
                f"❌ <b>{ch.get('title') or ch_id}</b> <code>{ch_id}</code>\n"
                f"Error: <code>{type(exc).__name__}: {str(exc)[:300]}</code>\n"
                "Check the channel ID and make sure the bot is added as an administrator."
            )
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_addfsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add force-sub channels. An invite URL may be supplied explicitly.

    Usage: /addfsub -1001234567890 [https://t.me/+invite]
           /addfsub -100111 -100222
    """
    if not _is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n/addfsub <channel_id> [invite_link]\n"
            "Example: /addfsub -1001234567890 https://t.me/+YourInviteLink\n"
            "For multiple channels, provide IDs without links: /addfsub -100111 -100222"
        )
        return

    results = []
    # A supplied URL is associated with the first channel ID.
    supplied_link = next((a for a in context.args if a.startswith(("https://t.me/", "http://t.me/"))), None)
    id_args = [a for a in context.args if a != supplied_link]
    for arg in id_args:
        try:
            ch_id = int(arg)
            if ch_id > 0:
                ch_id = int(f"-100{ch_id}")
            invite, title = await get_channel_invite(context, ch_id)
            if supplied_link and len(id_args) == 1:
                invite = supplied_link
            await db_add_fsub(ch_id, title, invite)
            status = "Join button ready" if invite else "Added, but invite unavailable — use /setfsublink"
            results.append(f"✅ <b>{title}</b> <code>{ch_id}</code> — {status}")
        except Exception as e:
            results.append(f"❌ <code>{arg}</code> — {e}")
    await update.message.reply_text("\n".join(results), parse_mode="HTML")


async def cmd_setfsublink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set/replace a force-sub channel's join URL manually."""
    if not _is_admin(update.effective_user.id):
        return
    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: /setfsublink <channel_id> <invite_link>\n"
            "Example: /setfsublink -1001234567890 https://t.me/+YourInviteLink"
        )
        return
    try:
        ch_id = int(context.args[0])
        link = context.args[1]
        if ch_id > 0:
            ch_id = int(f"-100{ch_id}")
        if not link.startswith(("https://t.me/", "http://t.me/")):
            raise ValueError("Invite link must start with https://t.me/ or http://t.me/")
        channels = await db_get_fsub()
        channel = next((c for c in channels if c["_id"] == ch_id), None)
        if not channel:
            await update.message.reply_text("❌ Channel not found. Add it first with /addfsub <channel_id>.")
            return
        await fsub_col.update_one({"_id": ch_id}, {"$set": {"invite_link": link}})
        await update.message.reply_text("✅ Force-sub join link updated. The Join button will use this link.")
    except Exception as e:
        await update.message.reply_text(f"❌ Could not update invite link: {e}")


async def cmd_removefsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /removefsub <channel_id>")
        return
    await db_remove_fsub(int(context.args[0]))
    await update.message.reply_text(f"✅ Removed <code>{context.args[0]}</code>", parse_mode="HTML")


async def cmd_listfsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    channels = await db_get_fsub()
    if not channels:
        await update.message.reply_text("No force sub channels.")
        return
    text = "📋 <b>Force Sub Channels:</b>\n\n"
    for ch in channels:
        text += f"• <b>{ch.get('title','?')}</b>\n  <code>{ch['_id']}</code>\n\n"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    text = (
        f"📊 <b>Stats</b>\n\n"
        f"👥 Users: <b>{await db_user_count()}</b>\n"
        f"📦 Files: <b>{await db_file_count()}</b>\n"
        f"🔒 FSub: <b>{len(await db_get_fsub())}</b>\n"
        f"⏳ Auto Del: <b>{_fmt_del(await db_get_auto_del())}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_mongo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    try:
        await _client.admin.command("ping")
        users = await db_user_count()
        files = await db_file_count()
        text = (
            f"🍃 <b>MongoDB Status</b>\n\n"
            f"✅ <b>Connected</b>\n"
            f"🗄 DB: <code>{DB_NAME}</code>\n"
            f"👥 Users: <b>{users}</b>\n"
            f"📦 Files: <b>{files}</b>\n\n"
            f"<i>Data persists across all deployments</i>"
        )
    except Exception as e:
        text = f"🍃 <b>MongoDB Status</b>\n\n❌ <b>Error:</b>\n<code>{e}</code>"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_setautodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /setautodel <seconds>")
        return
    try:
        val = int(context.args[0])
        if val < 30:
            await update.message.reply_text("❌ Minimum 30s.")
            return
        await db_set_setting("auto_del", val)
        await update.message.reply_text(f"✅ Auto delete: <b>{_fmt_del(val)}</b> ({val}s)", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid number.")


async def cmd_setmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setmsg <KEY> <text>")
        return
    key = context.args[0].upper()
    if key not in DEFAULT_MESSAGES:
        await update.message.reply_text(f"❌ Invalid key. Valid: {', '.join(DEFAULT_MESSAGES.keys())}")
        return
    await db_save_msg(key, " ".join(context.args[1:]))
    await update.message.reply_text(f"✅ <b>{key}</b> updated!", parse_mode="HTML")


async def cmd_listmsgs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    text = "📝 <b>Message Keys:</b>\n\n" + "\n".join(f"• <code>{k}</code>" for k in DEFAULT_MESSAGES)
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast text or copy a replied-to Telegram message/media to all users."""
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return

    source = update.message.reply_to_message
    text = " ".join(context.args).strip()
    if source is None and not text:
        await update.message.reply_text(
            "📡 Broadcast usage:\n"
            "• Text: /broadcast Your message\n"
            "• Media: send a photo/video/document/audio, then reply to it with /broadcast"
        )
        return

    users = await db_all_users()
    sent = failed = 0
    prog = await update.message.reply_text(f"📡 Sending to {len(users)} users...")
    for user in users:
        try:
            if source is not None:
                await context.bot.copy_message(
                    chat_id=user["_id"],
                    from_chat_id=source.chat_id,
                    message_id=source.message_id,
                )
            else:
                await context.bot.send_message(chat_id=user["_id"], text=text)
            sent += 1
        except Exception as exc:
            failed += 1
            logging.debug("Broadcast failed for user %s: %s", user["_id"], exc)
        await asyncio.sleep(0.05)

    await prog.edit_text(
        f"📡 <b>Broadcast complete</b>\n\n✅ Sent: {sent}\n❌ Failed: {failed}",
        parse_mode="HTML",
    )


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    uid = int(context.args[0])
    await db_ban(uid)
    await update.message.reply_text(f"🚫 Banned <code>{uid}</code>", parse_mode="HTML")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    uid = int(context.args[0])
    await db_unban(uid)
    await update.message.reply_text(f"✅ Unbanned <code>{uid}</code>", parse_mode="HTML")


async def cmd_delfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /delfile <token>")
        return
    token = context.args[0]
    doc = await db_get_file(token)
    if not doc:
        await update.message.reply_text("❌ Token not found.")
        return
    try:
        await context.bot.delete_message(DB_CHANNEL, doc["message_id"])
    except Exception:
        pass
    await db_del_file(token)
    await update.message.reply_text(f"🗑 <code>{token}</code> deleted.", parse_mode="HTML")


async def error_handler(update, context):
    err = context.error
    logging.error(f"Update {update} caused error: {err}", exc_info=context.error)


async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Open the Gudu Gaming main menu"),
        BotCommand("help", "Show available commands"),
        BotCommand("getkey", "Generate a free 1-day key"),
        BotCommand("mykey", "View your key information"),
        BotCommand("resetkey", "Reset your key device list"),
    ])


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("getkey", getkey_handler))
    app.add_handler(CommandHandler("mykey", mykey_handler))
    app.add_handler(CommandHandler("resetkey", resetkey_handler))
    app.add_handler(CallbackQueryHandler(user_callback, pattern="^(user_|key_|fsub_verify:)"))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("addmanager", cmd_addmanager))
    app.add_handler(CommandHandler("removemanager", cmd_removemanager))
    app.add_handler(CommandHandler("listmanagers", cmd_listmanagers))
    app.add_handler(CommandHandler("mongo", cmd_mongo))
    app.add_handler(CommandHandler("testfsub", cmd_testfsub))
    app.add_handler(CommandHandler("addfsub", cmd_addfsub))
    app.add_handler(CommandHandler("setfsublink", cmd_setfsublink))
    app.add_handler(CommandHandler("removefsub", cmd_removefsub))
    app.add_handler(CommandHandler("listfsub", cmd_listfsub))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("setautodel", cmd_setautodel))
    app.add_handler(CommandHandler("setmsg", cmd_setmsg))
    app.add_handler(CommandHandler("listmsgs", cmd_listmsgs))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("delfile", cmd_delfile))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_"))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO,
        file_handler,
    ))

    app.add_error_handler(error_handler)
    print(f"🌩️ {BOT_NAME} running...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
