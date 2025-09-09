import discord
from discord import app_commands
import json, os, re, time
from typing import List, Optional

CONFIGS_DIR = "configs"

ALLOWED_KEYS = [
    "support_role_ids",
    "ticket_category_id",
    "log_channel_id",
    "panel_channel_id",
    "user_limit_max_open",  # per-user open-ticket limit (staff/bot masters exempt)
]

# show small explanations in the key picker so people know what they're editing
KEY_CHOICES = [
    app_commands.Choice(name="support_role_ids — roles that can manage tickets", value="support_role_ids"),
    app_commands.Choice(name="ticket_category_id — default category for new tickets", value="ticket_category_id"),
    app_commands.Choice(name="log_channel_id — where transcripts are sent", value="log_channel_id"),
    app_commands.Choice(name="panel_channel_id — channel where the ticket panel lives", value="panel_channel_id"),
    app_commands.Choice(name="user_limit_max_open — max open tickets per user (staff exempt)", value="user_limit_max_open"),
]

def _cfg_path(guild_id: int) -> str:
    return os.path.join(CONFIGS_DIR, f"{guild_id}.json")

def get_server_config(guild_id: int) -> dict:
    path = _cfg_path(guild_id)
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_server_config(guild_id: int, config: dict) -> None:
    os.makedirs(CONFIGS_DIR, exist_ok=True)
    with open(_cfg_path(guild_id), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
def _tm_enabled_and_gids():
    try:
        with open("main_config.json", "r", encoding="utf-8") as f:
            tm = (json.load(f).get("test_mode") or {})
        enabled = bool(tm.get("enabled"))
        gids = set(int(x) for x in (tm.get("guild_ids") or []))
        if not gids and tm.get("guild_id"):
            gids = {int(tm["guild_id"])}
        return enabled, gids
    except Exception:
        return False, set()

def _blocked_by_testmode(gid: int | None) -> bool:
    enabled, gids = _tm_enabled_and_gids()
    if not enabled:
        return False
    return (gid is None) or (gid not in gids)

def _is_admin(member: discord.Member) -> bool:
    try:
        with open("main_config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        masters = set(int(v) for v in (cfg.get("bot_master_ids") or []))
        if cfg.get("bot_master_id"): masters.add(int(cfg["bot_master_id"]))
    except Exception:
        masters = set()
    return bool(member.guild_permissions.administrator) or (member.id in masters)

def _is_staff(member: discord.Member, cfg: dict, ticket_type_label: str | None, channel: discord.TextChannel | None) -> bool:
    if _is_admin(member):
        return True
    global_roles = cfg.get("support_role_ids", []) or []
    per_type_roles = []
    if ticket_type_label:
        t = next((t for t in cfg.get("ticket_types", []) if t.get("label")==ticket_type_label), {})
        per_type_roles = t.get("support_role_ids", []) or []
    role_ids = set(global_roles + per_type_roles)
    return any((r.id in role_ids) for r in getattr(member, "roles", []))

ROLE_ID_RE = re.compile(r"<@&(\d+)>|(\d+)")
def _parse_role_ids(text: str, guild: discord.Guild) -> List[int]:
    found = ROLE_ID_RE.findall(text or "")
    raw_ids = [(a or b) for (a, b) in found] or re.split(r"[,\s]+", (text or "").strip())
    ids: List[int] = []
    for token in raw_ids:
        try:
            rid = int(token)
            if guild.get_role(rid):
                ids.append(rid)
        except Exception:
            continue
    out, seen = [], set()
    for rid in ids:
        if rid not in seen:
            out.append(rid); seen.add(rid)
    return out

def _open_tickets_map() -> dict:
    try:
        with open("open_tickets.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _strip_status_marks(name: str) -> str:
    name = re.sub(r"^[🟢🟡🔴]\s*", "", name)
    name = re.sub(r"[ \-]*([🟢🟡🔴])\s*$", "", name)
    for pat in (r"\s*-\s*Approved$", r"\s*-\s*Waiting\s+for\s+Response$", r"\s*-\s*Issue\s*/\s*Problem$"):
        name = re.sub(pat, "", name)
    return name.strip(" -")

EMOJI_LABELS = {"approved":("🟢","Approved"), "waiting":("🟡","Waiting for Response"), "issue":("🔴","Issue / Problem")}

# autocomplete for ticket types in this guild
async def _ac_ticket_type(interaction: discord.Interaction, current: str):
    cfg = get_server_config(interaction.guild_id)
    labels = [t.get("label","") for t in (cfg.get("ticket_types") or [])]
    out = []
    for lbl in labels:
        if current.lower() in lbl.lower():
            out.append(app_commands.Choice(name=lbl, value=lbl))
        if len(out) >= 25:
            break
    return out
def setup(bot: discord.Client):
    # ----- /status -----
    async def _do_status(inter: discord.Interaction, value: str):
        if _blocked_by_testmode(inter.guild_id):
            await inter.response.send_message("Test mode is active. Commands are disabled in this server.", ephemeral=True); return
        await inter.response.defer(ephemeral=True)
        ch = inter.channel
        name = (getattr(ch, "name", "") or "").lower()
        if not isinstance(ch, discord.TextChannel) or not (name.startswith("ticket-") or name.startswith("testticket-")):
            await inter.followup.send("This is not a ticket channel.", ephemeral=True); return

        cfg = get_server_config(inter.guild_id)
        ot = _open_tickets_map(); rec = ot.get(str(ch.id)) or {}
        type_label = rec.get("type")

        if not _is_staff(inter.user, cfg, type_label, ch):
            await inter.followup.send("You don’t have permission to set ticket status.", ephemeral=True); return

        base=_strip_status_marks(ch.name)
        if value=="none":
            new_name=base; label_msg="none"; emoji=""; topic_text=None
        else:
            emoji,label=EMOJI_LABELS[value]
            if len(base)>99: base=base[:99]
            new_name=f"{base}{emoji}"; label_msg=f"{emoji} {label}"; topic_text=f"{emoji} {label}"

        now=time.time(); last=float(rec.get("last_status_rename",0)); COOLDOWN=600
        if now-last<COOLDOWN:
            try: await ch.edit(topic=topic_text)
            except: pass
            await inter.followup.send(f"✅ Status noted: {label_msg if emoji else 'none'}.\n⚠️ Rename cooldown (~2 per 10 min). Topic updated instead.", ephemeral=True); return

        try:
            await ch.edit(name=new_name, topic=topic_text, reason=f"/status by {inter.user}")
            rec["last_status_rename"]=now; ot[str(ch.id)]=rec
            with open("open_tickets.json","w",encoding="utf-8") as f: json.dump(ot,f,indent=2)
            await inter.followup.send(f"✅ Ticket status updated to {label_msg}", ephemeral=True)
        except Exception as e:
            await inter.followup.send(f"Failed to update status: {type(e).__name__}", ephemeral=True)

    @bot.tree.command(name="status", description="Set ticket status")
    @app_commands.choices(status=[
        app_commands.Choice(name="🟢 Approved", value="approved"),
        app_commands.Choice(name="🟡 Waiting for Response", value="waiting"),
        app_commands.Choice(name="🔴 Issue / Problem", value="issue"),
        app_commands.Choice(name="none (remove)", value="none"),
    ])
    async def status(interaction: discord.Interaction, status: app_commands.Choice[str]):
        await _do_status(interaction, status.value)

    # ----- /add -----
    @bot.tree.command(name="add", description="Add a user or role to a ticket")
    @app_commands.describe(user="User to add", role="Role to add (support perms)", ticket="Ticket channel (defaults to here)")
    async def add_to_ticket(interaction: discord.Interaction, user: Optional[discord.Member]=None, role: Optional[discord.Role]=None, ticket: Optional[discord.TextChannel]=None):
        if _blocked_by_testmode(interaction.guild_id):
            await interaction.response.send_message("Test mode is active. Commands are disabled in this server.", ephemeral=True); return

        await interaction.response.defer(ephemeral=True)
        if (user is None and role is None) or (user is not None and role is not None):
            await interaction.followup.send("Pick **one**: user *or* role.", ephemeral=True); return
        ch=ticket or interaction.channel
        name = (getattr(ch, "name", "") or "").lower()
        if not isinstance(ch, discord.TextChannel) or not (name.startswith("ticket-") or name.startswith("testticket-")):
            await interaction.followup.send("This is not a ticket channel.", ephemeral=True); return

        cfg=get_server_config(interaction.guild_id); ot=_open_tickets_map().get(str(ch.id)) or {}
        type_label=ot.get("type")
        if not _is_staff(interaction.user, cfg, type_label, ch):
            await interaction.followup.send("You don’t have permission to use /add here.", ephemeral=True); return

        perms=discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, add_reactions=True)
        try:
            if user:
                await ch.set_permissions(user, overwrite=perms, reason=f"/add by {interaction.user}")
                await interaction.followup.send(f"✅ Added {user.mention} to {ch.mention}.", ephemeral=True)
            else:
                if role and role.is_default(): await interaction.followup.send("Won’t modify @everyone on a ticket.", ephemeral=True); return
                await ch.set_permissions(role, overwrite=perms, reason=f"/add by {interaction.user}")
                await interaction.followup.send(f"✅ Added role {role.mention} to {ch.mention}.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to add: {type(e).__name__}", ephemeral=True)

    # ----- Admin-only: setup/panel/config -----
    @bot.tree.command(name="setup", description="Initial setup (admin only)")
    @app_commands.describe(panel_channel="Panel channel", ticket_category="Category for tickets", log_channel="Log channel", support_role="(Optional) Global support role")
    async def setup_cmd(interaction: discord.Interaction, panel_channel: discord.TextChannel, ticket_category: discord.CategoryChannel, log_channel: discord.TextChannel, support_role: Optional[discord.Role]=None):
        if _blocked_by_testmode(interaction.guild_id):
            await interaction.response.send_message("Test mode is active. Commands are disabled in this server.", ephemeral=True); return
        if not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
        cfg=get_server_config(interaction.guild.id)
        cfg["panel_channel_id"]=panel_channel.id; cfg["ticket_category_id"]=ticket_category.id; cfg["log_channel_id"]=log_channel.id
        if support_role:
            ids=set(cfg.get("support_role_ids",[])); ids.add(support_role.id); cfg["support_role_ids"]=list(ids)
        else:
            cfg.setdefault("support_role_ids", cfg.get("support_role_ids", []))
        save_server_config(interaction.guild.id, cfg)
        await interaction.response.send_message("✅ Setup complete. Use `/panel` to deploy the ticket panel.", ephemeral=True)

    @bot.tree.command(name="panel", description="Send the ticket panel")
    async def panel(interaction: discord.Interaction):
        if _blocked_by_testmode(interaction.guild_id):
            await interaction.response.send_message("Test mode is active. Commands are disabled in this server.", ephemeral=True); return
        if not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
        try:
            await bot.ticket_manager.send_ticket_panel(interaction)
        except Exception as e:
            print(f"[❌ PANEL ERROR] {type(e).__name__}: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Something went wrong while opening the panel.", ephemeral=True)

    @bot.tree.command(name="viewconfig", description="View current config (admin only)")
    async def view_config(interaction: discord.Interaction):
        if _blocked_by_testmode(interaction.guild_id):
            await interaction.response.send_message("Test mode is active. Commands are disabled in this server.", ephemeral=True); return
        if not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
        cfg=get_server_config(interaction.guild.id)
        await interaction.response.send_message(f"```json\n{json.dumps(cfg, indent=2)[:1900]}\n```", ephemeral=True)

    @bot.tree.command(name="editconfig", description="Edit a value in the server config (admin only)")
    @app_commands.describe(key="Which key (see descriptions in the list)", value="New value (IDs or mentions; for lists use comma/space separated)")
    @app_commands.choices(key=KEY_CHOICES)
    async def edit_config(interaction: discord.Interaction, key: app_commands.Choice[str], value: str):
        if _blocked_by_testmode(interaction.guild_id):
            await interaction.response.send_message("Test mode is active. Commands are disabled in this server.", ephemeral=True); return
        if not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
        cfg=get_server_config(interaction.guild.id); k=key.value
        if k not in ALLOWED_KEYS: await interaction.response.send_message("❌ This key cannot be edited.", ephemeral=True); return
        if k=="support_role_ids":
            cfg[k]=_parse_role_ids(value, interaction.guild)
        elif k=="user_limit_max_open":
            try: cfg["user_limit_max_open"]=max(0, int(value))
            except: await interaction.response.send_message("❌ Provide an integer.", ephemeral=True); return
        elif k.endswith("_id"):
            m=re.search(r"(\d+)", value); 
            if not m: await interaction.response.send_message("❌ Provide a valid channel/category ID or mention.", ephemeral=True); return
            cfg[k]=int(m.group(1))
        else:
            cfg[k]=value
        save_server_config(interaction.guild.id, cfg)
        await interaction.response.send_message(f"✅ Updated `{k}`.", ephemeral=True)

    # ----- /intake (admin-only) -----
    intake = app_commands.Group(name="intake", description="Manage intake forms (admin only)")

    @intake.command(name="enable", description="Enable or disable the intake form for a ticket type")
    @app_commands.describe(ticket_type="Ticket type label", enabled="Enable?")
    @app_commands.autocomplete(ticket_type=_ac_ticket_type)
    async def intake_enable(interaction: discord.Interaction, ticket_type: str, enabled: bool):
        if _blocked_by_testmode(interaction.guild_id): await interaction.response.send_message("Test mode is active.", ephemeral=True); return
        if not _is_admin(interaction.user): await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
        cfg=get_server_config(interaction.guild_id); t=next((t for t in cfg.get("ticket_types",[]) if t.get("label")==ticket_type), None)
        if not t: await interaction.response.send_message("Unknown ticket type.", ephemeral=True); return
        t.setdefault("intake_form",{})["enabled"]=bool(enabled); save_server_config(interaction.guild_id,cfg)
        await interaction.response.send_message(f"✅ Intake for **{ticket_type}** set to **{enabled}**.", ephemeral=True)

    @intake.command(name="view", description="Show current intake questions for a ticket type")
    @app_commands.describe(ticket_type="Ticket type label")
    @app_commands.autocomplete(ticket_type=_ac_ticket_type)
    async def intake_view(interaction: discord.Interaction, ticket_type: str):
        if _blocked_by_testmode(interaction.guild_id): await interaction.response.send_message("Test mode is active.", ephemeral=True); return
        if not _is_admin(interaction.user): await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
        cfg=get_server_config(interaction.guild_id); t=next((t for t in cfg.get("ticket_types",[]) if t.get("label")==ticket_type), None)
        if not t: await interaction.response.send_message("Unknown ticket type.", ephemeral=True); return
        form=t.get("intake_form") or {}; qs=form.get("questions") or []
        if not qs: await interaction.response.send_message("No questions set.", ephemeral=True); return
        lines=[f"{i+1}. **{q.get('label','Question')}** — style: {q.get('style','short')}, required: {bool(q.get('required',True))}, placeholder: {q.get('placeholder') or '—'}" for i,q in enumerate(qs)]
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @intake.command(name="addquestion", description="Add a question to a ticket type's intake form")
    @app_commands.describe(ticket_type="Ticket type label", label="Question label (<=45 chars)", style="short or paragraph", required="Is this field required?", placeholder="Optional placeholder", position="Insert at position (1-based); omit to append")
    @app_commands.autocomplete(ticket_type=_ac_ticket_type)
    async def intake_addquestion(interaction: discord.Interaction, ticket_type: str, label: str, style: str, required: bool, placeholder: Optional[str]=None, position: Optional[int]=None):
        if _blocked_by_testmode(interaction.guild_id): await interaction.response.send_message("Test mode is active.", ephemeral=True); return
        if not _is_admin(interaction.user): await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
        style = "paragraph" if str(style).lower().startswith("para") else "short"
        cfg=get_server_config(interaction.guild_id); t=next((t for t in cfg.get("ticket_types",[]) if t.get("label")==ticket_type), None)
        if not t: await interaction.response.send_message("Unknown ticket type.", ephemeral=True); return
        form=t.setdefault("intake_form",{}); qs=form.setdefault("questions",[])
        q={"label":label[:45],"style":style,"required":bool(required)}
        if placeholder: q["placeholder"]=placeholder[:80]
        if position and 1 <= position <= len(qs)+1:
            qs.insert(position-1,q)
        else:
            qs.append(q)
        form["enabled"]=True
        save_server_config(interaction.guild_id,cfg)
        await interaction.response.send_message(f"✅ Added question to **{ticket_type}** (now {len(qs)} total).", ephemeral=True)

    @intake.command(name="removequestion", description="Remove a question by index")
    @app_commands.describe(ticket_type="Ticket type label", index="1-based index")
    @app_commands.autocomplete(ticket_type=_ac_ticket_type)
    async def intake_removequestion(interaction: discord.Interaction, ticket_type: str, index: int):
        if _blocked_by_testmode(interaction.guild_id): await interaction.response.send_message("Test mode is active.", ephemeral=True); return
        if not _is_admin(interaction.user): await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
        cfg=get_server_config(interaction.guild_id); t=next((t for t in cfg.get("ticket_types",[]) if t.get("label")==ticket_type), None)
        if not t: await interaction.response.send_message("Unknown ticket type.", ephemeral=True); return
        qs=(t.setdefault("intake_form",{}).setdefault("questions",[]))
        if not (1 <= index <= len(qs)): await interaction.response.send_message("Index out of range.", ephemeral=True); return
        qs.pop(index-1); save_server_config(interaction.guild_id,cfg)
        await interaction.response.send_message(f"✅ Removed. {len(qs)} question(s) left.", ephemeral=True)

    @intake.command(name="clear", description="Remove all questions for a ticket type")
    @app_commands.describe(ticket_type="Ticket type label")
    @app_commands.autocomplete(ticket_type=_ac_ticket_type)
    async def intake_clear(interaction: discord.Interaction, ticket_type: str):
        if _blocked_by_testmode(interaction.guild_id): await interaction.response.send_message("Test mode is active.", ephemeral=True); return
        if not _is_admin(interaction.user): await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
        cfg=get_server_config(interaction.guild_id); t=next((t for t in cfg.get("ticket_types",[]) if t.get("label")==ticket_type), None)
        if not t: await interaction.response.send_message("Unknown ticket type.", ephemeral=True); return
        t.setdefault("intake_form",{})["questions"]=[]
        save_server_config(interaction.guild_id,cfg)
        await interaction.response.send_message("✅ Cleared all questions.", ephemeral=True)

    bot.tree.add_command(intake)
