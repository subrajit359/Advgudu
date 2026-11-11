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
