# V3 Role Dashboard and Authority Update

## Implemented in this revision

### Platform Admin
- Admin can open an individual customer-bot control screen from the Network Members dashboard.
- Individual customer bots can be started, stopped, and restarted from the central dashboard.
- The screen displays the stored status and whether a runtime is currently present.
- Existing member-wide suspend/reactivate actions remain available.

### AdminGG
- Admin can appoint/remove AdminGG managers using the existing commands and dashboard.
- Admin dashboard now opens a per-manager permission editor with immediate toggle updates.
- Supported delegated permissions in this revision: upload files, view statistics, and change main-bot auto-delete.
- Upload, statistics, and auto-delete entry points enforce their corresponding permissions.
- Newly appointed AdminGG managers start with upload permission only.

### Member / customer-bot owner
- The Member dashboard manager list now opens a per-manager permission editor.
- Multiple managers remain supported independently for each customer bot.
- Supported manager permissions: upload files, manage files, view users, ban/unban users, manage Force Join, edit welcome text, manage channel button, broadcast, change auto-delete, and view statistics.
- Permission changes are saved in the bot-specific manager record and take effect immediately at the panel/upload authorization points.
- Newly appointed managers start with upload permission only.
- The customer-bot `/panel` also exposes a manager permission editor for owners and filters manager dashboard buttons according to assigned permissions.

## Compatibility and security
- Existing MongoDB collection names and existing records are retained.
- Legacy customer-bot manager records without a `permissions` field retain their previous broad operational access; opening and changing their permissions in the UI persists an explicit permission map.
- Legacy AdminGG manager records without a permission map retain upload-only access.
- Member manager lookups remain scoped by both `bot_id` and `user_id`.
- AdminGG permissions and customer-bot manager permissions are stored in separate collections and cannot grant access across those role domains.

## Validation performed
- `python -m py_compile main.py network.py config.py` passed.
- AST parsing of the three Python modules passed.
- Live Telegram API, MongoDB, storage-channel, and Railway runtime tests were not run in this environment.

## Notes
- AdminGG's permission editor currently exposes the main-bot capabilities implemented and enforced in this revision (upload, statistics, auto-delete). Additional main-bot domains should be added only together with their corresponding handler-level authorization checks.
- Telegram callback menus are not a substitute for authorization: the sensitive callback and message paths added here re-check permissions server-side.


## Recheck fix (2026-11-12)

- Fixed AdminGG permission-toggle callback parsing. Permission keys contain underscores (for example `upload_files`, `view_stats`, `manage_autodel`); the previous parser truncated the key at the first underscore, preventing toggles from being applied.
- The callback now separates the numeric manager ID from the complete permission key and rejects malformed/unknown keys with an alert.
- Rechecked member-manager toggle callbacks: their colon-delimited parser preserves underscore-containing permission keys and scopes updates by both `bot_id` and `user_id`.
- Recompiled all Python source files and ran static callback parsing assertions. Live Telegram/MongoDB tests remain required.
