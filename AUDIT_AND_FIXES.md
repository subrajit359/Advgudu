# Audit and Fix Notes

- Reviewed manager permission callback handling.
- Corrected parsing for permission names containing underscores.
- Checked that manager actions are scoped to the relevant bot and manager.
- Reviewed owner and Super Admin access paths for bot management.
- Kept permanent bot removal behind an explicit confirmation step.
- Performed local Python compilation and static checks.

## Test limitation

Static checks cannot certify live Telegram behaviour or a production deployment. Complete a staging test with a Super Admin, a bot owner, a manager with limited permissions, and an ordinary user before updating the live service.
