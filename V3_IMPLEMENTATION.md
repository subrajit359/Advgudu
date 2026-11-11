# V3 Implementation Notes

## Included capabilities

- Role-based administration for Super Admin, AdminGG Managers, Members, and Member Bot Managers.
- Individually configurable AdminGG manager permissions.
- Individually configurable Member Bot Manager permissions.
- Owner-scoped management for each Member's connected bots.
- A single **Disconnect / Delete Bot** action for Super Admin and the relevant bot owner.
- A confirmation step before permanent deletion.
- Bot-specific deletion designed to leave unrelated members and bots unaffected.

## Validation status

Python syntax and local static checks were performed on the delivered source. These checks do not replace live testing. Verify role boundaries, permission changes, bot start/stop behaviour, and permanent deletion using a non-production account before deployment.


## Hybrid AI assistant

- Added conversational AI entry points in the main bot and connected Member bots.
- Added permission-filtered actions for summaries, file listing/removal, subscription settings, selected bot settings, broadcasts, user access management, and Super Admin connected-bot oversight.
- Read-only actions can run immediately; changes and destructive operations require explicit confirmation.
- Every action is checked again against the user's current role and permissions when executed.
- Added short per-user request throttling and bounded in-chat context.
- AI service credentials are optional; configure them privately to enable AI responses.
- AI prompts instruct the assistant not to disclose private implementation or configuration details.

The included action set is an initial Hybrid AI release, not unrestricted control of every existing feature. Expand it only by adding a narrowly validated action and matching server-side permission checks.
