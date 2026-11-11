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
- Central administration for connected bots, including AI-assisted bot oversight
- Optional Gudu AI chat for natural-language help and permitted management actions
- A single **Disconnect / Delete Bot** action with confirmation

## Roles

- **Super Admin:** oversees the platform and connected bots.
- **AdminGG Manager:** receives only the individual permissions granted by Super Admin.
- **Member:** manages their own connected bot or bots.
- **Member Bot Manager:** manages only the bot(s) and actions assigned by its owner.

## Gudu AI

Users can start an AI conversation with `/ai` and pause it with `/aistop`. The assistant can answer questions and perform permitted tasks, including viewing summaries, finding files, updating selected settings, managing subscription requirements, and managing user access. Read-only requests can run directly; file removal, broadcasts, user access changes, and settings updates require an explicit confirmation. Managers can only use actions allowed by their assigned permissions. AI access requires an AI service credential and can be configured through the private deployment settings. Do not send passwords, access tokens, or other secrets in AI conversations.

## Deployment

The project includes its deployment files and environment template. Configure the required private settings in your hosting provider before starting the service. Keep all credentials and private configuration values confidential; never publish them in screenshots, support messages, or public repositories.

## Safety

The **Disconnect / Delete Bot** action is permanent. The operator must confirm before it runs. It removes the selected bot and its associated platform information. It does not affect other members' bots.

## Important

Keep a private backup before making production changes. Test updates with a non-production bot before deploying them to your live service.
