import asyncio
import logging
import html
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
from network import (
    startup_network_bots, shutdown_network_bots, network_callback,
    network_text_message, cmd_network_fsub_add, cmd_network_fsub_remove,
    cmd_network_fsub_list, network_cancel, cmd_botverifyqueue, cmd_setbotverifier,
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


async def db_active_key_count():
    now = _utcnow()
    return await keys_col.count_documents({
        "status": "active",
        "revoked": {"$ne": True},
        "$or": [{"expires_at": {"$gt": now}}, {"expires_at": {"$exists": False}}],
    })


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
        [InlineKeyboardButton("🤖 Create Your Own File Share Bot", callback_data="network_create")],
    ]
    # Only registered network members see the Member control center.
    if await _db["network_bots"].find_one({"owner_id": user.id}, {"_id": 1}):
        btn.append([InlineKeyboardButton("👤 Member", callback_data="network_member")])
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


async def _prompt_network_create(message, context):
    context.user_data["network_await_token"] = True
    await message.reply_text(
        "🤖 Connect Your Telegram Bot\n\n"
        "Step 1: Open @BotFather.\n"
        "Step 2: Send /newbot and follow the instructions.\n"
        "Step 3: Choose a name and username for your bot.\n"
        "Step 4: Copy the API token provided by BotFather.\n"
        "Step 5: Send the token here to complete setup.\n\n"
        "🔒 Never share your bot token with anyone else.\n"
        "❌ Send /cancel to stop."
    )


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
        if token == "NETWORK_CREATE":
            await q.answer("Membership verified! You can now create your bot.")
            try:
                await q.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await _prompt_network_create(q.message, context)
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
    if q.data == "network_create":
        required_channels = await db_get_fsub()
        if not required_channels:
            await q.answer("Bot creation is temporarily unavailable.", show_alert=True)
            await q.message.reply_text("⚠️ Bot creation requires the administrator to configure the Main Bot Force Subscribe channel list first.")
            return
        not_joined, errors = await check_fsub(q.from_user.id, context)
        if errors:
            await q.answer("Membership check unavailable. Please try again later.", show_alert=True)
            await q.message.reply_text("⚠️ I couldn't verify membership. The main bot must be an administrator in every Force Subscribe channel.")
            return
        if not_joined:
            await q.answer("Join all required channels to create your bot.", show_alert=True)
            await send_fsub(update, context, not_joined, "NETWORK_CREATE", q.from_user.first_name)
            return
        await q.answer("Membership verified!")
        await _prompt_network_create(q.message, context)
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
        await q.answer()
        member_bots = await _db["network_bots"].find({"owner_id": q.from_user.id}).to_list(None)
        bot_ids = [int(b["_id"]) for b in member_bots]
        member_files = await _db["network_files"].count_documents({"bot_id": {"$in": bot_ids}}) if bot_ids else 0
        member_users = await _db["network_users"].count_documents({"bot_id": {"$in": bot_ids}}) if bot_ids else 0
        member_managers = await _db["network_bot_managers"].count_documents({"bot_id": {"$in": bot_ids}}) if bot_ids else 0
        member_downloads = await _db["network_users"].aggregate([{"$match": {"bot_id": {"$in": bot_ids}}}, {"$group": {"_id": None, "n": {"$sum": {"$convert": {
                "input": {"$ifNull": ["$downloads", 0]}, "to": "long",
                "onError": 0, "onNull": 0,
            }}}}}]).to_list(1) if bot_ids else []
        text = (
            "👤 <b>Your Gudu Gaming Account</b>\n\n"
            f"🆔 Telegram ID: <code>{q.from_user.id}</code>\n"
            f"📛 Name: {html.escape(q.from_user.first_name or 'Telegram user')}\n\n"
            f"🤖 Your bots: <b>{len(bot_ids)}</b>\n📦 Files: <b>{member_files}</b>\n"
            f"👥 Users: <b>{member_users}</b>\n📥 Downloads: <b>{member_downloads[0]['n'] if member_downloads else 0}</b>\n"
            f"🛠 Managers: <b>{member_managers}</b>"
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
                "🛠 <b>AdminGG Manager Panel</b>\n\n"
                "Send a document, photo, video, or audio here to save it and get a shareable link.\n\n"
                "You can also view usage statistics and the auto-delete setting.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 Users & Files Stats", callback_data="mg_stats")],
                    [InlineKeyboardButton("⏳ Auto Delete", callback_data="mg_autodel")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="user_home")],
                ])
            )
        else:
            await q.answer("AdminGG access is for approved managers only.", show_alert=True)
    elif q.data == "mg_stats":
        if not await _is_manager(q.from_user.id):
            await q.answer("Manager access only.", show_alert=True)
            return
        text = (
            "📊 <b>AdminGG Usage Stats</b>\n\n"
            f"👥 Users: <b>{await db_user_count()}</b>\n"
            f"📦 Files: <b>{await db_file_count()}</b>\n"
            f"🔑 Active keys: <b>{await db_active_key_count()}</b>"
        )
        await _edit_callback_message(q, text, InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="mg_stats")],
            [InlineKeyboardButton("⬅️ AdminGG Panel", callback_data="user_manager")],
        ]))
    elif q.data == "mg_autodel":
        if not await _is_manager(q.from_user.id):
            await q.answer("Manager access only.", show_alert=True)
            return
        seconds = await db_get_auto_del()
        text = (
            "⏳ <b>Auto Delete Settings</b>\n\n"
            f"Current auto-delete time: <b>{_fmt_del(seconds)}</b> ({seconds}s)\n\n"
            "Choose a duration below to update it. Minimum: 30 seconds."
        )
        await _edit_callback_message(q, text, InlineKeyboardMarkup([
            [InlineKeyboardButton("5 minutes", callback_data="mg_setdel_300"), InlineKeyboardButton("10 minutes", callback_data="mg_setdel_600")],
            [InlineKeyboardButton("1 hour", callback_data="mg_setdel_3600"), InlineKeyboardButton("24 hours", callback_data="mg_setdel_86400")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="mg_autodel")],
            [InlineKeyboardButton("⬅️ AdminGG Panel", callback_data="user_manager")],
        ]))
    elif q.data.startswith("mg_setdel_"):
        if not await _is_manager(q.from_user.id):
            await q.answer("Manager access only.", show_alert=True)
            return
        try:
            seconds = int(q.data.rsplit("_", 1)[1])
            if seconds < 30:
                raise ValueError
            await db_set_setting("auto_del", seconds)
            await q.answer(f"Auto-delete set to {_fmt_del(seconds)}")
        except ValueError:
            await q.answer("Invalid duration.", show_alert=True)
            return
        # Re-render the settings screen with the new value.
        text = f"⏳ <b>Auto Delete Settings</b>\n\nCurrent auto-delete time: <b>{_fmt_del(seconds)}</b> ({seconds}s)\n\nChoose a duration below."
        await _edit_callback_message(q, text, InlineKeyboardMarkup([
            [InlineKeyboardButton("5 minutes", callback_data="mg_setdel_300"), InlineKeyboardButton("10 minutes", callback_data="mg_setdel_600")],
            [InlineKeyboardButton("1 hour", callback_data="mg_setdel_3600"), InlineKeyboardButton("24 hours", callback_data="mg_setdel_86400")],
            [InlineKeyboardButton("⬅️ AdminGG Panel", callback_data="user_manager")],
        ]))
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
            [InlineKeyboardButton("🤖 Create Your Own Bot", callback_data="network_create")],
        ]
        # Show the Member area only to users who own at least one registered bot.
        if await _db["network_bots"].find_one({"owner_id": q.from_user.id}, {"_id": 1}):
            rows.append([InlineKeyboardButton("👤 Member", callback_data="network_member")])
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
    elif token == "NETWORK_CREATE":
        msg = (
            f"🤖 <b>Create Your Own File Share Bot, {first_name}!</b>\n\n"
            "To use this feature, join every official Force Subscribe channel below.\n"
            "After joining all channels, tap <b>Verify Membership</b> to continue bot creation."
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


async def _edit_callback_message(q, text, reply_markup=None, parse_mode="HTML"):
    """Edit text messages or media captions safely from any callback screen."""
    if q.message and q.message.caption is not None:
        return await q.edit_message_caption(
            caption=text, parse_mode=parse_mode, reply_markup=reply_markup
        )
    return await q.edit_message_text(
        text, parse_mode=parse_mode, reply_markup=reply_markup
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
    active_keys = await db_active_key_count()
    network_bots_count = await _db["network_bots"].count_documents({})
    network_members_count = await _db["network_bots"].distinct("owner_id")
    network_member_total = len(network_members_count)
    text = (
        f"👑 <b>{BOT_NAME} Control Center</b>\n"
        "<i>Manage your bot from one place.</i>\n\n"
        f"👥 <b>{users}</b> users     📦 <b>{files}</b> files\n"
        f"🔑 <b>{active_keys}</b> active keys\n"
        f"🔒 <b>{len(fsub)}</b> force-sub channels     ⏳ <b>{_fmt_del(auto_del)}</b> auto-delete\n"
        f"🛠 <b>{manager_count}</b> AdminGG managers\n"
        f"🤖 <b>{network_bots_count}</b> member bots · 👤 <b>{network_member_total}</b> network members\n\n"
        "Select a management section:"
    )
    btns = [
        [InlineKeyboardButton("📊 Overview & Stats", callback_data="adm_stats")],
        [InlineKeyboardButton("🛠 Manager Management (AdminGG)", callback_data="adm_managers")],
        [InlineKeyboardButton("🤖 Bot Verifiers", callback_data="adm_bot_verifiers")],
        [InlineKeyboardButton("👥 Network Members & Bots", callback_data="adm_network_members")],
        [InlineKeyboardButton("📢 Member-Bot Force Join", callback_data="adm_fsubmem_list")],
        [InlineKeyboardButton("🔒 Main-Bot Force Sub", callback_data="adm_listfsub")],
        [InlineKeyboardButton("📝 Edit Messages", callback_data="adm_listmsgs"),
         InlineKeyboardButton("📦 List Files", callback_data="adm_listfiles")],
        [InlineKeyboardButton("➕ Add Main FSub", callback_data="adm_addfsub_help"),
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

    if d == "adm_bot_verifiers":
        verifier_doc = await _db["network_settings"].find_one({"_id": "bot_verifiers"}) or {}
        verifier_ids = list(verifier_doc.get("ids", [None, None]))
        while len(verifier_ids) < 2: verifier_ids.append(None)
        text = (
            "🤖 <b>Bot Verifier Settings</b>\n\n"
            f"Verifier 1: <code>{verifier_ids[0] or 'Not set'}</code>\n"
            f"Verifier 2: <code>{verifier_ids[1] or 'Not set'}</code>\n\n"
            "Set or change a verifier with:\n"
            "<code>/setbotverifier 1 USER_ID</code>\n"
            "<code>/setbotverifier 2 USER_ID</code>\n"
            "Use <code>0</code> as USER_ID to clear a slot.\n\n"
            "Verifiers collect submitted usernames inside the bot using /botverifyqueue. "
            "Users do not need to DM verifiers."
        )
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_fsubmem_list":
        channels = await _db["network_global_fsub"].find().to_list(None)
        text = "📢 <b>Mandatory Force Join for Every Member Bot</b>\n\n"
        if channels:
            text += "\n".join(f"• {c.get('title','Channel')} — <code>{c['_id']}</code>" for c in channels)
        else:
            text += "No mandatory member-bot channels configured yet."
        text += "\n\nAdd: <code>/fsubmem CHANNEL_ID</code>\nRemove: <code>/removefsubmem CHANNEL_ID</code>\nList: <code>/listfsubmem</code>"
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_network_members" or d.startswith("adm_network_members_p"):
        bot_col = _db["network_bots"]
        page = int(d.rsplit("_p", 1)[1]) if "_p" in d else 0
        per_page = 15
        pipeline = [{"$group": {"_id": "$owner_id", "bots": {"$sum": 1}, "usernames": {"$push": "$username"}, "statuses": {"$push": "$status"}}}, {"$sort": {"bots": -1}}, {"$skip": page * per_page}, {"$limit": per_page}]
        members = await bot_col.aggregate(pipeline).to_list(per_page)
        all_member_ids = await bot_col.distinct("owner_id")
        total_members = len(all_member_ids)
        total_bots = await bot_col.count_documents({})
        text = f"👥 <b>Network Members</b>\n\nMembers: <b>{total_members}</b>\nTotal bots: <b>{total_bots}</b>\nPage: <b>{page + 1}/{max(1, (total_members + per_page - 1) // per_page)}</b>\n\n"
        rows = []
        for m in members:
            names = ", ".join("@" + str(x) for x in m.get("usernames", [])[:3])
            text += f"👤 <code>{m['_id']}</code> — <b>{m['bots']} bot(s)</b>\n{names}\n\n"
            rows.append([InlineKeyboardButton(f"👤 {m['_id']} · {m['bots']} bot(s)", callback_data=f"adm_member_{m['_id']}")])
        if not members:
            text += "No network members have registered bots yet."
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"adm_network_members_p{page - 1}"))
        if (page + 1) * per_page < total_members:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm_network_members_p{page + 1}"))
        if nav: rows.append(nav)
        rows.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"adm_network_members_p{page}"), InlineKeyboardButton("⬅️ Dashboard", callback_data="adm_back")])
        await _edit_callback_message(q, text[:3900], parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))

    elif d.startswith("adm_member_") and not (d.startswith("adm_member_suspend_") or d.startswith("adm_member_resume_")):
        try: member_id = int(d[len("adm_member_"):])
        except ValueError:
            await q.message.reply_text("Invalid member."); return
        bots = await _db["network_bots"].find({"owner_id": member_id}).to_list(None)
        if not bots:
            await q.message.reply_text("Member has no registered bots."); return
        text = f"👤 <b>Member:</b> <code>{member_id}</code>\n🤖 Bots: <b>{len(bots)}</b>\n\n"
        for b in bots:
            text += f"• @{b.get('username','unknown')} — {b.get('status','unknown')} (ID <code>{b['_id']}</code>)\n"
        rows = [[InlineKeyboardButton("🚫 Suspend member bots", callback_data=f"adm_member_suspend_{member_id}")], [InlineKeyboardButton("▶️ Reactivate bots", callback_data=f"adm_member_resume_{member_id}")], [InlineKeyboardButton("⬅️ Members", callback_data="adm_network_members")]]
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))

    elif d.startswith("adm_member_suspend_") or d.startswith("adm_member_resume_"):
        suspend = d.startswith("adm_member_suspend_")
        try: member_id = int(d.rsplit("_", 1)[1])
        except ValueError:
            await q.message.reply_text("Invalid member."); return
        bots = await _db["network_bots"].find({"owner_id": member_id}).to_list(None)
        from network import _stop_runtime
        for b in bots:
            if suspend:
                await _stop_runtime(int(b["_id"]))
                await _db["network_bots"].update_one({"_id": b["_id"]}, {"$set": {"status": "stopped_by_admin"}})
            else:
                try:
                    await _db["network_bots"].update_one({"_id": b["_id"]}, {"$set": {"status": "starting"}})
                    from network import _start_runtime
                    await _start_runtime(await _db["network_bots"].find_one({"_id": b["_id"]}))
                except Exception:
                    await _db["network_bots"].update_one({"_id": b["_id"]}, {"$set": {"status": "error"}})
        await _edit_callback_message(q, f"{'🚫 Suspended' if suspend else '▶️ Reactivation attempted'} bots for member <code>{member_id}</code>.\n\nSome bots may remain in error if Telegram rejects their token or permissions.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Members", callback_data="adm_network_members")], [InlineKeyboardButton("🏠 Dashboard", callback_data="adm_back")]]))

    elif d == "adm_managers":
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
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))

    elif d.startswith("adm_mgr_remove_"):
        try:
            uid = int(d[len("adm_mgr_remove_"):])
        except ValueError:
            await q.message.reply_text("Invalid manager ID.")
            return
        exists = await managers_col.find_one({"_id": uid})
        if not exists:
            await q.message.reply_text("Manager not found.")
            return
        await _edit_callback_message(q, 
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
            await q.message.reply_text("Invalid manager ID.")
            return
        result = await managers_col.delete_one({"_id": uid})
        await q.message.reply_text("Manager removed." if result.deleted_count else "Manager not found.")
        managers = await managers_col.find({}, {"_id": 1}).to_list(None)
        text = "🛠 <b>AdminGG Manager Management</b>\n\n" + (f"👥 Total managers: <b>{len(managers)}</b>\n\n" if managers else "No managers currently added.\n\n")
        text += "➕ Add manager: <code>/addmanager USER_ID</code>\n➖ Remove managers with the buttons below."
        rows = [[InlineKeyboardButton(f"❌ Remove {m['_id']}", callback_data=f"adm_mgr_remove_{m['_id']}")] for m in managers]
        rows += [[InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="adm_back")]]
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))

    elif d == "adm_stats":
        text = (
            f"📊 <b>Stats</b>\n\n"
            f"👥 Users: <b>{await db_user_count()}</b>\n"
            f"📦 Files: <b>{await db_file_count()}</b>\n"
            f"🔑 Active Keys: <b>{await db_active_key_count()}</b>\n"
            f"🔒 FSub: <b>{len(await db_get_fsub())}</b>\n"
            f"⏳ Auto Del: <b>{_fmt_del(await db_get_auto_del())}</b>"
        )
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=_back_btn())

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
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_listfsub":
        channels = await db_get_fsub()
        text = "📋 <b>Force Sub Channels:</b>\n\n" if channels else "No force sub channels.\n\nUse /fsub &lt;id1&gt; &lt;id2&gt; ..."
        for ch in channels:
            text += f"• <b>{ch.get('title','?')}</b>\n  <code>{ch['_id']}</code>\n\n"
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_listmsgs":
        text = "📝 <b>Editable Keys:</b>\n\n"
        for key in DEFAULT_MESSAGES:
            text += f"<code>/setmsg {key} text</code>\n"
        text += "\n<b>Placeholders:</b> <code>{first}</code> <code>{auto_del}</code> <code>{file_name}</code> <code>{count}</code>"
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_listfiles" or d.startswith("adm_listfiles_p"):
        page = int(d.split("_p")[1]) if "_p" in d else 0
        per_page = 8
        all_files = await db_list_files(200)
        total = len(all_files)
        chunk = all_files[page * per_page:(page + 1) * per_page]
        if not chunk:
            await _edit_callback_message(q, "No files stored.", reply_markup=_back_btn())
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
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

    elif d.startswith("adm_fileinfo_"):
        token = d[len("adm_fileinfo_"):]
        doc = await db_get_file(token)
        if not doc:
            await _edit_callback_message(q, "❌ File not found.", reply_markup=_back_btn())
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
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

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
        await q.message.reply_text("🗑 File deleted.")
        per_page = 8
        all_files = await db_list_files(200)
        total = len(all_files)
        chunk = all_files[page * per_page:(page + 1) * per_page]
        if not chunk and page > 0:
            page = max(0, page - 1)
            chunk = all_files[page * per_page:(page + 1) * per_page]
        if not chunk:
            await _edit_callback_message(q, "No files stored.", reply_markup=_back_btn())
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
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

    elif d == "adm_addfsub_help":
        await _edit_callback_message(q, 
            "➕ <b>Add Force Sub</b>\n\n"
            "1. Add bot as <b>admin</b> to channel\n"
            "2. Run (supports multiple):\n\n"
            "<code>/fsub -100xxx</code>\n"
            "<code>/fsub -100xxx -100yyy -100zzz</code>\n\n"
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
        await _edit_callback_message(q, text, parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_autodel_help":
        s = await db_get_auto_del()
        await _edit_callback_message(q, 
            f"⏳ <b>Auto Delete</b>\n\nCurrent: <b>{_fmt_del(s)}</b> ({s}s)\n\n"
            f"<code>/setautodel 300</code> — 5m\n"
            f"<code>/setautodel 3600</code> — 1h\n"
            f"<code>/setautodel 86400</code> — 24h",
            parse_mode="HTML", reply_markup=_back_btn()
        )

    elif d == "adm_broadcast_help":
        await _edit_callback_message(q, 
            f"📡 <b>Broadcast</b>\n\n{await db_user_count()} users will receive it.\n\n"
            "<b>Text:</b> <code>/broadcast Your message</code>\n"
            "<b>Photo/video/document/audio:</b> send the media to this chat, reply to it with <code>/broadcast</code>.\n"
            "Captions and supported Telegram media are copied without a forward header.",
            parse_mode="HTML", reply_markup=_back_btn()
        )

    elif d == "adm_ban_help":
        await _edit_callback_message(q, "🚫 <b>Ban</b>\n\n<code>/ban 123456789</code>", parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_unban_help":
        await _edit_callback_message(q, "✅ <b>Unban</b>\n\n<code>/unban 123456789</code>", parse_mode="HTML", reply_markup=_back_btn())

    elif d == "adm_delfile_help":
        await _edit_callback_message(q, 
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
            "Usage:\n/fsub <channel_id> [invite_link]\n"
            "Example: /fsub -1001234567890 https://t.me/+YourInviteLink\n"
            "For multiple channels, provide IDs without links: /fsub -100111 -100222"
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
    if not await _can_upload(update.effective_user.id): return
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
    if err is None:
        return
    logging.error(
        "Update processing failed (%s)", type(err).__name__,
        exc_info=(type(err), err, err.__traceback__),
    )
    # Give the user a useful response without exposing internal exception details.
    try:
        message = update.effective_message if update else None
        if message:
            await message.reply_text("❌ Something went wrong while processing that action. Please try again.")
    except Exception:
        logging.exception("Could not send the update-error notice to the user")


async def post_init(app):
    await startup_network_bots()
    await app.bot.set_my_commands([
        BotCommand("start", "Open the Gudu Gaming main menu"),
        BotCommand("help", "Show available commands"),
        BotCommand("getkey", "Generate a free 1-day key"),
        BotCommand("mykey", "View your key information"),
        BotCommand("resetkey", "Reset your key device list"),
    ])


async def post_shutdown(app):
    await shutdown_network_bots()


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("getkey", getkey_handler))
    app.add_handler(CommandHandler("mykey", mykey_handler))
    app.add_handler(CommandHandler("resetkey", resetkey_handler))
    app.add_handler(CallbackQueryHandler(user_callback, pattern="^(user_|key_|mg_|fsub_verify:|network_create$)"))
    app.add_handler(CallbackQueryHandler(network_callback, pattern=r"^network_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, network_text_message), group=0)
    app.add_handler(CommandHandler("cancel", network_cancel))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("addmanager", cmd_addmanager))
    app.add_handler(CommandHandler("removemanager", cmd_removemanager))
    app.add_handler(CommandHandler("listmanagers", cmd_listmanagers))
    app.add_handler(CommandHandler("mongo", cmd_mongo))
    app.add_handler(CommandHandler("testfsub", cmd_testfsub))
    app.add_handler(CommandHandler("networkfsubadd", cmd_network_fsub_add))
    app.add_handler(CommandHandler("networkfsubremove", cmd_network_fsub_remove))
    app.add_handler(CommandHandler("networkfsublist", cmd_network_fsub_list))
    app.add_handler(CommandHandler("fsubmem", cmd_network_fsub_add))
    app.add_handler(CommandHandler("removefsubmem", cmd_network_fsub_remove))
    app.add_handler(CommandHandler("listfsubmem", cmd_network_fsub_list))
    app.add_handler(CommandHandler("botverifyqueue", cmd_botverifyqueue))
    app.add_handler(CommandHandler("setbotverifier", cmd_setbotverifier))
    app.add_handler(CommandHandler("fsub", cmd_addfsub))
    app.add_handler(CommandHandler("addfsub", cmd_addfsub))  # backward-compatible alias
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
