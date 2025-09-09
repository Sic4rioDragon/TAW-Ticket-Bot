import discord
from discord.ext import commands
from discord import app_commands
import asyncio, os, sys, json

from ticket_manager import TicketManager
from config_commands import setup as setup_config_commands

CONFIG_FILE = "main_config.json"
if not os.path.exists(CONFIG_FILE):
    raise FileNotFoundError(f"{CONFIG_FILE} not found.")

with open(CONFIG_FILE, encoding="utf-8") as f:
    _cfg = json.load(f)

TOKEN = (_cfg.get("token") or "").strip()
if not TOKEN:
    raise ValueError("Token not found in main_config.json.")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
ticket_manager = TicketManager(bot)
bot.ticket_manager = ticket_manager

# global test-mode guard (multi-guild). if test is ON, disable in other guilds.
async def _tm_interaction_check(interaction: discord.Interaction) -> bool:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            tm = (json.load(f).get("test_mode") or {})
        enabled = bool(tm.get("enabled"))
        gids = set(int(x) for x in (tm.get("guild_ids") or []))
        if not gids and tm.get("guild_id"):
            gids = {int(tm["guild_id"])}
        if enabled and (interaction.guild_id not in gids):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Test mode is active. Commands are disabled in this server.",
                    ephemeral=True
                )
            return False
    except Exception:
        pass
    return True

try:
    bot.tree.interaction_check = _tm_interaction_check  # type: ignore[attr-defined]
    print("✅ Attached global interaction_check for test mode")
except Exception as e:
    print(f"⚠️ Could not attach interaction_check: {type(e).__name__}: {e}")
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

    # Read test-mode once for startup decisions
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        _cfg = json.load(f)
    tm = _cfg.get("test_mode") or {}
    tm_enabled = bool(tm.get("enabled"))
    tm_guild_ids = set(int(x) for x in (tm.get("guild_ids") or []))
    if not tm_guild_ids and tm.get("guild_id"):
        tm_guild_ids = {int(tm["guild_id"])}

    # 1) Register commands and persistent views
    try:
        setup_config_commands(bot)
        await ticket_manager.register_persistent_views()
        print("✅ Commands/views registered")
    except Exception as e:
        print(f"❌ setup error: {type(e).__name__}: {e}")

    # 2) Sync commands (robust: timeout + per-guild fallback)
    synced_global = False
    try:
        await asyncio.wait_for(bot.tree.sync(), timeout=20)
        print("✅ Synced commands globally")
        synced_global = True
    except asyncio.TimeoutError:
        print("⏳ Global sync timed out; falling back to per-guild sync…")
    except Exception as e:
        print(f"❌ Global sync failed: {type(e).__name__}: {e}")

    for g in bot.guilds:
        try:
            await asyncio.wait_for(bot.tree.sync(guild=discord.Object(id=g.id)), timeout=12)
            print(f"✅ Synced commands to {g.name} ({g.id})")
        except asyncio.TimeoutError:
            print(f"⏳ Per-guild sync timed out for {g.name} ({g.id})")
        except Exception as e:
            print(f"❌ Per-guild sync failed in {g.name}: {type(e).__name__}: {e}")

    # 3) Re-post the panel where allowed (auto-deletes old one)
    for guild in bot.guilds:
        try:
            if tm_enabled and tm_guild_ids and guild.id not in tm_guild_ids:
                print(f"⏭️ Test mode: skipping panel in {guild.name} ({guild.id})")
                continue
            cfg = ticket_manager.get_config(guild.id)
            ch = guild.get_channel(cfg.get("panel_channel_id"))
            if ch:
                await ticket_manager.send_ticket_panel_to_channel(ch)
                print(f"✅ Sent panel to #{getattr(ch,'name','?')} in {guild.name}")
        except Exception as e:
            print(f"⚠️ Panel refresh error in {guild.name}: {type(e).__name__}: {e}")

    # 4) Start the watcher (reload panels on config edits; restart on code edits)
    asyncio.create_task(_watch_files())

