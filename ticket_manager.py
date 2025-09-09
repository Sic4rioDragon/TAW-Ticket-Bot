import discord
import os, json, re, time, html
from datetime import datetime, timezone
from typing import Tuple, Optional

CONFIG_FOLDER = "configs"
DEFAULT_CONFIG = os.path.join(CONFIG_FOLDER, "default.json")
OPEN_TICKETS_FILE = "open_tickets.json"

# test-mode helpers (multi-guild). if test is ON, only listed guild(s) can run commands.
def _tm_guild_ids() -> tuple[bool, set[int]]:
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

def _tm_allows_guild(gid: int | None) -> bool:
    enabled, gids = _tm_guild_ids()
    if not enabled:
        return True
    return (gid is not None) and (gid in gids)

def _is_test_guild(gid: int | None) -> bool:
    enabled, gids = _tm_guild_ids()
    return enabled and (gid is not None) and (gid in gids)

def _bot_masters() -> set[int]:
    # you can have one or many bot masters in main_config.json
    try:
        with open("main_config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        ids = set()
        if cfg.get("bot_master_id"): ids.add(int(cfg["bot_master_id"]))
        for v in (cfg.get("bot_master_ids") or []): ids.add(int(v))
        return ids
    except Exception:
        return set()

def get_config_path(guild_id: int) -> str:
    return os.path.join(CONFIG_FOLDER, f"{guild_id}.json")

def load_config(guild_id: int) -> dict:
    path = get_config_path(guild_id)
    os.makedirs(CONFIG_FOLDER, exist_ok=True)
    if not os.path.exists(path):
        with open(DEFAULT_CONFIG, "r", encoding="utf-8") as df:
            default_data = json.load(df)
        with open(path, "w", encoding="utf-8") as nf:
            json.dump(default_data, nf, indent=2)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(guild_id: int, data: dict) -> None:
    os.makedirs(CONFIG_FOLDER, exist_ok=True)
    with open(get_config_path(guild_id), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def load_open_tickets() -> dict:
    if not os.path.exists(OPEN_TICKETS_FILE): return {}
    with open(OPEN_TICKETS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_open_tickets(data: dict) -> None:
    with open(OPEN_TICKETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _sanitize_username(name: str) -> str:
    # keep channel names readable + safe
    name = re.sub(r"[^a-zA-Z0-9_\-]+", "-", name).strip("-")
    return name[:48] or "user"
class TicketManager:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.open_tickets = load_open_tickets()

    # ---------- helpers ----------
    def get_config(self, guild_id: int) -> dict:
        return load_config(guild_id)

    def _make_overwrites(self, guild: discord.Guild, opener: discord.abc.User, support_role_ids: list[int]) -> dict:
        # default: hide from everyone, allow opener + support
        ow = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            opener: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        for rid in set(support_role_ids or []):
            role = guild.get_role(rid)
            if role:
                ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        return ow

    def _support_mentions(self, guild: discord.Guild, role_ids: list[int], exclude_ids: list[int] | None, allow_mentions: bool) -> list[str]:
        # build @role mentions unless we're in test mode or role is excluded from ping
        if not allow_mentions:
            return []
        excludes = set(exclude_ids or [])
        out = []
        for rid in dict.fromkeys(role_ids or []):
            if rid in excludes:
                continue
            role = guild.get_role(rid)
            if role:
                out.append(role.mention)
        return out

    # ---------- numbering ----------
    def _next_ticket_number(self, guild_id: int, type_label: str | None, cfg: dict) -> Tuple[str, int]:
        tn = cfg.setdefault("ticket_numbers", {})
        width = int(tn.get("width", 4))
        width = min(max(width, 3), 6)
        per_type = tn.setdefault("per_type", {})
        if type_label and type_label in per_type:
            block = per_type[type_label] = {
                "start": int(per_type[type_label].get("start", 1)),
                "next":  int(per_type[type_label].get("next",  per_type[type_label].get("start", 1))),
            }
        else:
            g = tn.setdefault("global", {})
            block = tn["global"] = {"start": int(g.get("start", 1)), "next": int(g.get("next", g.get("start", 1)))}
        n = int(block["next"]); block["next"] = n + 1; save_config(guild_id, cfg)
        return str(n).zfill(width), n

    # ---------- per-user limit (staff + masters exempt) ----------
    def _user_limit_violation(self, member: discord.Member, cfg: dict, combined_roles: list[int]) -> Optional[str]:
        max_open = int(cfg.get("user_limit_max_open") or 0)
        if max_open <= 0:
            return None
        # exemptions
        if member.guild_permissions.administrator:
            return None
        if member.id in _bot_masters():
            return None
        # staff exempt if they have any support role (global or per-type combined)
        if any((member.guild.get_role(rid) in member.roles) for rid in combined_roles if member.guild.get_role(rid)):
            return None
        # count how many tickets this user currently owns in this guild
        count = sum(1 for _, rec in (self.open_tickets or {}).items()
                    if rec.get("guild_id") == member.guild.id and rec.get("user_id") == member.id)
        if count >= max_open:
            return f"You already have {count} open ticket(s). Limit is {max_open}."
        return None
    # ---------- panel ----------
    async def _delete_old_panels(self, channel: discord.TextChannel, limit: int = 200):
        # nuke old panels we posted earlier so the channel stays tidy
        deleted = 0
        try:
            async for msg in channel.history(limit=limit):
                if msg.author.id != self.bot.user.id:
                    continue
                is_panel = False
                try:
                    if msg.components and "ticket_type_select" in str(msg.components):
                        is_panel = True
                except Exception:
                    pass
                if not is_panel and msg.embeds:
                    try:
                        if any((e.title or "").lower() == "support panel" for e in msg.embeds):
                            is_panel = True
                    except Exception:
                        pass
                if is_panel:
                    try:
                        await msg.delete()
                        deleted += 1
                    except Exception as e:
                        print(f"[⚠️ Panel delete error] {type(e).__name__}: {e}")
        except Exception as e:
            print(f"[⚠️ Panel sweep error] {type(e).__name__}: {e}")
        if deleted:
            print(f"🧽 Removed {deleted} old panel message(s) in #{channel.name}")

    async def send_ticket_panel(self, interaction: discord.Interaction):
        if not _tm_allows_guild(interaction.guild_id):
            await interaction.response.send_message("Test mode is active. This bot only works in the designated test server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._delete_old_panels(interaction.channel)
        await self._send_ticket_panel_internal(interaction.guild_id, interaction.channel, interaction=interaction)

    async def send_ticket_panel_to_channel(self, channel: discord.TextChannel):
        if not _tm_allows_guild(channel.guild.id): return
        await self._delete_old_panels(channel)
        await self._send_ticket_panel_internal(channel.guild.id, channel)

    async def _send_ticket_panel_internal(self, guild_id: int, channel: discord.TextChannel, interaction: discord.Interaction | None = None):
        config = load_config(guild_id)
        # only show enabled types in the dropdown
        ticket_types: list[dict] = [t for t in (config.get("ticket_types") or []) if t.get("enabled", True)]

        async def _create_after_form(ix: discord.Interaction, ticket_type_label: str, form_answers: list[tuple[str,str]] | None):
            guild = ix.guild
            ttype = next((t for t in ticket_types if t.get("label") == ticket_type_label), {})
            per_type_roles = ttype.get("support_role_ids", []) or []
            global_roles = config.get("support_role_ids", []) or []
            combined_roles = list(dict.fromkeys(per_type_roles + global_roles))
            # per-user limit (staff/masters exempt)
            violation = self._user_limit_violation(ix.user, config, combined_roles)
            if violation:
                await ix.followup.send(f"❌ {violation}", ephemeral=True); return

            # make a pretty, stable channel name
            padded, number = self._next_ticket_number(guild.id, ticket_type_label, config)
            uname = _sanitize_username(ix.user.name)
            prefix = "testticket" if (guild.id == 1354566385438691479 and _is_test_guild(guild.id)) else "ticket"
            ch_name = f"{prefix}-{padded}-{uname}"

            # create the channel (explicit null category_id => top-level)
            try:
                if "category_id" in ttype:
                    cid = ttype.get("category_id")
                    cat = guild.get_channel(cid) if cid else None
                else:
                    cat = guild.get_channel(config.get("ticket_category_id"))
                overwrites = self._make_overwrites(guild, ix.user, combined_roles)
                ticket_channel = await guild.create_text_channel(
                    name=ch_name,
                    category=cat if isinstance(cat, discord.CategoryChannel) else None,
                    overwrites=overwrites
                )
            except Exception as e:
                print(f"[❌ CREATE CHANNEL ERROR] {type(e).__name__}: {e}")
                await ix.followup.send("❌ Could not create a ticket channel (check permissions/config).", ephemeral=True)
                return

            # store metadata so we can manage status, notes thread, etc.
            self.open_tickets[str(ticket_channel.id)] = {
                "guild_id": guild.id, "user_id": ix.user.id, "type": ticket_type_label,
                "number": number, "open_time": time.time()
            }
            save_open_tickets(self.open_tickets)

            # minimal overview embed (your partner bot does the wordy welcome)
            try:
                overview = discord.Embed(
                    title=f"Ticket #{padded}",
                    description=f"Opened by {ix.user.mention}\nType: **{ticket_type_label}**",
                    color=0x2f3136
                )
                await ticket_channel.send(embed=overview)
            except Exception as e:
                print(f"[⚠️ OVERVIEW EMBED ERROR] {type(e).__name__}: {e}")

            # staff-only notes thread (private thread). lazy add support members.
            try:
                thread = await ticket_channel.create_thread(name=f"notes-{padded}", type=discord.ChannelType.private_thread, invitable=False)
                added = set()
                for rid in combined_roles:
                    role = guild.get_role(rid)
                    if not role: continue
                    for m in role.members:
                        if m.bot or m.id in added: continue
                        try: await thread.add_user(m); added.add(m.id)
                        except Exception: pass
                await thread.send("🗒️ Staff-only notes thread created. Use this thread for internal discussion.")
                rec = self.open_tickets.get(str(ticket_channel.id)) or {}
                rec["notes_thread_id"] = thread.id
                self.open_tickets[str(ticket_channel.id)] = rec; save_open_tickets(self.open_tickets)
            except Exception as e:
                print(f"[⚠️ NOTES THREAD ERROR] {type(e).__name__}: {e}")
            # ping support unless we're in test mode or role is excluded from mention
            allow_mentions = not _is_test_guild(guild.id)
            exclude = list(dict.fromkeys((config.get("no_mention_role_ids") or []) + (ttype.get("no_mention_role_ids") or [])))
            mentions = " ".join(self._support_mentions(guild, combined_roles, exclude, allow_mentions))
            mention_prefix = (mentions + " ") if mentions else ""

            # post the control message with the Close button — and PIN it so it's easy to find
            try:
                ctrl_msg = await ticket_channel.send(
                    f"{mention_prefix}{ix.user.mention} A staff member will be with you shortly.",
                    view=self._close_view()
                )
                try:
                    await ctrl_msg.pin(reason="Pin ticket controls")
                except Exception as e:
                    print(f"[⚠️ PIN ERROR] {type(e).__name__}: {e}")
            except Exception as e:
                print(f"[❌ SEND GREETING ERROR] {type(e).__name__}: {e}")

            await ix.followup.send(f"✅ Ticket #{padded} created!", ephemeral=True)

        async def create_ticket(ix: discord.Interaction, ticket_type_label: str):
            if not _tm_allows_guild(getattr(ix.guild, "id", None)):
                await ix.response.send_message("Test mode is active. This bot only works in the designated test server.", ephemeral=True)
                return

            ttype = next((t for t in ticket_types if t.get("label") == ticket_type_label), {})
            intake = (ttype.get("intake_form") or {})

            # dynamic intake form (up to 5 inputs)
            if intake.get("enabled") and isinstance(intake.get("questions"), list) and intake["questions"]:
                class IntakeModal(discord.ui.Modal, title=f"{ticket_type_label} – Intake"): pass
                inputs = []
                for q in intake["questions"][:5]:
                    label = (str(q.get("label") or "")[:45]) or "Question"
                    style = discord.TextStyle.short if str(q.get("style", "short")).lower() == "short" else discord.TextStyle.paragraph
                    required = bool(q.get("required", True))
                    placeholder = (str(q.get("placeholder") or "")[:80]) or None
                    ti = discord.ui.TextInput(label=label, style=style, required=required, placeholder=placeholder, max_length=4000)
                    setattr(IntakeModal, f"field_{len(inputs)}", ti); inputs.append(ti)
                modal = IntakeModal()
                async def on_submit(_self, _ix: discord.Interaction):
                    await _ix.response.defer(ephemeral=True)
                    answers = [(inp.label, str(inp.value)) for inp in inputs]
                    await _create_after_form(_ix, ticket_type_label, answers)
                modal.on_submit = on_submit
                await ix.response.send_modal(modal)
            else:
                await ix.response.defer(ephemeral=True)
                await _create_after_form(ix, ticket_type_label, None)

        # ---- panel UI ----
        class TicketTypeDropdown(discord.ui.Select):
            def __init__(self):
                options=[]
                for t in ticket_types:
                    try: options.append(discord.SelectOption(label=t["label"], description=t.get("description",""), emoji=t.get("emoji")))
                    except: continue
                super().__init__(placeholder="Choose a ticket type...", options=options, custom_id="ticket_type_select")
            async def callback(self, ix2: discord.Interaction):
                try: await create_ticket(ix2, self.values[0])
                except Exception as e:
                    print(f"[❌ CREATE_TICKET ERROR] {type(e).__name__}: {e}")
                    if not ix2.response.is_done():
                        await ix2.response.send_message("❌ Failed to create ticket.", ephemeral=True)

        class TicketView(discord.ui.View):
            def __init__(self): super().__init__(timeout=None); self.add_item(TicketTypeDropdown())

        panel_embed = discord.Embed(title="Support Panel", description="Select the type of ticket you'd like to open.", color=0x2f3136)
        try:
            await channel.send(embed=panel_embed, view=TicketView())
        except Exception as e:
            print(f"[❌ PANEL SEND ERROR] {type(e).__name__}: {e}")
            if interaction and not interaction.response.is_done():
                await interaction.response.send_message("❌ Something went wrong while opening the panel.", ephemeral=True)

    # single-use Close button view for new tickets
    def _close_view(self) -> discord.ui.View:
        manager = self
        class CloseView(discord.ui.View):
            def __init__(self): super().__init__(timeout=None)
            @discord.ui.button(label="Close", style=discord.ButtonStyle.red, custom_id="close_ticket_button")
            async def close(self, ix: discord.Interaction, button: discord.ui.Button):
                await manager.close_ticket(ix)
        return CloseView()
    # persistent views so buttons survive restarts
    async def register_persistent_views(self):
        class FallbackTypeDropdown(discord.ui.Select):
            def __init__(self):
                opts=[discord.SelectOption(label="Unavailable", description="Re-open the panel to create a ticket")]
                super().__init__(placeholder="⚠️ Reopen panel to use", options=opts, custom_id="ticket_type_select")
            async def callback(self, ix: discord.Interaction):
                await ix.response.send_message("⚠️ Ticket creation is disabled after a restart. Please run `/panel`.", ephemeral=True)
        class FallbackTypeView(discord.ui.View):
            def __init__(self): super().__init__(timeout=None); self.add_item(FallbackTypeDropdown())
        manager=self
        class CloseView(discord.ui.View):
            def __init__(self): super().__init__(timeout=None)
            @discord.ui.button(label="Close", style=discord.ButtonStyle.red, custom_id="close_ticket_button")
            async def close(self, ix: discord.Interaction, button: discord.ui.Button): await manager.close_ticket(ix)
        self.bot.add_view(FallbackTypeView()); self.bot.add_view(CloseView())

    # ask how to close, with opener having fewer options
    async def close_ticket(self, interaction: discord.Interaction):
        try: await interaction.response.defer(ephemeral=True)
        except: pass

        if not _tm_allows_guild(getattr(interaction.guild, "id", None)):
            try: await interaction.followup.send("Test mode is active. This bot only works in the designated test server.", ephemeral=True)
            except: pass
            return

        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.TextChannel):
            try: await interaction.followup.send("❌ This must be used inside a ticket channel.", ephemeral=True)
            except: pass
            return

        name = (channel.name or "").lower()
        is_ticket = name.startswith("ticket-") or name.startswith("testticket-")
        if not is_ticket:
            try: await interaction.followup.send("❌ This must be used inside a ticket channel.", ephemeral=True)
            except: pass
            return

        rec = self.open_tickets.get(str(channel.id)) or {}
        is_opener = rec.get("user_id") and interaction.user.id == rec.get("user_id")
        view = self._ConfirmCloseViewOpener(self, channel) if is_opener else self._ConfirmCloseView(self, channel)
        try:
            await interaction.followup.send("How should I close this ticket?", view=view, ephemeral=True)
        except Exception as e:
            print(f"[❌ CLOSE DIALOG ERROR] {type(e).__name__}: {e}")

    # keep disk tidy: if >50 transcripts, delete 20 oldest
    def _prune_transcripts_if_needed(self):
        try:
            folder = "transcripts"
            if not os.path.isdir(folder):
                return
            files = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".html")]
            if len(files) <= 50:
                return
            files.sort(key=lambda p: os.path.getmtime(p))  # oldest first
            for p in files[:20]:
                try:
                    os.remove(p)
                except Exception as e:
                    print(f"[⚠️ prune] {type(e).__name__}: {e}")
            print("🧹 pruned 20 old transcripts.")
        except Exception as e:
            print(f"[⚠️ prune error] {type(e).__name__}: {e}")

    class _ConfirmCloseView(discord.ui.View):
        def __init__(self, manager:"TicketManager", channel:discord.TextChannel): super().__init__(timeout=60); self.manager=manager; self.channel=channel
        @discord.ui.button(label="Save transcript & delete", style=discord.ButtonStyle.green, custom_id="confirm_close_save")
        async def confirm_save(self, ix:discord.Interaction, _): await ix.response.defer(ephemeral=True); await self.manager._finalize_close(ix, self.channel, True); await ix.followup.send("saved", ephemeral=True)
        @discord.ui.button(label="Delete without transcript", style=discord.ButtonStyle.gray, custom_id="confirm_close_delete")
        async def confirm_delete(self, ix:discord.Interaction, _):
            rec=self.manager.open_tickets.get(str(self.channel.id)) or {}
            if ix.user.id == rec.get("user_id"): await ix.response.send_message("As the ticket opener, you can only **save transcript & delete**.", ephemeral=True); return
            await ix.response.defer(ephemeral=True); await self.manager._finalize_close(ix, self.channel, False)
        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="confirm_close_cancel")
        async def cancel(self, ix:discord.Interaction, _): await ix.response.edit_message(content="Close canceled.", view=None)

    class _ConfirmCloseViewOpener(discord.ui.View):
        def __init__(self, manager:"TicketManager", channel:discord.TextChannel): super().__init__(timeout=60); self.manager=manager; self.channel=channel
        @discord.ui.button(label="Save transcript & delete", style=discord.ButtonStyle.green, custom_id="confirm_close_save_opener")
        async def confirm_save_opener(self, ix:discord.Interaction, _): await ix.response.defer(ephemeral=True); await self.manager._finalize_close(ix, self.channel, True); await ix.followup.send("saved", ephemeral=True)
        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="confirm_close_cancel_opener")
        async def cancel_opener(self, ix:discord.Interaction, _): await ix.response.edit_message(content="Close canceled.", view=None)
    async def _finalize_close(self, ix: Optional[discord.Interaction], channel: discord.TextChannel, save_transcript: bool):
        guild = channel.guild
        config = load_config(guild.id)
        rec = self.open_tickets.get(str(channel.id)) or {}
        opener_id, per_type = rec.get("user_id"), rec.get("type")
        opener = guild.get_member(opener_id) if opener_id else None

        transcript_path = None
        if save_transcript:
            # build a Ticket-Tool style HTML transcript (embeds, attachments, avatars, participants)
            messages_html = []
            participants = {}  # uid -> (name, avatar_url)
            count = 0
            try:
                async for msg in channel.history(limit=None, oldest_first=True):
                    count += 1
                    # participants
                    try:
                        participants[msg.author.id] = (
                            str(msg.author),
                            str(getattr(msg.author.display_avatar, "url", "")),
                        )
                    except Exception:
                        participants[msg.author.id] = (str(msg.author), "")

                    ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                    author = html.escape(str(msg.author))
                    avatar = html.escape(str(getattr(msg.author.display_avatar, "url", "")))
                    content = html.escape(msg.content or "").replace("\n", "<br>")

                    # attachments
                    attach_html = ""
                    if msg.attachments:
                        links = []
                        for a in msg.attachments:
                            try:
                                links.append(f"<a href='{html.escape(a.url)}' target='_blank'>{html.escape(a.filename)}</a>")
                            except Exception:
                                pass
                        if links:
                            attach_html = f"<div class='attach'>📎 {' • '.join(links)}</div>"

                    # embeds
                    ehtml = []
                    for e in (msg.embeds or []):
                        try:
                            color = f"#{(e.color.value if e.color else 0x5865F2):06x}"
                        except Exception:
                            color = "#5865F2"
                        title = html.escape(e.title) if getattr(e, "title", None) else ""
                        url = html.escape(e.url) if getattr(e, "url", None) else ""
                        desc = html.escape(e.description) if getattr(e, "description", None) else ""
                        if desc:
                            desc = desc.replace("\n", "<br>")

                        fields = ""
                        try:
                            for fld in e.fields:
                                fname = html.escape(fld.name or "")
                                fval = html.escape(fld.value or "").replace("\n", "<br>")
                                fields += f"<div class='efield'><div class='fname'>{fname}</div><div class='fvalue'>{fval}</div></div>"
                        except Exception:
                            pass

                        footer = ""
                        try:
                            ftxt = e.footer.text if e.footer else ""
                            if ftxt:
                                footer = f"<div class='efooter'>{html.escape(ftxt)}</div>"
                        except Exception:
                            pass

                        image_html = ""
                        try:
                            iurl = getattr(e.image, 'url', None)
                            if iurl:
                                image_html = f"<img class='eimg' src='{html.escape(iurl)}'/>"
                        except Exception:
                            pass

                        title_html = ""
                        if title and url:
                            title_html = f"<div class='etitle'><a href='{url}' target='_blank'>{title}</a></div>"
                        elif title:
                            title_html = f"<div class='etitle'>{title}</div>"

                        ehtml.append(
                            f"<div class='embed' style='border-color:{color}'>"
                            f"{title_html}"
                            f"{('<div class=\"edesc\">'+desc+'</div>') if desc else ''}"
                            f"{('<div class=\"efields\">'+fields+'</div>') if fields else ''}"
                            f"{image_html}{footer}</div>"
                        )

                    messages_html.append(
                        "<div class='msg'>"
                        f"<img class='avatar' src='{avatar}' onerror=\"this.style.display='none'\">"
                        "<div class='bubble'>"
                        f"<div class='head'><span class='author'>{author}</span>"
                        f"<span class='time'>{ts}</span></div>"
                        f"{('<div class=\"content\">'+content+'</div>') if content else ''}"
                        f"{''.join(ehtml)}{attach_html}"
                        "</div></div>"
                    )

                # Participants list (chips)
                part_html = ""
                if participants:
                    chips = []
                    for _uid, (nm, av) in participants.items():
                        chips.append(
                            f"<div class='chip'><img src='{html.escape(av)}' onerror=\"this.style.display='none'\">"
                            f"<span>{html.escape(nm)}</span></div>"
                        )
                    part_html = "<div class='participants'><div class='ptitle'>Participants</div>" + "".join(chips) + "</div>"

                os.makedirs("transcripts", exist_ok=True)
                transcript_path = f"transcripts/{channel.name}.html"
                number = rec.get("number")
                opened = datetime.fromtimestamp(rec.get("open_time", time.time()), tz=timezone.utc)
                closed = datetime.now(tz=timezone.utc)
                topic = channel.topic or ""
                header = (
                    f"<div class='card'><div class='cardtitle'>Ticket Summary</div>"
                    f"<table class='meta'>"
                    f"<tr><th>Guild</th><td>{html.escape(guild.name)}</td></tr>"
                    f"<tr><th>Channel</th><td>{html.escape(channel.name)}</td></tr>"
                    f"<tr><th>Ticket #</th><td>{number}</td></tr>"
                    f"<tr><th>Type</th><td>{html.escape(per_type or '')}</td></tr>"
                    f"<tr><th>Opener</th><td>{html.escape(str(opener)) if opener else opener_id}</td></tr>"
                    f"<tr><th>Opened</th><td>{opened.strftime('%Y-%m-%d %H:%M:%S UTC')}</td></tr>"
                    f"<tr><th>Closed</th><td>{closed.strftime('%Y-%m-%d %H:%M:%S UTC')}</td></tr>"
                    f"<tr><th>Status/Topic</th><td>{html.escape(topic)}</td></tr>"
                    f"<tr><th>Message Count</th><td>{count}</td></tr>"
                    f"</table></div>"
                )

                html_doc = (
                    "<html><head><meta charset='UTF-8'>"
                    "<style>"
                    "body{background:#2f3136;color:#ddd;font-family:Segoe UI,Arial,sans-serif;margin:0;padding:24px}"
                    ".card{background:#1e1f22;border:1px solid #3a3c41;border-radius:10px;padding:14px;margin-bottom:14px}"
                    ".cardtitle{font-weight:700;margin-bottom:8px;color:#fff}"
                    ".meta{border-collapse:collapse;width:100%}.meta th{background:#232428;color:#aaa;text-align:left;padding:6px 10px;width:180px}"
                    ".meta td{background:#1e1f22;padding:6px 10px;border-left:1px solid #2a2c30}"
                    ".participants{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0 18px 0}"
                    ".participants .ptitle{width:100%;color:#9ca3af;margin-bottom:2px}"
                    ".chip{display:inline-flex;align-items:center;gap:8px;background:#1e1f22;border:1px solid #2a2c30;border-radius:99px;padding:4px 10px}"
                    ".chip img{width:18px;height:18px;border-radius:50%}"
                    ".msg{display:flex;gap:10px;margin:10px 0}"
                    ".avatar{width:38px;height:38px;border-radius:50%;flex:0 0 38px}"
                    ".bubble{background:#1e1f22;border:1px solid #2a2c30;border-radius:10px;padding:8px 12px;flex:1}"
                    ".head{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}"
                    ".author{font-weight:600;color:#fff}"
                    ".time{color:#8a8e95;font-size:12px}"
                    ".content{white-space:pre-wrap;line-height:1.35}"
                    ".attach{margin-top:6px;color:#cbd5e1}.attach a{color:#93c5fd;text-decoration:none}.attach a:hover{text-decoration:underline}"
                    ".embed{margin-top:8px;border-left:4px solid #5865F2;background:#111214;border:1px solid #2a2c30;border-radius:8px;padding:8px 10px}"
                    ".etitle{font-weight:600;margin-bottom:4px}.etitle a{color:#c7d2fe;text-decoration:none}"
                    ".edesc{color:#d1d5db;margin-bottom:4px}"
                    ".efields{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:6px;margin-top:6px}"
                    ".efield{background:#1b1c1f;border:1px solid #2a2c30;border-radius:6px;padding:6px}"
                    ".fname{font-weight:600;color:#e5e7eb;margin-bottom:2px}.fvalue{color:#cbd5e1}"
                    ".eimg{max-width:100%;border-radius:6px;margin-top:6px}"
                    ".efooter{color:#9ca3af;margin-top:6px;font-size:12px}"
                    "h2{display:none}"
                    "</style></head><body>"
                    f"{header}{part_html}{''.join(messages_html)}</body></html>"
                )

                with open(transcript_path, "w", encoding="utf-8") as f:
                    f.write(html_doc)

                # post to log channel (with (test) prefix if test guild)
                log_ch = guild.get_channel(config.get("log_channel_id"))
                if isinstance(log_ch, discord.TextChannel):
                    test_tag = "(test) " if _is_test_guild(guild.id) else ""
                    await log_ch.send(content=f"{test_tag}📝 Transcript from `{channel.name}`", file=discord.File(transcript_path))

                # after saving one, trim the folder so it doesn't grow forever
                self._prune_transcripts_if_needed()

            except Exception as e:
                print(f"[❌ Transcript Error] {type(e).__name__}: {e}")

        # remove opener if non-staff (so the ticket isn't hanging around for them post-close)
        per_type_roles = []
        if per_type:
            ttype = next((t for t in config.get("ticket_types", []) if t.get("label") == per_type), {})
            per_type_roles = ttype.get("support_role_ids", []) or []
        global_roles = config.get("support_role_ids", []) or []
        combined_roles = set(per_type_roles + global_roles)
        if opener and opener_id:
            masters = _bot_masters()
            is_staff = opener.id in masters or any((guild.get_role(rid) in opener.roles) for rid in combined_roles if guild.get_role(rid))
            if not is_staff:
                try: await channel.set_permissions(opener, overwrite=None)
                except Exception as e: print(f"[⚠️ Remove Opener Error] {type(e).__name__}: {e}")

        # forget that this channel existed, and delete it
        self.open_tickets.pop(str(channel.id), None); save_open_tickets(self.open_tickets)
        try: await channel.delete()
        except Exception as e: print(f"[❌ Channel Deletion Error] {type(e).__name__}: {e}")

    # user left server? close any tickets they still own (save transcript)
    async def autoclose_if_opener(self, member: discord.Member):
        to_close = [int(cid) for cid, rec in self.open_tickets.items()
                    if rec.get("guild_id")==member.guild.id and rec.get("user_id")==member.id]
        for cid in to_close:
            ch = member.guild.get_channel(cid)
            if isinstance(ch, discord.TextChannel):
                try: await self._finalize_close(None, ch, True)
                except Exception as e: print(f"[❌ AutoClose Error] {type(e).__name__}: {e}")
