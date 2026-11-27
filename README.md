# trytkaBot

A modular Discord bot built with `discord.py` and SQLite, featuring Gemini-powered chat, automated anti-spam moderation, voice channel time tracking with daily midnight summaries and leaderboards, join-to-create temporary voice channels, reaction-role verification, custom welcome messages, and bulk nickname prefix management.

## Features

* **Gemini AI Chat:** Listens in a configured channel (`AI_CHAT_CHANNEL`) using `google-genai` (`gemini-3.8-flash`), maintains per-channel/per-user conversation history, enforces a per-user cooldown, splits responses over 2,000 characters, and supports `/ai-reset`.
* **Anti-Spam Protection:** Tracks message burst rates, duplicate messages within a sliding window, and mass user/role mentions, purging recent messages from the offender and applying a temporary timeout.
* **Voice & Message Statistics:** Tracks voice session durations (splitting sessions across midnight in `Europe/Warsaw`), persists daily totals and message counts in SQLite (`bot.db`), posts automated `00:00` daily VC summaries, and provides `/stats` and `/leaderboard` slash commands.
* **Temporary Voice Channels:** Converts a designated hub voice channel into on-demand private voice channels inside a target category and cleans up empty temporary channels automatically.
* **Verification & Welcomes:** Creates reaction-based verification embeds (`/verify-create`) that swap roles and remove the user's reaction, alongside customizable join welcome embeds (`/welcome-setup`) supporting `{user}`, `{server}`, and `{count}` placeholders.
* **Nickname Prefix Commands (`/komunizuj`, `/odkomunizuj`):** Adds or removes `Towarzysz` / `Towarzyszka` (based on role matching, defaulting to `dziewczyna`) across a single member or the guild while respecting Discord's 32-character limit and role hierarchy.

## Setup & Installation

1. **Clone the repository and enter the project directory:**
   ```bash
   git clone <repository-url>
   cd <repository-directory>
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install discord.py python-dotenv google-genai
   ```

4. **Configure environment variables:**
   Create a `.env` file in the project root:
   ```env
   DISCORD_TOKEN=your_discord_bot_token
   GEMINI_API_KEY=your_gemini_api_key
   AI_CHAT_CHANNEL=optional_channel_id_for_ai_chat
   ```

5. **Enable required Discord Developer Portal intents:**
   In the Discord Developer Portal under **Bot → Privileged Gateway Intents**, enable:
   * **Server Members Intent** (`intents.members`)
   * **Message Content Intent** (`intents.message_content`)

6. **Run the bot:**
   ```bash
   python bot.py
   ```

## Slash Commands

| Command | Description | Required Permissions |
| :--- | :--- | :--- |
| `/verify-create` | Posts a verification embed and reaction that swaps a "before" role for an "after" role | Manage Roles |
| `/clear` | Bulk-deletes between `1` and `500` messages in the current channel | Manage Messages |
| `/welcome-setup` | Sets the welcome channel and message template (`{user}`, `{server}`, `{count}`) | Manage Server |
| `/stats-setup` | Sets the channel for automated `00:00` (`Europe/Warsaw`) daily VC summaries | Manage Server |
| `/stats` | Displays VC time, VC rank, active VC days, daily average, best day, message count, and join date | None |
| `/leaderboard` | Shows the top 10 users by voice channel time (plus caller rank if outside top 10) | None |
| `/voice-setup` | Configures the hub voice channel and category for temporary voice channels | Manage Channels |
| `/komunizuj` | Prepends `Towarzysz` or `Towarzyszka` to a member's or server members' nicknames | Manage Nicknames |
| `/odkomunizuj` | Removes the `Towarzysz` / `Towarzyszka` prefix from server nicknames | Manage Nicknames |
| `/ai-reset` | Clears your active Gemini chat history in the current channel | None |

## Configuration & Storage

* **SQLite Database (`bot.db`):** Created automatically on startup with tables for `verifications`, `welcome`, `stats_channel`, `voice_daily`, `voice_hub`, `temp_channels`, and `message_counts`.
* **Constants in Source:** Anti-spam thresholds (`SPAM_MSG_LIMIT`, `SPAM_MSG_WINDOW`, `SPAM_DUP_LIMIT`, `SPAM_DUP_WINDOW`, `SPAM_MAX_MENTIONS`, `SPAM_TIMEOUT_MIN`), AI parameters (`GEMINI_MODEL`, `AI_HISTORY_LEN`, `AI_COOLDOWN`, `AI_SYSTEM_PROMPT`), and nickname prefix settings (`COMRADE_ROLE_FEMALE`, `COMRADE_PREFIX_M`, `COMRADE_PREFIX_F`, `COMRADE_SKIP_BOTS`) are defined at the top of the script.
