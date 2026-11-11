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
