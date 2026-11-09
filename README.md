# ☁️ Gudu Gaming Bot

A powerful Telegram **File-to-Link** bot. Upload files to the bot → get a unique shareable link → users receive the file, which auto-deletes after a timer. Built for Telegram channels to distribute files securely.

**Features:**
- 🔗 File → Unique link system
- 🔒 Force Subscribe gate (multi-channel)
- ⏳ Auto-delete after configurable timer
- 📦 Paginated file browser with inline get-link & delete buttons
- 📊 Admin panel via inline buttons (`/admin`)
- 🍃 MongoDB powered — data persists across any host/redeploy
- 📡 Broadcast to all users
- 🚫 Ban / unban users
- ✏️ All bot messages editable live via commands
- 🌐 Railway / VPS / Windows ready

---

## 📁 Project Structure

```
nexdrop/
├── .env.example      ← copy to .env and fill your values
├── config.py         ← reads .env, don't touch
├── main.py           ← bot logic, don't touch
├── requirements.txt
├── railway.toml      ← Railway deploy config
└── .gitignore
```

---

## ⚙️ Step 1 — Configure

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```env
BOT_TOKEN=your_bot_token_here
BOT_USERNAME=your_bot_username
BOT_NAME=Gudu Gaming
CHANNEL_BTN=📢 Join My Channel
CHANNEL_URL=https://t.me/your_channel_username
OWNER_ID=your_telegram_user_id

DB_URI=mongodb+srv://username:password@cluster.mongodb.net/?appName=YourApp
DB_NAME=nexdrop
DB_CHANNEL=-100your_channel_id

AUTO_DEL=300

START_PHOTO=https://your-start-image-url.jpg
FSUB_PHOTO=https://your-fsub-image-url.jpg
```

### Variable Guide

| Variable | Description | Example |
|---|---|---|
| `BOT_TOKEN` | From @BotFather | `123456:ABCdef...` |
| `BOT_USERNAME` | Bot username without @ | `myfilebot` |
| `BOT_NAME` | Bot display name | `Gudu Gaming` |
| `CHANNEL_BTN` | Start screen button text | `📢 Join My Channel` |
| `CHANNEL_URL` | Start screen button link | `https://t.me/mychannel` |
| `OWNER_ID` | Your Telegram user ID | `123456789` |
| `DB_URI` | MongoDB Atlas connection string | `mongodb+srv://...` |
| `DB_NAME` | Database name | `nexdrop` |
| `DB_CHANNEL` | Private storage channel ID | `-1001234567890` |
| `AUTO_DEL` | Auto-delete timer in seconds | `300` = 5min, `3600` = 1h |
| `START_PHOTO` | Photo URL for /start message | direct image link |
| `FSUB_PHOTO` | Photo URL for force-sub screen | direct image link |

### How to get each value

**BOT_TOKEN** — Message @BotFather → `/newbot` → copy the token

**OWNER_ID** — Forward any of your messages to @userinfobot → copy the `id`

