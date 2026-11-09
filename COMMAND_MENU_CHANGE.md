# Member bot command menu change

- The Telegram command menu for each member bot now exposes only `/start`.
- Owner tools remain available through the inline Control Center opened from the owner's `/start` screen.
- `/help` no longer prints the private owner command list.
- Existing command handlers and authorization checks are retained; this change hides the long command list from the Telegram menu, it does not remove the underlying owner functionality.

Validation performed: Python bytecode compilation of all project `.py` files.
Live Telegram menu refresh should be confirmed after deploying/restarting the member bot runtimes.
