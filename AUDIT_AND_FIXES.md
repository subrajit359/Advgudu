# Gudu Gaming Bot — audit and fixes

## Changes in this revision

- Fixed the member-bot `/panel` command so both the bot owner and that bot's appointed managers can open the Control Center.
- Fixed the member-bot inline Control Center callback authorization so managers can use operational controls in the bot where they were appointed.
- Fixed pending text actions and media-broadcast authorization so an appointed manager can complete permitted panel actions.
- Added an Auto Delete control to the member-bot Control Center (seconds, 30–86400).
- Kept manager appointment/removal owner-only, including protection against crafted callback data and stale add-manager panel actions.
- Excluded the bot owner and that bot's managers from member-bot user lists, user totals, download totals, and broadcast recipients, including legacy records already present in MongoDB.
- Prevented the owner or another manager from being banned through the member-bot user panel or `/ban` command.
- Added `/addchannel -100XXXXXXXXXX https://t.me/+INVITATION_CODE` for a member bot. It stores the channel against the current bot ID only and checks that the main Gudu bot is an administrator in the channel.
- Kept existing `/addfjoin`, `/listfjoin`, and `/removefjoin` commands and made them available to appointed managers, consistent with the manager's Force Join control access.

## Validation performed

- Python AST parsing and bytecode compilation passed for `main.py`, `network.py`, and `config.py`.
- Static regression assertions passed for manager panel access, manager callback/text authorization, owner-only manager administration, user exclusion, Auto Delete control, and `/addchannel` registration.
- The project ZIP was inspected and all source files were reviewed for handler registration and bot-specific database filters.

## Important limits

This is static/local validation. No live Telegram API, BotFather token, Railway deployment, or production MongoDB credentials were available, so end-to-end behaviour has not been verified against your running bots. Test with one owner, one manager, and one ordinary user in a staging bot before deploying to production. Keep your existing ZIP and database backup.

## Existing design notes to review in a later hardening pass

- `TOKEN_KEY` is embedded in `network.py`. Moving it to an environment variable needs a migration plan so already-encrypted bot tokens remain decryptable.
- Broadcast loops are sequential and have no resumable job state; large bot audiences can take time and may be interrupted by deployment restarts.
- `_check_membership` checks both global and bot-specific channels through the main bot client; ensure the main bot remains an administrator in every configured channel.
- The current manual verification queue requires a verifier to add a member bot to storage before it becomes active; that operational dependency should be documented and monitored.


## Runtime NameError fix (2026-11-11)

- Restored the missing `_bot_privileged_ids(bot_id, owner_id)` helper in `network.py`.
- The helper returns the bot owner plus managers assigned to that specific bot, using `bot_managers_col`.
- Existing callers remain unchanged, preserving user exclusion from statistics/broadcasts and owner/manager ban protection.
- Python compilation passed for every `.py` file in the delivered project. Live Telegram/MongoDB behavior still requires staging verification.

## Account and member-control repair (2026-11-11)

- Restored `_is_bot_manager(bot_id, user_id)`, which was called by the customer-bot Control Center but missing from the supplied source. Its lookup is scoped to both bot ID and Telegram user ID.
- Restored `_normal_bot_user_filter(bot_id, owner_id)`, which was called by both customer-bot account workflows but missing from the supplied source. It excludes the owner and only managers assigned to that specific bot.
- Added a per-bot **My Account** entry to the main bot's Member Bot management screen. Its callback verifies `owner_id` and scopes files, users, downloads, and managers to the selected bot.
- Hardened account/download aggregation to treat null, invalid, and non-numeric `downloads` values as zero rather than allowing malformed legacy values to break the account panel.
- Added the missing callback acknowledgement to the main bot's `user_account` path and escaped the displayed Telegram first name for HTML mode.
- Updated both main-bot and customer-bot error handlers to log the original exception traceback and send a generic user-facing failure notice without exposing internals.
- Kept `_bot_privileged_ids` and all existing call sites; its owner/manager security semantics were not bypassed.

## Validation performed for this repair

- `python -m compileall -q` passed for all supplied Python source files (`main.py`, `network.py`, `config.py`).
- Python AST parsing and compilation passed for each source file.
- Static regression assertions passed for the three manager/privilege/account helper definitions, all account callback registrations/routes, the new Member Bot My Account route, and traceback logging.
- Mock async tests passed for bot-specific manager lookup and owner/manager exclusion from normal-user filters, including cross-bot isolation.
- The runtime dependencies `python-telegram-bot`, `motor`, and `pymongo` are not installed in the local validation environment. Therefore live imports, Telegram callback execution, and MongoDB aggregation execution were not tested here. Live staging verification remains required.
