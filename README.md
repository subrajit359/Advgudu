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
