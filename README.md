# TAW DayZ Division Ticket Bot — Quick Setup

Discord ticket bot with panels, per-type tickets, intake forms, HTML transcripts, `/status`, `/add`, per-user limits, and a safe **test mode** with production whitelist.

---

## 1. Requirements

- Python 3.10+ (3.11/3.12 also supported)
- Install dependencies:
  ```bash
  pip install -U discord.py
  ```
- **Bot permissions in your server:**
  - Manage Channels
  - Manage Threads
  - Read Message History
  - Send Messages
  - Embed Links
  - Attach Files
  - Manage Messages (cleanup old panels)
  - Create Private Threads

- *(Recommended)* Enable **Server Members Intent** in the Discord Developer Portal to auto-add support to the private notes thread.

---

## 2. Files & Folders

- `bot.py`, `ticket_manager.py`, `config_commands.py`
- `main_config.json` (global config)
- `configs/` (per-server JSON; created from `configs/default.json`)
- `open_tickets.json` (runtime)
- `transcripts/` (HTML transcripts)

---

## 3. Configure `main_config.json`

Fill in your bot token and (optionally) test/production behavior:

```json
{
  "token": "YOUR_BOT_TOKEN_HERE",
  "bot_master_ids": [],
  "test_mode": {
    "enabled": false,
    "guild_ids": [],
    "prod_override_ids": []
  }
}
```

- Set `enabled` to `true` and add your test server to `guild_ids` while setting up.
- Add real servers to `prod_override_ids` when you want them live.

---

## 4. Start the Bot

**Windows example (`start ticketbot.bat`):**
```bat
@echo off
cd /d %~dp0
py -3 bot.py
pause
```

**Or run directly:**
```bash
python bot.py
```

On startup you should see:
- “Commands/views registered”
- “Synced commands globally”
- “Watcher started”

---

## 5. Initial Server Setup (in Discord)

Run these as a server Administrator:

**Create the base config for this server:**
```
/setup panel_channel:#channel ticket_category:<category> log_channel:#channel [support_role:@role]
```

**Post the panel:**
```
/panel
```

**(Optional) Limit how many open tickets a non-staff user can have:**
```
/editconfig key:user_limit_max_open value:1
```

**View current config:**
```
/viewconfig
```

---

## 6. Add Ticket Types

You can edit `configs/<guild_id>.json` directly or use commands.

**Minimal JSON for a type:**
```json
{
  "label": "Support Ticket",
  "description": "General help",
  "emoji": "🛠️",
  "category_id": null,
  "enabled": true,
  "support_role_ids": [],
  "intake_form": { "enabled": false, "questions": [] },
  "no_mention_role_ids": []
}
```

- Set `category_id` to a real category ID (or `null` to create tickets outside any category).
- Add multiple entries to the `"ticket_types"` array.
- Save the file; the watcher will refresh the panel automatically (old panels are cleaned). You can also re-run `/panel`.

**Intake form (optional):**
```
/intake enable ticket_type:<label> enabled:true
/intake addquestion ticket_type:<label> label:"Your SteamID?" style:short required:true
/intake view ticket_type:<label>
/intake removequestion ticket_type:<label> index:1
/intake clear ticket_type:<label>
```

---

## 7. Daily Use

- Open a ticket via the panel dropdown (only enabled types show).
- The first ticket message (with Close button) is pinned.
- A private staff “notes” thread is created automatically.

**Update status:**
```
/status
```
- Options: Approved / Waiting for Response / Issue (emoji added to name, topic updated; rename has cooldown)

**Add a participant:**
```
/add
```
- Add a user OR a role (never edits @everyone)

**Close flow (button):**
- Save transcript & delete OR Delete without transcript.
- Ticket opener can only Save & delete.
- If the opener isn’t staff, they’re removed from the channel on close.

**Transcripts:**
- Pretty HTML saved under `transcripts/` and posted to the log channel.
- Auto-prunes: keeps 50 newest, deletes 20 oldest.

---

## 8. Test Mode Behavior

If `test_mode.enabled = true`:

- Servers in `guild_ids` behave as test:
  - Channel names use `testticket-...`
  - Log posts are tagged as (test)
- Servers in `prod_override_ids` behave normally (full production) even while test mode is on.
- All other servers see a polite “disabled in test mode” message.

---

## 9. Updating / Hot Reload

- Edit code (`bot.py`, `ticket_manager.py`, `config_commands.py`) → bot restarts cleanly.
- Edit configs in `configs/` → panel auto-refreshes (old panels cleaned).
- Edit `main_config.json` → new settings apply to new interactions (debounced watcher log).

---

## 11. Troubleshooting

- **Slash commands not visible:**
  - The bot must have “Use Application Commands”
  - You ran `/setup`
  - If newly invited, allow some minutes or try re-sync (restart bot)

- **Panel didn’t post:**
  - Ensure `panel_channel_id` is set (via `/setup` or JSON)
  - Check the bot can send messages/embeds in that channel

- **Staff not in notes thread:**
  - Enable Server Members Intent on the bot app

- **Transcripts or close failing:**
  - The bot needs Read Message History, Attach Files, Manage Channels in the ticket channel
