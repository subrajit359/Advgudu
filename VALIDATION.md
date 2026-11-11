# Validation Checklist

## Before deployment

1. Confirm the project starts successfully in a test environment.
2. Confirm Super Admin can manage all connected bots.
3. Confirm each AdminGG Manager sees and can use only individually granted permissions.
4. Change a manager permission and verify the change takes effect immediately.
5. Confirm a Member can manage only bots they own.
6. Confirm a Member Bot Manager can access only assigned bots and granted actions.
7. Confirm one bot owner's data and activity are isolated from other bot owners.
8. Test **Disconnect / Delete Bot** as Super Admin and as the relevant bot owner.
9. Confirm the confirmation screen prevents accidental deletion.
10. Confirm deletion affects only the selected bot and cannot be triggered by an unauthorized user.
11. Test file sharing, access checks, broadcasts, and automatic cleanup.

## Local checks

Python syntax compilation and static callback checks were performed. Live service and production integration tests were not performed in this environment. Run the checklist above with test accounts before deploying to production.


## Gudu AI checks

1. Set the AI service credential and restart the service.
2. Start AI chat with `/ai` in the main bot and in a connected Member bot.
3. Confirm ordinary users can chat but cannot perform management actions.
4. Confirm each manager can perform only actions covered by their current permissions.
5. Confirm read-only requests run without an approval prompt.
6. Confirm file removal, broadcasts, access changes, and configuration changes require explicit confirmation.
7. Revoke a manager permission, then confirm the same action is denied immediately, including at confirmation time.
8. Use `/aistop` and confirm AI chat pauses.
9. Test unavailable AI service responses and request rate limiting.

Local AI helper unit tests cover missing credentials, allow-listed actions, unknown-action rejection, privacy instructions, and request throttling. External AI service and live Telegram integration were not tested in this environment.