def _sanitize_cfg_for_panel(d: dict) -> dict:
    # ignore counters so ticket-number bumps don't trigger a panel refresh
    if not isinstance(d, dict):
        return {}
    out = dict(d)
    out.pop("ticket_numbers", None)
    return out

async def _watch_files():
    """Reload panels on real config edits; restart on code edits; ignore ticket number bumps."""
    def mtimes(paths):
        out = {}
        for p in paths:
            if os.path.exists(p):
                try:
                    out[p] = os.path.getmtime(p)
                except:
                    pass
        return out

    def tracked_cfg():
        cfg_dir = "configs"
        return [os.path.join(cfg_dir, f) for f in os.listdir(cfg_dir) if f.endswith(".json")] if os.path.isdir(cfg_dir) else []

    def tracked_all():
        return [CONFIG_FILE] + code_files + tracked_cfg()

    def load_json_safe(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    code_files = ["bot.py", "ticket_manager.py", "config_commands.py"]
    last_mtime = mtimes(tracked_all())
    cfg_snapshot = {p: _sanitize_cfg_for_panel(load_json_safe(p) or {}) for p in tracked_cfg()}
    print("👀 Watcher started.")

    while True:
        await asyncio.sleep(2.0)
        current = mtimes(tracked_all())

        # code changes → restart
        if any(p in current and p in last_mtime and current[p] != last_mtime[p] for p in code_files):
            print("♻️ Code change detected. Restarting…")
            try:
                await bot.close()
            except:
                pass
            os.execl(sys.executable, sys.executable, *sys.argv)
            return

        # main_config.json changed
        if CONFIG_FILE in current and CONFIG_FILE in last_mtime and current[CONFIG_FILE] != last_mtime[CONFIG_FILE]:
            print("🔄 main_config.json changed. New settings will apply to new interactions.")

        # config changes → refresh panel only if meaningful fields changed
        for path in list(cfg_snapshot.keys()) + [p for p in tracked_cfg() if p not in cfg_snapshot]:
            if path not in current:
                continue
            if path in last_mtime and current.get(path) == last_mtime.get(path):
                continue

            new_cfg_raw = load_json_safe(path) or {}
            new_sanitized = _sanitize_cfg_for_panel(new_cfg_raw)
            old_sanitized = cfg_snapshot.get(path)
            cfg_snapshot[path] = new_sanitized
            last_mtime[path] = current.get(path)

            if new_sanitized == old_sanitized:
                continue  # only ticket number counters changed

            try:
                gid = int(os.path.splitext(os.path.basename(path))[0])
                guild = bot.get_guild(gid)
                if not guild:
                    continue

                with open(CONFIG_FILE, "r", encoding="utf-8") as _f:
                    _tm = (json.load(_f).get("test_mode") or {})
                tm_enabled = bool(_tm.get("enabled"))
                tm_gids = set(int(x) for x in (_tm.get("guild_ids") or []))
                if not tm_gids and _tm.get("guild_id"):
                    tm_gids = {int(_tm["guild_id"])}
                if tm_enabled and tm_gids and guild.id not in tm_gids:
                    continue

                cfg = ticket_manager.get_config(gid)
                ch_id = cfg.get("panel_channel_id")
                ch = guild.get_channel(ch_id) if ch_id else None
                if not ch:
                    print(f"⚠️ Config changed for {gid} but panel channel missing.")
                    continue

                async for msg in ch.history(limit=50):
                    if msg.author.id == bot.user.id:
                        await msg.delete()
                await ticket_manager.send_ticket_panel_to_channel(ch)
                print(f"🔁 Refreshed panel in {guild.name} after config edit.")
            except Exception as e:
                print(f"⚠️ Panel refresh failed for {path}: {type(e).__name__}: {e}")

@bot.event
async def on_member_remove(member: discord.Member):
    try:
        await ticket_manager.autoclose_if_opener(member)
    except Exception as e:
        print(f"[⚠️ on_member_remove] {type(e).__name__}: {e}")

def run():
    asyncio.run(bot.start(TOKEN))

if __name__ == "__main__":
    run()