**DB_URI** — [MongoDB Atlas](https://cloud.mongodb.com) → your cluster → Connect → Drivers → copy string → replace `<password>`

**DB_CHANNEL** — Create a private channel → forward a message from it to @userinfobot → get the number → add `-100` prefix (e.g. `1234567890` → `-1001234567890`)

**AUTO_DEL:**
```
300   = 5 minutes
3600  = 1 hour
86400 = 24 hours
```

---

## 🚀 Step 2 — Install & Run

```bash
pip install -r requirements.txt
python main.py
```

---

## 📋 Step 3 — Setup Checklist

- [ ] Create a **private Telegram channel** for file storage
- [ ] Add bot as **Admin** in that channel (Post Messages permission)
- [ ] Set `DB_CHANNEL` in `.env`
- [ ] Run the bot
- [ ] Add force-sub channels via `/addfsub`
- [ ] Bot must be **Admin** in all force-sub channels (Invite Users permission)

---

## 🛠 Admin Commands

| Command | Description |
|---|---|
| `/admin` | Admin panel with inline buttons |
| `/addfsub -100xxx -100yyy` | Add force-sub channels (multiple at once) |
| `/removefsub -100xxx` | Remove a force-sub channel |
| `/listfsub` | List active force-sub channels |
| `/setautodel 3600` | Change auto-delete timer |
| `/setmsg KEY text` | Edit bot messages live |
| `/listmsgs` | Show all editable message keys |
| `/broadcast text` | Send to all users |
| `/stats` | Users, files, FSub count |
| `/mongo` | MongoDB connection status |
| `/ban 123456789` | Ban a user |
| `/unban 123456789` | Unban a user |
| `/delfile token` | Delete file from DB + channel |

---

## 📝 Editable Messages

Use `/setmsg KEY text` to customize any message live without restart:

| Key | Shown when | Placeholders |
|---|---|---|
| `START` | User sends /start | — |
| `FSUB` | Force-sub gate | `{first}` |
| `FILE_CAPTION` | File delivered to user | `{file_name}`, `{auto_del}` |
| `FILE_EXPIRED` | After file auto-deleted | — |
| `NO_FILE` | Invalid/expired link | — |
| `BANNED` | Banned user tries bot | — |
| `BROADCAST_DONE` | Broadcast complete | `{count}` |

---

## 📦 File Browser

`/admin` → **List Files** — paginated browser:
- **📄 Name** — tap for full info + copyable link
- **🔗** — opens share link directly
- **🗑** — deletes from channel + DB instantly

Links never expire unless manually deleted. Safe even if you clear bot chat history.

---

## 🌐 Deploy on Railway

1. Push this repo to GitHub (`.env` is gitignored — safe)
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Go to **Variables** tab → add all your `.env` vars
4. Deploy — `railway.toml` handles the rest

---

## 🌐 Deploy on VPS

```bash
git clone https://github.com/yourusername/nexdrop
cd nexdrop
cp .env.example .env
nano .env   # fill your values
pip install -r requirements.txt
python main.py
```

**Keep running after disconnect:**
```bash
screen -S nexdrop
python main.py
# Ctrl+A then D to detach
```

---

## 🗄 Data Persistence

All data lives in MongoDB — switch host, redeploy, restart — nothing lost:
- Force-sub channels + invite links
- Custom messages
- Auto-delete timer
- All file tokens + links
- Users + banned list

---

## 📜 License

MIT — free to use, modify, and distribute.

---



## AdminGG manager role

The bot owner (`OWNER_ID`) can assign limited upload managers:

- `/addmanager <telegram_user_id>` — grant AdminGG access.
- `/removemanager <telegram_user_id>` — revoke access.
- `/listmanagers` — list current managers.

Managers see an **AdminGG** button and can upload supported documents, photos, videos, and audio. The bot stores each upload and returns its share link. Managers cannot access the owner dashboard, change settings, manage force-sub channels, broadcast, ban users, or delete files. Manager assignments are stored in MongoDB and persist across redeployments.

## 🤖 Gudu Gaming Custom Bot Network (new)

Users can connect their own Telegram file-sharing bot from the main menu using **Create Your Own File Share Bot**. Their custom bot only accepts owner uploads and serves generated share links. Owners manage statistics, owner Force Subscribe channels, auto-delete duration, restart/stop, and token replacement from **My Bots** in the main Gudu Gaming bot.

### Required Railway variables

- `NETWORK_STORAGE_CHANNEL_ID=3959825660` (the code normalizes this raw channel ID to `-1003959825660`; if you already have the full `-100...` ID, set that instead).
- Bot-token encryption uses a Fernet key hard-coded in `network.py` as `TOKEN_KEY`. Keep that exact key secret and unchanged; changing or losing it prevents decrypting previously stored bot tokens.

### Storage channel setup

Create/use the dedicated private Gudu Gaming Network Storage channel. Each custom bot must be added as an administrator with permission to post messages. The custom bot forwards uploaded messages into this channel; the file payload stays on Telegram and MongoDB stores the storage message ID and ownership metadata. The channel ID supplied for this project is `3959825660` (normalized internally to `-1003959825660`).

The main Gudu Gaming bot must be an administrator in any channel used for membership verification. The platform owner can configure mandatory Gudu Gaming channels separately using `/networkfsubadd CHANNEL_ID`, `/networkfsublist`, and `/networkfsubremove CHANNEL_ID`. These mandatory channels are stored separately and cannot be removed by custom-bot owners. Each custom-bot owner can add their own channels from My Bots; the main Gudu Gaming bot must also be admin in those channels so it can verify membership.

### File size note

The platform enforces a maximum of 2 GB per upload. Uploads are forwarded server-side from the owner's custom bot to the storage channel, avoiding a download/re-upload through Railway. Telegram/Bot API behavior for very large files can vary; test a large file on the deployed bots before advertising guaranteed 2 GB delivery. Protected-content messages cannot be forwarded.

### Security and isolation

- Bot tokens are encrypted at rest with Fernet; the encryption key is hard-coded as `TOKEN_KEY` in `network.py`.
- Telegram `getMe` validates each token; the Telegram bot ID is the unique ownership key, so one bot cannot be registered to multiple Gudu Gaming accounts.
- Token replacement only accepts a new token for the same Telegram bot ID.
- Each file record is scoped to its bot ID and owner ID.
- Each custom bot runs as a polling application within the shared Python service.
- Custom bots are separate from the original Gudu Gaming bot's files, keys, settings, and AdminGG data.

### Important operational limits

Every connected custom bot uses its own Telegram long-polling connection and must be an admin in the network storage channel. Railway resource use grows with the number of connected bots; monitor CPU, memory, and Telegram API limits as the network grows. The auto-delete setting removes the delivered copy from a recipient's chat; it does not delete the source message in the private storage channel.

## 🤖 Custom Bot Verification Queue

After a customer submits a valid BotFather token, the main bot asks them to submit their bot username into the Gudu Gaming bot. The username is stored in a shared verification queue; customers do not need to DM either verifier. The owner configures up to two verifier Telegram user IDs from the `/admin` → **Bot Verifiers** section, using `/setbotverifier 1 USER_ID` and `/setbotverifier 2 USER_ID`. Configured verifiers run `/botverifyqueue` in the main bot to collect pending usernames, then manually add each customer bot to the private storage channel with **Post Messages** permission. The customer taps **Continue Verification** to run the final access/admin/permission checks and start their bot.


## Member Bot Owner Features (updated)

A Gudu Network member is the Telegram account that registered and owns one or more custom bots. Each member bot has isolated users, files, managers, bans, force-join channels, welcome text, and channel settings.

### Owner commands inside each member bot

| Command | Purpose |
|---|---|
| `/myaccount` | Total bots, files, users, downloads, and managers across the member's bots |
| `/addmanager USER_ID` | Add a manager to this bot |
| `/removemanager USER_ID` | Remove a manager |
| `/listmanagers` | List this bot's managers |
| `/ban USER_ID` / `/unban USER_ID` | Block or restore a user for this bot only |
| `/addfjoin CHANNEL_ID` | Add an owner-managed force-join channel |
| `/listfjoin` | List both Gudu-mandated and owner-managed channels |
| `/removefjoin CHANNEL_ID` | Remove an owner-managed channel only |
| `/setwelcome TEXT` | Customize the bot's welcome message |
| `/setchannel URL [Button text]` | Add a channel button to `/start` |
| `/broadcast TEXT` | Broadcast text to users who have started the bot |
| `/broadcast` (reply to media) | Copy a media/message broadcast to users |

Only the bot owner and explicitly-added managers can upload files. All other users can only receive files through share links. A ban applies to that member bot only. Managers can upload but cannot change owner settings or manage other users.

### Gudu admin controls for member bots

- `/fsubmem CHANNEL_ID` — add a mandatory force-join channel to every member bot.
- `/listfsubmem` — list mandatory member-bot channels.
- `/removefsubmem CHANNEL_ID` — remove a mandatory channel.

These platform channels appear in every member bot's `/listfjoin` output and are checked even when the member has not added any personal force-join channel. Member owners cannot remove platform channels. There is currently no per-member channel limit.

The main admin dashboard also includes **Network Members & Bots**, showing member IDs, bot counts, individual bot statuses, and suspend/reactivate controls.


## Member Bot Owner Features (updated)

A Gudu Network member is the Telegram account that registered and owns one or more custom bots. Each member bot has isolated users, files, managers, bans, force-join channels, welcome text, and channel settings.

### Owner commands inside each member bot

| Command | Purpose |
|---|---|
| `/myaccount` | Total bots, files, users, downloads, and managers across the member's bots |
| `/addmanager USER_ID` | Add a manager to this bot |
| `/removemanager USER_ID` | Remove a manager |
| `/listmanagers` | List this bot's managers |
| `/ban USER_ID` / `/unban USER_ID` | Block or restore a user for this bot only |
| `/addfjoin CHANNEL_ID` | Add an owner-managed force-join channel |
| `/listfjoin` | List both Gudu-mandated and owner-managed channels |
| `/removefjoin CHANNEL_ID` | Remove an owner-managed channel only |
| `/setwelcome TEXT` | Customize the bot's welcome message |
| `/setchannel URL [Button text]` | Add a channel button to `/start` |
| `/broadcast TEXT` | Broadcast text to users who have started the bot |
| `/broadcast` (reply to media) | Copy a media/message broadcast to users |

Only the bot owner and explicitly-added managers can upload files. All other users can only receive files through share links. A ban applies to that member bot only. Managers can upload but cannot change owner settings or manage other users.

### Gudu admin controls for member bots

- `/fsubmem CHANNEL_ID` — add a mandatory force-join channel to every member bot.
- `/listfsubmem` — list mandatory member-bot channels.
- `/removefsubmem CHANNEL_ID` — remove a mandatory channel.

These platform channels appear in every member bot's `/listfjoin` output and are checked even when the member has not added any personal force-join channel. Member owners cannot remove platform channels. There is currently no per-member channel limit.

The main admin dashboard also includes **Network Members & Bots**, showing member IDs, bot counts, individual bot statuses, and suspend/reactivate controls.
