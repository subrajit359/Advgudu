# Validation notes — Member Control Center update

## Automated checks performed

- `python -m py_compile main.py network.py` — passed.
- Python AST parsing of `main.py` and `network.py` — passed.
- Confirmed the member-bot runtime registers `/panel` and `/dashboard` commands.
- Confirmed the runtime registers the `netpanel:` callback handler and text-input handler.
- Confirmed owner identity is checked before displaying or executing dashboard actions.
- Confirmed dashboard actions are scoped to the current bot ID, and file deletion is scoped to the bot owner.
- Confirmed platform force-join channels are displayed separately and cannot be removed from the member dashboard.
- Confirmed media broadcast mode is handled before normal file-upload handling.

## Manual staging tests still required

Live Telegram and MongoDB behavior cannot be certified by syntax checks alone. Before production deployment, test with a staging bot and database:

1. Open `/panel` as the registered owner; confirm a non-owner cannot open it.
2. Open each dashboard section and use its Back button.
3. Add and remove a manager; verify the manager can upload but cannot open owner controls.
4. Ban and unban a test user; verify access is blocked and restored for this bot only.
5. Add a personal force-join channel, verify membership gating, and remove it. Confirm platform channels remain enforced.
6. Change welcome text and channel button; verify both appear on `/start`.
7. Send text and media broadcasts; verify delivery counts and that media is not stored as a new file.
8. Delete a recent file; verify the database record and storage-channel message are removed when the main bot has permission.
9. Restart the service and confirm registered member bots start normally.

Do not change the existing Fernet token-encryption key when deploying; doing so would make already-stored encrypted bot tokens unreadable.

## Force-subscribe delivery fix (2026-11-10)

- `_all_required_channels()` now combines platform-wide (`network_global_fsub`) and per-member-bot channels, normalizes channel IDs, and de-duplicates overlapping entries.
- `_check_membership()` now fails closed: only explicit `creator`, `administrator`, and `member` states pass; `left`/`kicked` are blocked; `restricted` passes only when Telegram reports `is_member=True`; API errors and unknown states block delivery.
- Both initial deep-link delivery and the Verify callback call the same membership gate before copying a file.
- The Force Join prompt now makes verification failures explicit and offers a retry button.
- Automated mock checks passed for left/kicked, joined, API-error, and empty-configuration cases.
- `python -m py_compile main.py network.py` passed.

Operational check: run `/listfsubmem` in the main Gudu Gaming bot and confirm the required platform channels are listed. If it reports no channels, no platform-wide Force Subscribe rule is configured in the database, so member bots have nothing global to enforce. The main Gudu Gaming bot must be an administrator in every required channel so Telegram membership checks are authoritative.

## Main-bot Member button update (2026-11-10)

- The main bot `/start` menu now shows `👤 Member` only when the Telegram user owns at least one registered network bot.
- The main-menu home callback applies the same ownership-based visibility rule.
- `👤 Member` opens the Member Control Center, lists only bots whose `owner_id` matches the clicking user, and opens the selected bot's management dashboard.
- Added main-bot file management for each owned bot: list up to 15 newest files and delete a selected file from MongoDB and the storage channel.
- The bot-management menu links to statistics, file management, Force Subscribe, managers, ban/unban, welcome text, channel button, broadcast, auto-delete, restart/stop, and token replacement.
- `python -m py_compile main.py network.py` — passed after this update.

Live Telegram/MongoDB tests are still required; static checks do not prove runtime delivery or API permissions.
