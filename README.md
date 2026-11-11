# Gudu Gaming Bot

A Telegram file-sharing and multi-bot management platform. Create shareable links, manage access, configure bot behaviour, and oversee connected bots from role-based dashboards.

## Features

- Share files through unique links
- Configurable access requirements
- Automatic cleanup of delivered messages
- File and user management tools
- Broadcast and editable bot messages
- Separate owner and manager dashboards
- Individually configurable manager permissions
- Central administration for connected bots
- A single **Disconnect / Delete Bot** action with confirmation

## Roles

- **Super Admin:** oversees the platform and connected bots.
- **AdminGG Manager:** receives only the individual permissions granted by Super Admin.
- **Member:** manages their own connected bot or bots.
- **Member Bot Manager:** manages only the bot(s) and actions assigned by its owner.

## Deployment

The project includes its deployment files and environment template. Configure the required private settings in your hosting provider before starting the service. Keep all credentials and private configuration values confidential; never publish them in screenshots, support messages, or public repositories.

## Safety

The **Disconnect / Delete Bot** action is permanent. The operator must confirm before it runs. It removes the selected bot and its associated platform information. It does not affect other members' bots.

## Important

Keep a private backup before making production changes. Test updates with a non-production bot before deploying them to your live service.
