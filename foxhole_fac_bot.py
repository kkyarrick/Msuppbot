import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
from discord import app_commands
from datetime import datetime, timezone, time
import json
import os

# ============================================================
# CONFIGURATION
# ============================================================

DATA_FILE = "tunnels.json"
USER_FILE = "users.json"
DASH_FILE = "dashboard.json"
ORDERS_FILE = "orders.json"
CONTRIB_FILE = "contributions.json"
SUPPLY_INCREMENT_Dunne = 1500
SUPPLY_INCREMENT_Stowheel = 6000

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("❌ No DISCORD_TOKEN found in environment variables.")

# ============================================================
# ARCHIVE SYSTEM
# ============================================================
from pathlib import Path
ARCHIVE_DIR = Path("data/archives")
def ensure_archive_root():
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
def create_war_archive_folder(timestamp_str: str) -> Path:
    ensure_archive_root()
    folder = ARCHIVE_DIR / timestamp_str
    folder.mkdir(exist_ok=True)
    return folder

def export_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def generate_markdown_report(
    path: Path,
    guild_name: str,
    war_end_time: str,
    facility_count: int,
    tunnel_count: int,
    total_supplies: int,
    sorted_contribs: list,
    guild: discord.Guild
):
    lines = []
    lines.append(f"# 🏁 Foxhole MSUPP — End of War Report\n")
    lines.append(f"**Server:** {guild_name}")
    lines.append(f"**Date:** {war_end_time}")
    lines.append(f"**Facilities Operated:** {facility_count}")
    lines.append(f"**Tunnels Managed:** {tunnel_count}")
    lines.append(f"**Total Supplies Delivered:** {total_supplies:,}\n")
    lines.append("## 🥇 Top Contributors")
    if sorted_contribs:
        for i, (uid, amt) in enumerate(sorted_contribs):
            member = guild.get_member(int(uid))
            name = member.display_name if member else f"User {uid}"
            lines.append(f"{i+1}. **{name}** — {amt:,}")
    else:
        lines.append("_No contributions this war._")
    lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

# ============================================================
# DATA MANAGEMENT
# ============================================================

def load_data(file, default):
    if os.path.exists(file):
        with open(file, "r") as f:
            return json.load(f)
    return default

def save_data(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=4)

def load_orders():
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r") as f:
            try:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
    return {"next_id": 1, "orders": {}}

# Ensure data files exist
for file, default in [
    (DATA_FILE, {}),
    (USER_FILE, {}),
    (DASH_FILE, {}),
    (ORDERS_FILE, {"next_id": 1, "orders": {}})
]:
    if not os.path.exists(file):
        save_data(file, default)

tunnels = load_data(DATA_FILE, {})
users = load_data(USER_FILE, {})
dashboard_info = load_data(DASH_FILE, {})  # {guild_id: {"channel": id, "message": id}}
contributions = load_data(CONTRIB_FILE, {})

# ============================================================
# TUNNEL STRUCTURE MIGRATION (flat -> per-facility)
# ============================================================

def is_nested_tunnel_structure(data: dict) -> bool:
    """Return True if `data` looks like {facility: { 'tunnels': { ... }}}."""
    if not data:
        return False
    for v in data.values():
        if not isinstance(v, dict):
            return False
        if "tunnels" not in v or not isinstance(v["tunnels"], dict):
            return False
    return True


def migrate_flat_tunnels_to_facilities():
    """
    If tunnels are still in the old flat format:
        { "Tunnel A": {...}, "Tunnel B": {...} }
    wrap them into a default facility:
        { "Legacy Facility": { "tunnels": { ... } } }
    """
    global tunnels
    if is_nested_tunnel_structure(tunnels):
        return

    if not tunnels:
        return  # nothing to migrate

    flat = tunnels
    tunnels = {
        "Legacy Facility": {
            "tunnels": flat
        }
    }
    save_data(DATA_FILE, tunnels)

migrate_flat_tunnels_to_facilities()

# ============================================================
# FACILITY / TUNNEL HELPERS
# ============================================================

def get_facility_record(facility_name: str) -> dict:
    """
    Ensure the facility exists in `tunnels` and return its record.
    Structure:
        tunnels[facility_name] = { "tunnels": { <tunnel_name>: { ... } } }
    """
    facility = tunnels.setdefault(facility_name, {})
    if "tunnels" not in facility or not isinstance(facility["tunnels"], dict):
        facility["tunnels"] = facility.get("tunnels", {})
    return facility


def get_facility_tunnels(facility_name: str) -> dict:
    """Convenience: returns the dict of tunnels under a facility."""
    facility = get_facility_record(facility_name)
    return facility["tunnels"]


def find_tunnel(tunnel_name: str):
    """
    Find a tunnel by name across all facilities.

    Returns (facility_name, tunnel_dict) if found, otherwise (None, None).
    """
    for fname, facility in tunnels.items():
        tun_dict = facility.get("tunnels", {})
        if tunnel_name in tun_dict:
            return fname, tun_dict[tunnel_name]
    return None, None


def get_facility_for_channel(guild_id: str, channel_id: int) -> str | None:
    """
    Given a guild + channel/thread id, return the facility bound to that dashboard,
    or None if this channel is not the home dashboard for any facility.
    """
    info = dashboard_info.get(guild_id, {})
    facilities = info.get("facilities", {})
    for fname, fdata in facilities.items():
        if fdata.get("tunnel_channel") == channel_id:
            return fname
    return None

async def tunnel_name_autocomplete_impl(
    interaction: discord.Interaction,
    current: str
):
    """
    Shared autocomplete helper for tunnel name fields.
    - If the current channel is bound to a facility dashboard: only show that facility's tunnels.
    - Otherwise: show all unique tunnel names across facilities.
    """
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    channel = interaction.channel
    facility_name = None

    if guild_id and hasattr(channel, "id"):
        facility_name = get_facility_for_channel(guild_id, channel.id)

    names = []
    if facility_name:
        tun_dict = get_facility_tunnels(facility_name)
        names = list(tun_dict.keys())
    else:
        # Fallback: unique tunnel names across all facilities
        seen = set()
        for fac in tunnels.values():
            for tname in fac.get("tunnels", {}):
                if tname not in seen:
                    seen.add(tname)
                    names.append(tname)

    current_lower = current.lower()
    choices: list[app_commands.Choice[str]] = []
    for tname in names:
        if current_lower in tname.lower():
            choices.append(app_commands.Choice(name=tname, value=tname))
            if len(choices) >= 25:
                break

    return choices

def normalize_dashboard_info():
    """
    Normalize dashboard_info into the new structure:

    dashboard_info[guild_id] = {
        "facilities": {
            "<facility_name>": {
                "tunnel_channel": int,
                "tunnel_message": int
            },
            ...
        },
        "orders_channel": int,
        "orders_message": int,
        "log_channel": int,
        "leaderboard_channel": int,
    }

    Also migrates old top-level tunnel_channel/tunnel_message into a
    single 'Legacy Facility'.
    """
    changed = False

    for gid, info in dashboard_info.items():

        # Fix old tunnel keys
        if "channel" in info:
            info["tunnel_channel"] = info.pop("channel")
            changed = True
        if "message" in info:
            info["tunnel_message"] = info.pop("message")
            changed = True

        # Fix orders dashboard keys
        if "order_channel" in info:
            info["orders_channel"] = info.pop("order_channel")
            changed = True
        if "order_message" in info:
            info["orders_message"] = info.pop("order_message")
            changed = True

        # Ensure facilities sub-dict
        facilities = info.setdefault("facilities", {})

        # If we still have a single, old-style tunnel dashboard at the top level,
        # move it into a 'Legacy Facility' facility.
        if "tunnel_channel" in info or "tunnel_message" in info:
            legacy_name = "Legacy Facility"
            fac_cfg = facilities.setdefault(legacy_name, {})

            if "tunnel_channel" in info:
                fac_cfg["tunnel_channel"] = info.pop("tunnel_channel")
                changed = True

            if "tunnel_message" in info:
                fac_cfg["tunnel_message"] = info.pop("tunnel_message")
                changed = True

            facilities[legacy_name] = fac_cfg
            info["facilities"] = facilities

    if changed:
        save_data(DASH_FILE, dashboard_info)

def catch_up_tunnels():
    """
    Apply offline usage decay if the bot was down for a while.
    Uses per-facility nested tunnel structure.
    """
    now = datetime.now(timezone.utc)
    updated = False

    for facility_name, facility_data in tunnels.items():
        tun_dict = facility_data.get("tunnels", {})

        for tunnel_name, tdata in tun_dict.items():
            usage = tdata.get("usage_rate", 0)
            last_str = tdata.get("last_updated")

            if not last_str:
                tdata["last_updated"] = now.isoformat()
                continue

            try:
                last = datetime.fromisoformat(last_str)
            except Exception:
                tdata["last_updated"] = now.isoformat()
                continue

            hours_passed = (now - last).total_seconds() / 3600

            if hours_passed > 0 and usage > 0:
                tdata["total_supplies"] = max(
                    0,
                    tdata.get("total_supplies", 0) - (usage * hours_passed)
                )
                tdata["last_updated"] = now.isoformat()
                updated = True

    if updated:
        save_data(DATA_FILE, tunnels)

# ============================================================
# HYBRID FACILITY NORMALIZATION (Phase 5 — Step 3A)
# ============================================================

REQUIRED_FACILITY_FIELDS = {
    "facility_name": "",
    "tunnels": {},
    "tunnel_channel": 0,
    "tunnel_message": 0,
    "created_at": "",
    "created_by": "",
    "last_refresh": ""
}

def normalize_facility_record(name: str, record: dict, creator_id: str | None = None):
    """
    Ensures a facility record has all mandatory fields.
    Optional / custom fields are preserved.
    """
    changed = False

    # Always enforce the facility name field
    if "facility_name" not in record:
        record["facility_name"] = name
        changed = True

    for key, default in REQUIRED_FACILITY_FIELDS.items():
        if key not in record:
            # For creator_id override
            if key == "created_by" and creator_id:
                record[key] = str(creator_id)
            # For timestamps
            elif key in ("created_at", "last_refresh"):
                record[key] = datetime.now(timezone.utc).isoformat()
            else:
                # Copy default of correct type
                record[key] = default if not isinstance(default, (dict, list)) else default.copy()
            changed = True

    # Ensure tunnels exists and is a dict
    if not isinstance(record.get("tunnels"), dict):
        record["tunnels"] = {}
        changed = True

    return changed

def normalize_all_facilities():
    """
    Normalizes all facilities in dashboard_info.
    Called at startup.
    """
    changed = False

    for guild_id, info in dashboard_info.items():
        facs = info.get("facilities", {})
        for fac_name, fac_record in facs.items():
            if normalize_facility_record(fac_name, fac_record):
                changed = True
    if changed:
        save_data(DASH_FILE, dashboard_info)

# ============================================================
# GLOBAL PERMISSIONS SYSTEM
# ============================================================

def has_authorized_role(member: discord.Member):
    """
    TEMPORARY GLOBAL LOCK:
    Restrict ALL bot actions to 'Officer'.

    To switch to Verified™ later, simply replace:
        "Officer" → "Verified™"
    """
    return any(r.name == "Verified™" for r in member.roles)


async def interaction_role_guard(interaction: discord.Interaction):
    """
    This protects ALL button/select/modal interactions.
    If user is not authorized → block the action.
    """
    if not has_authorized_role(interaction.user):
        try:
            await interaction.response.send_message(
                "🚫 You do not have permission to perform this action.",
                ephemeral=True
            )
        except discord.InteractionResponded:
            await interaction.followup.send(
                "🚫 You do not have permission to perform this action.",
                ephemeral=True
            )
        return False

    return True


@bot.check
async def global_permission_lock(ctx: commands.Context):
    """
    Global lock for ALL slash commands.
    This means officers-only until changed.
    """
    if isinstance(ctx.user, discord.Member) and has_authorized_role(ctx.user):
        return True

    raise commands.CheckFailure("🚫 You do not have permission to use this command.")

# ============================================================
# DATA LOGGING — Unified System (A2 Format)
# ============================================================

def format_log(
    actor: discord.Member | discord.User,
    action: str,
    target: str | None = None,
    details: str | None = None,
):
    """Return a perfectly formatted log line according to A2 standard."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    base = f"🧾 `{timestamp}` — {actor.display_name} {action}"

    if target:
        base += f" **{target}**"

    if details:
        base += f" ({details})"

    return base


async def get_fac_log_thread(guild: discord.Guild):
    """Returns the FAC Logs thread, creating it if missing."""
    guild_id = str(guild.id)
    log_channel_id = dashboard_info.get(guild_id, {}).get("log_channel")
    if not log_channel_id:
        return None

    log_channel = guild.get_channel(log_channel_id)
    if not log_channel:
        return None

    # Find existing thread
    thread = discord.utils.get(log_channel.threads, name="FAC Logs")
    if thread:
        return thread

    # Create thread if missing
    thread = await log_channel.create_thread(
        name="FAC Logs",
        type=discord.ChannelType.public_thread
    )
    await thread.send("🧾 **FAC Audit Log Thread Created**")
    return thread


# ------------------------------------------------------------
# BATCHING for SUPPLY ADDITIONS ONLY
# ------------------------------------------------------------
log_buffer = {}  # (guild_id, user_id, tunnel_name, date) → {amount, last_action}


async def flush_supply_logs():
    """Flushes batched supply submissions immediately."""
    now = datetime.now(timezone.utc)

    for key, entry in list(log_buffer.items()):
        guild_id, user_id, tunnel_name, date_key = key
        amount = entry["amount"]

        guild = discord.utils.get(bot.guilds, id=guild_id)
        if not guild:
            continue

        thread = await get_fac_log_thread(guild)
        if not thread:
            continue

        user = guild.get_member(user_id) or await bot.fetch_user(user_id)

        line = format_log(
            actor=user,
            action="added supplies to",
            target=tunnel_name,
            details=f"{amount:,} total today"
        )

        await thread.send(line)
        del log_buffer[key]


# ------------------------------------------------------------
# Unified Log Entry Function (Supply, Orders, Admin, Everything)
# ------------------------------------------------------------
async def log_action(
    guild: discord.Guild,
    actor: discord.Member | discord.User,
    action: str,
    target_name: str | None = None,
    amount: int | None = None,
    details: str | None = None,
):
    """Universal logger for tunnels, orders, and admin actions."""

    try:
        # Get logging location
        thread = await get_fac_log_thread(guild)
        if not thread:
            return

        # ------------------------------------------------------------
        # SUPPLY ACTION? → batch it
        # ------------------------------------------------------------
        is_supply = action.lower() in [
            "added supplies",
            "submitted stacks",
            "1500 added",
            "supply added",
            "stack submission",
        ]

        if is_supply:
            key = (
                guild.id,
                actor.id,
                target_name,
                datetime.now(timezone.utc).strftime("%Y-%m-%d")
            )

            if key not in log_buffer:
                log_buffer[key] = {"amount": 0}

            if amount:
                log_buffer[key]["amount"] += amount

            return  # don’t immediately send, wait or flush on change

        # ------------------------------------------------------------
        # NON-SUPPLY ACTIONS → send instantly
        # ------------------------------------------------------------
        line = format_log(
            actor=actor,
            action=action,
            target=target_name,
            details=details
        )
        await thread.send(line)

    except Exception as e:
        print(f"[LOGGING ERROR] {e}")
       
def log_contribution(user_id: str, action: str, amount: int | float = 0, tunnel: str | None = None):
    """Record player contributions for analytics."""
    user_id = str(user_id)
    now = datetime.now(timezone.utc).isoformat()

    if user_id not in contributions:
        contributions[user_id] = {
            "total_supplies": 0,
            "actions": []
        }

    # Add to running totals if relevant
    if action.lower() in ["add supplies", "submit stacks", "1500 (done)"]:

        contributions[user_id]["total_supplies"] += amount

    # Log each event
    contributions[user_id]["actions"].append({
        "timestamp": now,
        "action": action,
        "tunnel": tunnel,
        "amount": amount
    })

    save_data(CONTRIB_FILE, contributions)

class StackSubmitModal(discord.ui.Modal, title="Submit Stacks"):
    tunnel_name: str

    def __init__(self, tunnel_name):
        super().__init__(title=f"Submit stacks to {tunnel_name}")
        self.tunnel_name = tunnel_name
        self.amount = discord.ui.TextInput(
            label="Number of stacks (100 each)",
            placeholder="e.g., 5 for 500 supplies",
            required=True
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        stacks = int(self.amount.value)
        amount = stacks * 100

        guild_id = str(interaction.guild.id)
        channel_id = interaction.channel.id

        # Prefer facility bound to this channel
        facility_name = get_facility_for_channel(guild_id, channel_id)
        tdata = None

        if facility_name:
            fac_rec = get_facility_record(facility_name)
            tdata = fac_rec["tunnels"].get(self.tunnel_name)
            if not tdata:
                # Tunnel exists elsewhere?
                other_fac, _ = find_tunnel(self.tunnel_name)
                if other_fac:
                    await interaction.response.send_message(
                        f"❌ Tunnel **{self.tunnel_name}** belongs to facility "
                        f"**{other_fac}**. Please use that facility's dashboard thread.",
                        ephemeral=True
                    )
                    return
        else:
            facility_name, tdata = find_tunnel(self.tunnel_name)

        if not tdata:
            await interaction.response.send_message(
                f"❌ Tunnel **{self.tunnel_name}** not found.",
                ephemeral=True
            )
            return

        tdata["total_supplies"] = tdata.get("total_supplies", 0) + amount

        user_id = str(interaction.user.id)
        users[user_id] = users.get(user_id, 0) + amount
        save_data(DATA_FILE, tunnels)
        save_data(USER_FILE, users)

        log_contribution(interaction.user.id, "submit stacks", amount, self.tunnel_name)
        await log_action(
            interaction.guild,
            interaction.user,
            "added supplies",
            target_name=f"[{facility_name}] {self.tunnel_name}" if facility_name else self.tunnel_name,
            amount=amount
        )

        await refresh_dashboard(interaction.guild, facility_name)
        await interaction.response.send_message(
            f"🪣 Submitted {amount} supplies ({stacks} stacks) to **{self.tunnel_name}**.",
            ephemeral=True
        )

# ============================================================
# DASHBOARD VIEW
# ============================================================

class TunnelButton(Button):
    def __init__(self, tunnel):
        super().__init__(
            label=f"{tunnel} + Msupps",
            style=discord.ButtonStyle.green,
            custom_id=f"tunnel_{tunnel.lower().replace(' ', '_')}"
        )
        self.tunnel = tunnel

    async def callback(self, interaction: discord.Interaction):
        if not await interaction_role_guard(interaction):
            return

        view = discord.ui.View(timeout=30)

        guild_id = str(interaction.guild.id)
        if guild_id not in dashboard_info:
            dashboard_info[guild_id] = {}

        async def dunne_callback(interaction: discord.Interaction):

            user_id = str(interaction.user.id)
            users[user_id] = users.get(user_id, 0) + SUPPLY_INCREMENT_Dunne
            guild_id = str(interaction.guild.id)
            channel_id = interaction.channel.id

            facility_name = get_facility_for_channel(guild_id, channel_id)
            tdata = None

            if facility_name:
                fac_rec = get_facility_record(facility_name)
                tdata = fac_rec["tunnels"].get(self.tunnel)
                if not tdata:
                    other_fac, _ = find_tunnel(self.tunnel)
                    if other_fac:
                        await interaction.response.edit_message(
                            content=(
                                f"❌ Tunnel **{self.tunnel}** belongs to facility "
                                f"**{other_fac}**. Please use that facility's dashboard thread."
                            ),
                            view=None
                        )
                        return
            else:
                facility_name, tdata = find_tunnel(self.tunnel)

            if not tdata:
                await interaction.response.edit_message(
                    content=f"❌ Tunnel **{self.tunnel}** no longer exists.",
                    view=None
                )
                return

            tdata["total_supplies"] = tdata.get("total_supplies", 0) + SUPPLY_INCREMENT_Dunne
            save_data(DATA_FILE, tunnels)
            save_data(USER_FILE, users)

            log_contribution(interaction.user.id, "1500 (Done)", SUPPLY_INCREMENT_Dunne, self.tunnel)
            await log_action(
                interaction.guild,
                interaction.user,
                "added supplies",
                target_name=f"[{facility_name}] {self.tunnel}" if facility_name else self.tunnel,
                amount=SUPPLY_INCREMENT_Dunne
            )

            await refresh_dashboard(interaction.guild, facility_name)
            await interaction.response.edit_message(
                content=f"🪣 Added {SUPPLY_INCREMENT_Dunne} supplies to **{self.tunnel}**!",
                view=None
            )

        async def Stowheel_callback(interaction: discord.Interaction):

            user_id = str(interaction.user.id)
            users[user_id] = users.get(user_id, 0) + SUPPLY_INCREMENT_Stowheel
            guild_id = str(interaction.guild.id)
            channel_id = interaction.channel.id

            facility_name = get_facility_for_channel(guild_id, channel_id)
            tdata = None

            if facility_name:
                fac_rec = get_facility_record(facility_name)
                tdata = fac_rec["tunnels"].get(self.tunnel)
                if not tdata:
                    other_fac, _ = find_tunnel(self.tunnel)
                    if other_fac:
                        await interaction.response.edit_message(
                            content=(
                                f"❌ Tunnel **{self.tunnel}** belongs to facility "
                                f"**{other_fac}**. Please use that facility's dashboard thread."
                            ),
                            view=None
                        )
                        return
            else:
                facility_name, tdata = find_tunnel(self.tunnel)

            if not tdata:
                await interaction.response.edit_message(
                    content=f"❌ Tunnel **{self.tunnel}** no longer exists.",
                    view=None
                )
                return

            tdata["total_supplies"] = tdata.get("total_supplies", 0) + SUPPLY_INCREMENT_Stowheel
            save_data(DATA_FILE, tunnels)
            save_data(USER_FILE, users)

            log_contribution(interaction.user.id, "1500 (Done)", SUPPLY_INCREMENT_Stowheel, self.tunnel)
            await log_action(
                interaction.guild,
                interaction.user,
                "added supplies",
                target_name=f"[{facility_name}] {self.tunnel}" if facility_name else self.tunnel,
                amount=SUPPLY_INCREMENT_Stowheel
            )

            await refresh_dashboard(interaction.guild, facility_name)
            await interaction.response.edit_message(
                content=f"🪣 Added {SUPPLY_INCREMENT_Stowheel} supplies to **{self.tunnel}**!",
                view=None
            )


        async def stack_callback(interaction: discord.Interaction):
            modal = StackSubmitModal(self.tunnel)
            await interaction.response.send_modal(modal)

        view.add_item(discord.ui.Button(label="1500 (Dunne)", style=discord.ButtonStyle.green))
        view.children[0].callback = dunne_callback
        view.add_item(discord.ui.Button(label="6000 (Stowheel)", style=discord.ButtonStyle.green))
        view.children[1].callback = Stowheel_callback       
        view.add_item(discord.ui.Button(label="Submit Stacks (x100)", style=discord.ButtonStyle.blurple))
        view.children[2].callback = stack_callback

        await interaction.response.send_message(
            f"How much would you like to submit to **{self.tunnel}**?",
            ephemeral=True,
            view=view
        )

# ============================================================
# DASHBOARD PAGINATION SYSTEM
# ============================================================

class DashboardPaginator(discord.ui.View):
    """Combined paginated + interactive dashboard for tunnels."""
    def __init__(self, tunnels, facility_name: str | None = None, per_page=8):
        super().__init__(timeout=None)
        self.facility_name = facility_name
        self.tunnels = list(tunnels.items())
        self.per_page = per_page
        self.page = 0
        self.total_pages = max(1, -(-len(self.tunnels) // self.per_page))
        self.build_page_buttons()

    # -----------------------------------------
    # Build the embed for the current page
    # -----------------------------------------
    def build_page_embed(self):
        title = "🛠 Foxhole FAC Dashboard"
        if self.facility_name:
            title += f" — {self.facility_name}"
        title += f" — Page {self.page + 1}/{self.total_pages}"

        embed = discord.Embed(
            title=title,
            color=0x00ff99,
            timestamp=datetime.now(timezone.utc)
        )

        start = self.page * self.per_page
        end = start + self.per_page
        subset = self.tunnels[start:end]
        
        for name, data in subset:
            supplies = int(data.get("total_supplies", 0))
            usage = int(data.get("usage_rate", 0))
            hours = int(supplies / usage) if usage > 0 else 0

            # Status (same logic as original)
            status = "🟢" if hours >= 24 else "🟡" if hours >= 4 else "🔴"

            if usage > 0:
                value = (
                    f"**Supplies:** {supplies:,} | "
                    f"**Usage:** {usage}/hr | "
                    f"{status} **{hours}h**"
                )
            else:
                value = (
                    f"**Supplies:** {supplies:,} | "
                    f"**Usage:** 0/hr | ⚪ **Stable**"
                )

            embed.add_field(name=name, value=value, inline=False)

        embed.set_footer(
            text="Updated every 2 minutes. Use the buttons below to add supplies or navigate pages."
        )
        return embed

    # -----------------------------------------
    # Rebuild tunnel buttons dynamically
    # -----------------------------------------
    def build_page_buttons(self):
        """Clear and rebuild tunnel buttons for current page."""
        self.clear_items()

        # navigation buttons
        nav_buttons = [
            discord.ui.Button(label="⏮️", style=discord.ButtonStyle.gray, custom_id="nav_first", row=0),
            discord.ui.Button(label="◀️", style=discord.ButtonStyle.gray, custom_id="nav_prev", row=0),
            discord.ui.Button(label="▶️", style=discord.ButtonStyle.gray, custom_id="nav_next", row=0),
            discord.ui.Button(label="⏭️", style=discord.ButtonStyle.gray, custom_id="nav_last", row=0),
        ]
        for b in nav_buttons:
            self.add_item(b)

        # tunnel buttons for visible subset
        start = self.page * self.per_page
        end = start + self.per_page
        tunnels_per_row = 4
        for i, (name, _) in enumerate(self.tunnels[start:end]):
            button = TunnelButton(name)
            button.row = 1 + (i // tunnels_per_row)
            self.add_item(button)

    # -----------------------------------------
    # Handle navigation click manually
    # -----------------------------------------
    async def interaction_check(self, interaction: discord.Interaction):
        cid = interaction.data.get("custom_id")
        if not cid:
            return True

        # Handle navigation clicks
        if cid.startswith("nav_"):
            old_page = self.page
            if cid == "nav_first":
                self.page = 0
            elif cid == "nav_prev" and self.page > 0:
                self.page -= 1
            elif cid == "nav_next" and self.page < self.total_pages - 1:
                self.page += 1
            elif cid == "nav_last":
                self.page = self.total_pages - 1

            if old_page != self.page:
                self.build_page_buttons()
                await interaction.response.edit_message(embed=self.build_page_embed(), view=self)
            else:
                await interaction.response.defer()
            return False

        return True

class MsuppDashboardModal(discord.ui.Modal, title="Create MSUPP Facility"):
    def __init__(self, suggested_name: str, channel_id: int, guild_id: int):
        super().__init__(title="Create MSUPP Facility")
        self.channel_id = channel_id
        self.guild_id = guild_id

        self.facility_name_input = discord.ui.TextInput(
            label="Facility name",
            placeholder=suggested_name,
            required=True,
            max_length=100
        )
        self.add_item(self.facility_name_input)

    async def on_submit(self, interaction: discord.Interaction):
        facility_name = self.facility_name_input.value.strip() or self.facility_name_input.placeholder
        guild_id_str = str(self.guild_id)
        guild = interaction.guild
        channel = interaction.channel

        facility_record = get_facility_record(facility_name)
        fac_tunnels = facility_record["tunnels"]

        if guild_id_str not in dashboard_info:
            dashboard_info[guild_id_str] = {}
        info = dashboard_info[guild_id_str]
        facilities = info.setdefault("facilities", {})

        paginator = DashboardPaginator(fac_tunnels, facility_name=facility_name)
        msg = await channel.send(embed=paginator.build_page_embed(), view=paginator)

        facilities[facility_name] = {
            "tunnel_channel": channel.id,
            "tunnel_message": msg.id
        }
        info["facilities"] = facilities
        dashboard_info[guild_id_str] = info
        normalize_facility_record(
            facility_name,
            facilities[facility_name],
            creator_id=interaction.user.id
        )
        save_data(DASH_FILE, dashboard_info)

        await interaction.response.send_message(
            f"✅ MSUPP dashboard for **{facility_name}** created in {channel.mention}.",
            ephemeral=True
        )


# ============================================================
# EMBED BUILDERS
# ============================================================

def build_dashboard_embed():
    embed = discord.Embed(
        title="🛠 Foxhole FAC Dashboard",
        color=0x00ff99,
        timestamp=datetime.now(timezone.utc)
    )

    if not tunnels:
        embed.description = "No tunnels added yet. Use `/addtunnel`."
        return embed

    # Sort alphabetically or numerically if your tunnels are numbered
    sorted_tunnels = dict(sorted(tunnels.items(), key=lambda x: x[0].lower()))

    for name, data in sorted_tunnels.items():
        supplies = int(data.get("total_supplies", 0))
        usage = int(data.get("usage_rate", 0))
        hours = int(supplies / usage) if usage > 0 else 0

        # Traffic light system
        if hours >= 24:
            status = "🟢"
        elif hours >= 4:
            status = "🟡"
        else:
            status = "🔴"

        # Compact field layout
        tunnel_field = f"**{name}**\n`{usage}/hr`"
        supplies_field = f"**{supplies:,}**"
        status_field = f"{status} **{hours}h**"

        # Add fields as inlines to form a 3-column dashboard
        embed.add_field(name="Tunnel / Usage", value=tunnel_field, inline=True)
        embed.add_field(name="Supplies", value=supplies_field, inline=True)
        embed.add_field(name="Status", value=status_field, inline=True)
        embed.add_field(name="\u200b", value="━━━━━━━━━━━", inline=False)

    embed.set_footer(text="🕒 Updated every 2 minutes.")
    return embed

async def refresh_msupp_dashboard(guild: discord.Guild, facility_name: str):
    """Edit or recreate the persistent tunnel dashboard message for a single facility."""
    guild_id = str(guild.id)
    info = dashboard_info.get(guild_id, {})
    facilities = info.get("facilities", {})
    fac_cfg = facilities.get(facility_name)

    # Update facility metadata
    fac_cfg["last_refresh"] = datetime.now(timezone.utc).isoformat()
    save_data(DASH_FILE, dashboard_info)

    # Normalize facility structure before building UI
    if normalize_facility_record(facility_name, fac_cfg):
        save_data(DASH_FILE, dashboard_info)

    if not fac_cfg:
        print(f"[INFO] No facility '{facility_name}' dashboard info for guild {guild.name}")
        return

    channel_id = fac_cfg.get("tunnel_channel")
    msg_id = fac_cfg.get("tunnel_message")

    if not channel_id or not msg_id:
        print(f"[INFO] Facility '{facility_name}' missing tunnel_channel/message in guild {guild.name}")
        return

    channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
    if not channel:
        print(f"[WARN] Tunnel dashboard channel missing for facility '{facility_name}' in {guild.name}")
        return

    facility_tunnels = get_facility_tunnels(facility_name)
    paginator = DashboardPaginator(facility_tunnels, facility_name=facility_name)

    try:
        msg = await channel.fetch_message(msg_id)
        await msg.edit(embed=paginator.build_page_embed(), view=paginator)
    except discord.NotFound:
        new_msg = await channel.send(embed=paginator.build_page_embed(), view=paginator)
        fac_cfg["tunnel_channel"] = new_msg.channel.id
        fac_cfg["tunnel_message"] = new_msg.id
        facilities[facility_name] = fac_cfg
        info["facilities"] = facilities
        dashboard_info[guild_id] = info
        save_data(DASH_FILE, dashboard_info)
        print(f"[RECOVERY] Dashboard for facility '{facility_name}' recreated in {guild.name}")
    except Exception as inner_e:
        print(f"[FATAL] Could not recreate dashboard for facility '{facility_name}' in {guild.name}: {inner_e}")


async def refresh_dashboard(guild: discord.Guild, facility_name: str | None = None):
    """
    Backwards-compatible wrapper:
    - If facility_name is provided, refresh that facility.
    - Else, refresh all known facilities in this guild.
    """
    guild_id = str(guild.id)
    info = dashboard_info.get(guild_id, {})
    facilities = info.get("facilities", {})
    if facility_name:
        await refresh_msupp_dashboard(guild, facility_name)
        return

    for fname in facilities.keys():
        await refresh_msupp_dashboard(guild, fname)

# ============================================================
# ORDER DASHBOARD VIEW
# ============================================================

class OrderActionView(discord.ui.View):
    """Interactive buttons for managing a specific order."""
    def __init__(self, order_id: str):
        super().__init__(timeout=60)
        self.order_id = order_id

        self.add_item(discord.ui.Button(label="Claim", style=discord.ButtonStyle.blurple, custom_id=f"claim_{order_id}"))
        self.add_item(discord.ui.Button(label="Update", style=discord.ButtonStyle.green, custom_id=f"update_{order_id}"))
        self.add_item(discord.ui.Button(label="Complete", style=discord.ButtonStyle.gray, custom_id=f"complete_{order_id}"))
        self.add_item(discord.ui.Button(label="Delete", style=discord.ButtonStyle.red, custom_id=f"delete_{order_id}"))

    async def interaction_check(self, interaction: discord.Interaction):
        if not has_authorized_role(interaction.user):
            await interaction.response.send_message("🚫 You are not authorized for order management.", ephemeral=True)
            return False
        return True

    async def on_error(self, error, item, interaction):
        print(f"[ERROR] Dashboard button failed: {error}")
        await interaction.response.send_message("⚠️ An error occurred while processing that action.", ephemeral=True)

# ------------------------------------------------------------
# Dashboard Builder
# ------------------------------------------------------------
def build_order_dashboard():
    """Build the dashboard embed summarizing all current orders."""
    embed = discord.Embed(
        title="📦 Foxhole FAC Orders Dashboard",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    if not orders_data["orders"]:
        embed.description = "No active orders. Use `/order_create` to start a new one."
        return embed

    header = "**ID | Item | Qty | Status | Priority | Claimed By**\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    lines = []

    for oid, o in orders_data["orders"].items():
        status = o["status"]
        priority = o.get("priority", "Normal")
        item = o["item"]
        qty = o["quantity"]
        claimed = "-"
        if o.get("claimed_by"):
            try:
                claimed_user = bot.get_user(int(o["claimed_by"])) or f"<@{o['claimed_by']}>"
                claimed = claimed_user.display_name if hasattr(claimed_user, "display_name") else claimed_user
            except Exception:
                claimed = "Unknown"

        # Add colored emoji for priority
        priority_icon = {"High": "🔴", "Normal": "🟡", "Low": "🟢"}.get(priority, "🟢")
        lines.append(f"**#{oid}** | {item} | {qty} | {status} | {priority_icon} {priority} | {claimed}")

    embed.description = f"{header}\n" + "\n".join(lines)
    embed.set_footer(text="🔁 Updated automatically every 5 minutes.")
    return embed

# ------------------------------------------------------------
# Refresh Order Dashboard
# ------------------------------------------------------------
async def refresh_order_dashboard(guild: discord.Guild):
    """Updates or recreates the order dashboard message if needed."""
    gid = str(guild.id)
    info = dashboard_info.get(gid, {})

    channel_id = info.get("orders_channel")
    message_id = info.get("orders_message")

    if not channel_id or not message_id:
        print(f"[INFO] No order dashboard data found for guild {guild.name}.")
        return

    channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
    if not channel:
        print(f"[WARN] Orders channel missing for guild {guild.name}.")
        return

    try:
        msg = await channel.fetch_message(message_id)
        await msg.edit(embed=build_clickable_order_dashboard(), view=OrderDashboardView())
        #print(f"[OK] Refreshed order dashboard for {guild.name}.")
    except discord.NotFound:
        # The message was deleted — recreate it
        new_msg = await channel.send(embed=build_clickable_order_dashboard(), view=OrderDashboardView())
        dashboard_info[gid]["orders_channel"] = channel.id
        dashboard_info[gid]["orders_message"] = new_msg.id
        save_data(DASH_FILE, dashboard_info)
        print(f"[INFO] Recreated order dashboard in {channel.name}.")
    except Exception as e:
        print(f"[ERROR] Failed to refresh order dashboard in {guild.name}: {e}")

# ============================================================
# AUTO-REFRESH ORDERS DASHBOARD (every 5 minutes)
# ============================================================

@tasks.loop(minutes=5)
async def refresh_orders_loop():
    """Refresh the interactive orders dashboard every 5 minutes."""
    for guild in bot.guilds:
        # Look up where the dashboard was last posted
        info = dashboard_info.get(str(guild.id), {})
        channel_id = info.get("orders_channel")
        message_id = info.get("orders_message")

        if not channel_id or not message_id:
            continue  # nothing to refresh yet

        channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
        if not channel:
            continue

        try:
            msg = await channel.fetch_message(message_id)
            view = OrderDashboardView()
            embed = build_clickable_order_dashboard()
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            # Dashboard message no longer exists
            continue
        except Exception as e:
            print(f"[ORDER DASHBOARD REFRESH ERROR] {e}")

@refresh_orders_loop.before_loop
async def before_refresh_orders_loop():
    await bot.wait_until_ready()

# ============================================================
# INTERACTIVE ORDER STATUS DROPDOWN
# ============================================================

class OrderStatusSelect(discord.ui.Select):
    """Dropdown to update an order's status directly."""
    def __init__(self, order_id: str):
        self.order_id = order_id

        options = [
            discord.SelectOption(label="Order Placed", emoji="📦", description="Order has been placed."),
            discord.SelectOption(label="Order Claimed", emoji="🧰", description="Order has been claimed."),
            discord.SelectOption(label="Order Started", emoji="⚙️", description="Work on the order has begun."),
            discord.SelectOption(label="In Progress", emoji="🚧", description="Order is currently being worked on."),
            discord.SelectOption(label="Ready for Collection", emoji="📦", description="Order is ready to be picked up."),
            discord.SelectOption(label="Complete", emoji="✅", description="Order has been completed.")
        ]

        super().__init__(
            placeholder="Select a new order status...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"status_select_{order_id}"
        )

    async def callback(self, interaction: discord.Interaction):
        """Handle status updates when a selection is made."""
        new_status = self.values[0]
        order = orders_data["orders"].get(self.order_id)

        if not order:
            await interaction.response.send_message(
                f"❌ Order **#{self.order_id}** not found.",
                ephemeral=True
            )
            return

        # Update the order record
        old_status = order.get("status", "Unknown")
        order["status"] = new_status
        order["timestamps"]["last_update"] = datetime.now(timezone.utc).isoformat()
        save_orders()

        # Log the change
        await log_action(
            interaction.guild,
            interaction.user,
            "updated order status",
            target_name=f"#{self.order_id}",
            details=f"{old_status} → {new_status}"
        )

        # Refresh dashboard view
        await refresh_order_dashboard(interaction.guild)

        # Notify user
        await interaction.response.send_message(
            f"✅ Order **#{self.order_id}** updated to **{new_status}**.",
            ephemeral=True
        )


class OrderStatusSelectView(discord.ui.View):
    """A simple view container for the order status dropdown."""
    def __init__(self, order_id: str):
        super().__init__(timeout=60)
        self.add_item(OrderStatusSelect(order_id))

class SingleOrderView(discord.ui.View):
    """Interactive buttons for a single order."""
    def __init__(self, order_id: str):
        super().__init__(timeout=60)
        self.order_id = order_id

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.blurple)
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        if not has_authorized_role(interaction.user):
            await interaction.followup.send("🚫 Unauthorized.", ephemeral=True)
            return

        order = orders_data["orders"].get(self.order_id)
        if not order:
            await interaction.followup.send(f"❌ Order #{self.order_id} not found.", ephemeral=True)
            return

        order["claimed_by"] = str(interaction.user.id)
        order["status"] = "Order Claimed"
        order["timestamps"]["claimed"] = datetime.now(timezone.utc).isoformat()
        save_orders()

        await log_action(
            interaction.guild,
            interaction.user,
            "claimed order",
            target_name=f"#{self.order_id}",
            details=f"{order['item']} x{order['quantity']}"
        )
        await refresh_order_dashboard(interaction.guild)
        await interaction.followup.send(f"🛠 Order **#{self.order_id}** claimed successfully.", ephemeral=True)

    # ✅ Fixed Update Button – Opens Dropdown
    @discord.ui.button(label="Update Status", style=discord.ButtonStyle.green)
    async def update_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Opens a dropdown to update order status."""
        if not has_authorized_role(interaction.user):
            await interaction.response.send_message("🚫 Unauthorized.", ephemeral=True)
            return

        await interaction.response.send_message(
            "📝 Select a new status from the dropdown below:",
            view=OrderStatusSelectView(self.order_id),
            ephemeral=True
        )

    @discord.ui.button(label="Mark Complete", style=discord.ButtonStyle.gray)
    async def complete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        if not has_authorized_role(interaction.user):
            await interaction.followup.send("🚫 Unauthorized.", ephemeral=True)
            return

        order = orders_data["orders"].get(self.order_id)
        if not order:
            await interaction.followup.send(f"❌ Order #{self.order_id} not found.", ephemeral=True)
            return

        order["status"] = "Complete"
        order["timestamps"]["completed"] = datetime.now(timezone.utc).isoformat()
        save_orders()

        await log_action(
            interaction.guild,
            interaction.user,
            "marked order complete",
            target_name=f"#{self.order_id}",
            details=f"{order['item']} x{order['quantity']}"
        )

        await refresh_order_dashboard(interaction.guild)
        await interaction.followup.send(f"✅ Order **#{self.order_id}** marked complete.", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.red)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
        if not officer_role or officer_role not in interaction.user.roles:
            await interaction.followup.send("🚫 Only Officers can delete orders.", ephemeral=True)
            return

        if self.order_id not in orders_data["orders"]:
            await interaction.followup.send(f"❌ Order #{self.order_id} not found.", ephemeral=True)
            return

        deleted = orders_data["orders"].pop(self.order_id)
        save_orders()

        await log_action(
            interaction.guild,
            interaction.user,
            "deleted order",
            target_name=f"#{self.order_id}",
            details=f"{deleted['item']} x{deleted['quantity']}"
        )
        await refresh_order_dashboard(interaction.guild)
        await interaction.followup.send(f"🗑️ Order **#{self.order_id}** deleted.", ephemeral=True)
        
# ============================================================
# CLICKABLE ORDER DASHBOARD
# ============================================================

class OrderButton(discord.ui.Button):
    """Button representing a single order in the dashboard."""
    def __init__(self, order_id: str, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.gray, custom_id=f"order_{order_id}")
        self.order_id = order_id

    async def callback(self, interaction: discord.Interaction):
        if not await interaction_role_guard(interaction):
            return
        """When an order button is clicked, show detailed interactive view."""
        order = orders_data["orders"].get(self.order_id)
        if not order:
            await interaction.response.send_message(f"❌ Order **#{self.order_id}** not found.", ephemeral=True)
            return
            
        # Calculate how long ago the order was created
        created_time = datetime.fromisoformat(order["timestamps"]["created"])
        elapsed = datetime.now(timezone.utc) - created_time
        hours_ago = int(elapsed.total_seconds() // 3600)
        minutes_ago = int((elapsed.total_seconds() % 3600) // 60)
        time_str = f"{hours_ago}h {minutes_ago}m ago" if hours_ago > 0 else f"{minutes_ago}m ago"

        embed = discord.Embed(
            title=f"🧾 Order #{self.order_id}: {order['item']} x{order['quantity']}",
            color=discord.Color.blurple(),
            description=(
                f"**Priority:** {order['priority']}\n"
                f"**Status:** {order['status']}\n"
                f"**Location:** {order.get('location', 'Unknown')}\n"
                f"**Requested by:** <@{order['requested_by']}>\n"
                f"**Claimed by:** {('<@' + order['claimed_by'] + '>') if order['claimed_by'] else '—'}\n"
                f"**Placed:** {time_str}"
            ),
            timestamp=datetime.now(timezone.utc)
        )

        await interaction.response.send_message(embed=embed, view=SingleOrderView(self.order_id), ephemeral=True)


class OrderDashboardView(discord.ui.View):
    """Dynamic dashboard view with clickable order buttons."""
    def __init__(self):
        super().__init__(timeout=None)
        self.build_buttons()

    def build_buttons(self):
        self.clear_items()
        if not orders_data["orders"]:
            return

        for oid, o in orders_data["orders"].items():
            label = f"#{oid}"
            self.add_item(OrderButton(oid, label))


# ============================================================
# BUILD EMBED (Updated to show clickable buttons)
# ============================================================
def build_clickable_order_dashboard():
    """Clean, modern order dashboard to match tunnel aesthetic."""
    embed = discord.Embed(
        title="📦 Foxhole FAC Orders Dashboard",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    if not orders_data["orders"]:
        embed.description = "No active orders. Use `/order_create` to add one."
        return embed

    lines = []
    for oid, o in orders_data["orders"].items():
        item = o["item"]
        qty = o["quantity"]
        priority = o.get("priority", "Normal")
        status = o["status"]
        claimed = f"<@{o['claimed_by']}>" if o.get("claimed_by") else "—"

        priority_icon = {"High": "🔴", "Normal": "🟡", "Low": "🟢"}.get(priority, "🟢")
        status_icon = {
            "Order Placed": "🕓",
            "Order Claimed": "🟦",
            "Order Started": "🧰",
            "In Progress": "⚙️",
            "Ready for Collection": "📦",
            "Complete": "✅"
        }.get(status, "📋")

        lines.append(
            f"**#{oid}** {item} x{qty} | {priority_icon} **{priority}** | "
            f"{status_icon} {status} | 👤 {claimed}"
        )

    embed.description = "\n".join(lines)
    embed.set_footer(text="💡 Click an Order ID below to manage it.")
    return embed

# ============================================================
# BOT EVENTS
# ============================================================

@bot.event
async def on_ready():
    normalize_dashboard_info()
    normalize_all_facilities()
    catch_up_tunnels()  # ✅ simulate supply loss while offline
    await bot.tree.sync()
    print(f"🔁 Synced slash commands for {len(bot.tree.get_commands())} commands.")
    print(f"✅ Logged in as {bot.user}")
    weekly_leaderboard.start()
    refresh_dashboard_loop.start()
    refresh_orders_loop.start()
    flush_log_buffer.start()


# ============================================================
# COMMANDS
# ============================================================

@bot.tree.command(name="addtunnel", description="Add a new tunnel.")
async def addtunnel(interaction: discord.Interaction, name: str, total_supplies: int, usage_rate: int, location: str = "Unknown"):
    await interaction.response.defer(ephemeral=True)

    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
        return    

    guild_id = str(interaction.guild_id)
    channel_id = interaction.channel.id

    # Try to bind to a facility based on this channel, else pick a sensible default
    facility_name = get_facility_for_channel(guild_id, channel_id)
    if not facility_name:
        if tunnels:
            # If we already have facilities, default to the first one
            facility_name = next(iter(tunnels.keys()))
        else:
            # New guild / no facilities yet: use channel/thread name as initial facility name
            facility_name = interaction.channel.name or "New Facility"

    facility_record = get_facility_record(facility_name)
    fac_tunnels = facility_record["tunnels"]

    if name in fac_tunnels:
        await interaction.followup.send(
            f"❌ A tunnel named **{name}** already exists in facility **{facility_name}**.\n"
            f"Use `/updatetunnel` to modify it instead.",
            ephemeral=True
        )
        return

    fac_tunnels[name] = {
        "total_supplies": total_supplies,
        "usage_rate": usage_rate,
        "location": location,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_data(DATA_FILE, tunnels)

    if guild_id not in dashboard_info:
        dashboard_info[guild_id] = {}
    info = dashboard_info[guild_id]
    facilities = info.setdefault("facilities", {})

    fac_cfg = facilities.get(facility_name)
    if not fac_cfg or not fac_cfg.get("tunnel_message"):
        # First dashboard instance for this facility: create and store it
        paginator = DashboardPaginator(fac_tunnels, facility_name=facility_name)
        msg = await interaction.followup.send(embed=paginator.build_page_embed(), view=paginator)

        facilities[facility_name] = {
            "tunnel_channel": msg.channel.id,
            "tunnel_message": msg.id
        }
        info["facilities"] = facilities
        dashboard_info[guild_id] = info
        save_data(DASH_FILE, dashboard_info)
    else:
        await log_action(
            interaction.guild,
            interaction.user,
            "added new tunnel",
            target_name=f"[{facility_name}] {name}" if facility_name else name,
            amount=total_supplies,
            details=f"Usage: {usage_rate}/hr"
        )

        await refresh_msupp_dashboard(interaction.guild, facility_name)
        await interaction.followup.send(
            f"✅ Tunnel **{name}** added to facility **{facility_name}** and dashboard updated.",
            ephemeral=True
        )

@bot.tree.command(name="addsupplies", description="Add supplies to a tunnel and record contribution.")
async def addsupplies(interaction: discord.Interaction, name: str, amount: int):
    await interaction.response.defer(ephemeral=True)
    
    guild_id = str(interaction.guild_id)
    channel_id = interaction.channel.id

    facility_from_channel = get_facility_for_channel(guild_id, channel_id)
    facility_name = facility_from_channel
    tdata = None

    if facility_from_channel:
        fac_rec = get_facility_record(facility_from_channel)
        tdata = fac_rec["tunnels"].get(name)
        if not tdata:
            # Does this tunnel exist in another facility?
            other_fac, _ = find_tunnel(name)
            if other_fac:
                await interaction.followup.send(
                    f"❌ Tunnel **{name}** belongs to facility **{other_fac}**. "
                    f"Please use that facility's MSUPP dashboard thread.",
                    ephemeral=True
                )
                return
    else:
        facility_name, tdata = find_tunnel(name)

    if not tdata:
        await interaction.followup.send(f"❌ Tunnel **{name}** not found.", ephemeral=True)
        return

    tdata["total_supplies"] = tdata.get("total_supplies", 0) + amount
    uid = str(interaction.user.id)
    users[uid] = users.get(uid, 0) + amount

    save_data(DATA_FILE, tunnels)
    save_data(USER_FILE, users)
    await refresh_dashboard(interaction.guild, facility_name)

    log_contribution(interaction.user.id, "add supplies", amount, name)
    await log_action(
        interaction.guild,
        interaction.user,
        "added supplies",
        target_name=f"[{facility_name}] {name}" if facility_name else name,
        amount=amount
    )

    await interaction.followup.send(f"🪣 Added {amount:,} supplies to **{name}**.", ephemeral=True)

@addsupplies.autocomplete("name")
async def addsupplies_name_autocomplete(
    interaction: discord.Interaction,
    current: str
):
    return await tunnel_name_autocomplete_impl(interaction, current)

@bot.tree.command(name="updatetunnel", description="Update tunnel values without affecting leaderboard.")
async def updatetunnel(
    interaction: discord.Interaction,
    name: str,
    supplies: int = None,
    usage_rate: int = None,
    location: str = None
):
    await interaction.response.defer(ephemeral=True)

    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
        return
        
    guild_id = str(interaction.guild_id)
    channel_id = interaction.channel.id

    facility_from_channel = get_facility_for_channel(guild_id, channel_id)
    facility_name = facility_from_channel
    tdata = None

    if facility_from_channel:
        fac_rec = get_facility_record(facility_from_channel)
        tdata = fac_rec["tunnels"].get(name)
        if not tdata:
            other_fac, _ = find_tunnel(name)
            if other_fac:
                await interaction.followup.send(
                    f"❌ Tunnel **{name}** belongs to facility **{other_fac}**. "
                    f"Please use that facility's MSUPP dashboard thread.",
                    ephemeral=True
                )
                return
    else:
        facility_name, tdata = find_tunnel(name)

    if not tdata:
        await interaction.followup.send(f"❌ Tunnel **{name}** not found.", ephemeral=True)
        return
    
    # Update only provided fields
    if supplies is not None:
        tdata["total_supplies"] = supplies
    if usage_rate is not None:
        tdata["usage_rate"] = usage_rate
    if location is not None:
        tdata["location"] = location

    total_supplies = tdata.get("total_supplies", 0)
    current_rate = tdata.get("usage_rate", 0)

    save_data(DATA_FILE, tunnels)
    await refresh_dashboard(interaction.guild, facility_name)

    await log_action(
        interaction.guild,
        interaction.user,
        "updated tunnel",
        target_name=f"[{facility_name}] {name}" if facility_name else name,
        amount=total_supplies,
        details=f"Rate: {current_rate}/hr"
    )

    await interaction.followup.send(f"✅ Tunnel **{name}** updated successfully.", ephemeral=True)
@updatetunnel.autocomplete("name")
async def updatetunnel_name_autocomplete(
    interaction: discord.Interaction,
    current: str
):
    return await tunnel_name_autocomplete_impl(interaction, current)

@bot.tree.command(name="msupp_dashboard", description="Create or refresh an MSUPP dashboard for this facility/thread.")
async def msupp_dashboard(interaction: discord.Interaction):
    """
    - If this channel/thread already has a facility dashboard: refresh it.
    - Otherwise: open a modal, suggesting the thread name as the facility name.
    """
    channel = interaction.channel
    guild = interaction.guild
    guild_id = str(guild.id)
    channel_id = channel.id

    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
        return

    # If this channel is already bound to a facility, refresh only that one
    existing_facility_name = get_facility_for_channel(guild_id, channel_id)
    if existing_facility_name:
        await refresh_msupp_dashboard(guild, existing_facility_name)
        await interaction.response.send_message(
            f"🔁 Refreshed MSUPP dashboard for **{existing_facility_name}**.",
            ephemeral=True
        )
        return

    # No facility bound yet → open modal with thread/channel name as the suggestion
    suggested = channel.name or "New MSUPP Facility"
    modal = MsuppDashboardModal(suggested_name=suggested, channel_id=channel_id, guild_id=guild.id)
    await interaction.response.send_modal(modal)

@bot.tree.command(name="order_dashboard", description="Show or bind the order management dashboard.")
async def order_dashboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    embed = build_order_dashboard()

    if guild_id in dashboard_info and "orders_message" in dashboard_info[guild_id]:
        await refresh_order_dashboard(interaction.guild)
        await interaction.followup.send("🔁 Order dashboard refreshed.", ephemeral=True)
        return

    msg = await interaction.followup.send(embed=embed)
    if guild_id not in dashboard_info:
        dashboard_info[guild_id] = {}
    dashboard_info[guild_id]["orders_channel"] = msg.channel.id
    dashboard_info[guild_id]["orders_message"] = msg.id
    save_data(DASH_FILE, dashboard_info)

    await interaction.followup.send("✅ Order dashboard created and bound to this channel.", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show current contributors.")
async def leaderboard(interaction: discord.Interaction):
    try:
        await interaction.response.defer(thinking=True)

        if not users:
            await interaction.followup.send("No contributions yet!", ephemeral=True)
            return

        top = sorted(users.items(), key=lambda x: x[1], reverse=True)[:10]

        # Medal emojis for top 3 positions
        medals = ["🥇", "🥈", "🥉"]

        desc_lines = []
        for i, (uid, amt) in enumerate(top):
            user = await bot.fetch_user(int(uid))
            medal = medals[i] if i < 3 else f"**{i+1}.**"
            desc_lines.append(f"{medal} {user.display_name} — **{amt:,}**")

        desc = "\n".join(desc_lines) or "No contributions recorded."

        embed = discord.Embed(
            title="🏆 Supply Leaderboard",
            description=desc or "No data.",
            color=0xFFD700
        )
        await interaction.followup.send(embed=embed)

    except discord.errors.NotFound:
        # Fallback if the interaction expired or Discord rejected the defer
        channel = interaction.channel
        await channel.send("⚠️ Interaction expired — here's the current leaderboard:")

        sorted_users = sorted(users.items(), key=lambda x: x[1], reverse=True)
        desc = "\n".join(
            [
                f"**{i+1}.** {(await bot.fetch_user(int(uid))).display_name} — {amt:,} supplies"
                for i, (uid, amt) in enumerate(sorted_users[:10])
            ]
        )
        embed = discord.Embed(
            title="🏆 Supply Leaderboard",
            description=desc or "No data.",
            color=0xFFD700
        )
        await channel.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error showing leaderboard: {e}", ephemeral=True)

@bot.tree.command(name="stats", description="View your personal contribution stats.")
async def stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    uid = str(interaction.user.id)

    if uid not in contributions:
        await interaction.followup.send("📊 No contribution data found yet.", ephemeral=True)
        return

    data = contributions[uid]
    total = data.get("total_supplies", 0)
    actions = data.get("actions", [])
    last_action = actions[-1]["timestamp"] if actions else "N/A"

    embed = discord.Embed(
        title=f"📈 Contribution Stats — {interaction.user.display_name}",
        color=discord.Color.teal()
    )
    embed.add_field(name="Total Supplies Added", value=f"**{total:,}**", inline=False)
    embed.add_field(name="Total Actions", value=f"**{len(actions)}**", inline=False)
    embed.add_field(name="Last Action", value=f"`{last_action}`", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="deletetunnel", description="Officer-only: Delete a tunnel from the dashboard.")
async def deletetunnel(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)

    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    channel_id = interaction.channel.id

    facility_from_channel = get_facility_for_channel(guild_id, channel_id)
    facility_name = facility_from_channel
    tdata = None

    if facility_from_channel:
        facility_record = get_facility_record(facility_from_channel)
        tdata = facility_record["tunnels"].get(name)
        if not tdata:
            other_fac, _ = find_tunnel(name)
            if other_fac:
                await interaction.followup.send(
                    f"❌ Tunnel **{name}** belongs to facility **{other_fac}**. "
                    f"Please use that facility's MSUPP dashboard thread.",
                    ephemeral=True
                )
                return
    else:
        facility_name, tdata = find_tunnel(name)
        if facility_name:
            facility_record = get_facility_record(facility_name)

    if not tdata or not facility_name:
        await interaction.followup.send(f"❌ Tunnel **{name}** not found.", ephemeral=True)
        return
        
    # Remove from its facility
    facility_record["tunnels"].pop(name, None)
    save_data(DATA_FILE, tunnels)
    await refresh_dashboard(interaction.guild, facility_name)

    await log_action(
        interaction.guild,
        interaction.user,
        "deleted tunnel",
        target_name=f"[{facility_name}] {name}" if facility_name else name
    )

    await interaction.followup.send(
        f"🗑️ Tunnel **{name}** deleted successfully and dashboard updated.",
        ephemeral=True
    )

@deletetunnel.autocomplete("name")
async def deletetunnel_name_autocomplete(
    interaction: discord.Interaction,
    current: str
):
    return await tunnel_name_autocomplete_impl(interaction, current)

@bot.tree.command(name="endwar", description="Officer-only: End the war, close all MSUPP facilities, and reset systems.")
async def endwar(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 You do not have permission.", ephemeral=True)
        return

    await interaction.followup.send("DEBUG: reached endwar body", ephemeral=True)

# ============================================================
# 1️⃣ ARCHIVE SNAPSHOT BEFORE RESET
# ============================================================

    war_end_time = datetime.now(timezone.utc)
    timestamp_str = war_end_time.strftime("%Y-%m-%d_%H-%M-%S_UTC")
    archive_folder = create_war_archive_folder(timestamp_str)

    # Save current state
    export_json(archive_folder / "tunnels.json", tunnels)
    export_json(archive_folder / "dashboard.json", dashboard_info)
    export_json(archive_folder / "orders.json", load_data(ORDERS_FILE, {}))
    export_json(archive_folder / "users.json", users)
    export_json(archive_folder / "contributions.json", contributions)

    # Build summary before reset
    total_supplies = sum(users.values())
    sorted_contribs = sorted(users.items(), key=lambda x: x[1], reverse=True)
    facility_count = len(tunnels)
    tunnel_count = sum(len(f.get("tunnels", {})) for f in tunnels.values())

    export_json(archive_folder / "war_summary.json", {
        "facility_count": facility_count,
        "tunnel_count": tunnel_count,
        "total_supplies": total_supplies,
        "leaderboard": sorted_contribs,
        "timestamp": war_end_time.isoformat()
    })

    # Write MD report
    generate_markdown_report(
        archive_folder / "war_report.md",
        interaction.guild.name,
        war_end_time.strftime("%Y-%m-%d %H:%M UTC"),
        facility_count,
        tunnel_count,
        total_supplies,
        sorted_contribs,
        interaction.guild
    )   

    guild = interaction.guild
    guild_id = str(guild.id)

    # ============================================================
    # 1️⃣ GATHER WAR SUMMARY BEFORE RESETTING ANY DATA
    # ============================================================

    # Total contributions (per-user)
    total_contribs = {uid: amt for uid, amt in users.items() if amt > 0}

    # Total supplies delivered overall
    total_supplies = sum(total_contribs.values())

    # Sort users for leaderboard
    sorted_contribs = sorted(
        total_contribs.items(), key=lambda x: x[1], reverse=True
    )

    # Facility/tunnel counts
    facility_count = len(tunnels)
    tunnel_count = sum(len(f["tunnels"]) for f in tunnels.values())

    # Build summary lines
    leaderboard_lines = []
    rank = 1
    for uid, amount in sorted_contribs[:10]:
        member = guild.get_member(int(uid))
        name = member.display_name if member else f"User {uid}"
        leaderboard_lines.append(f"**{rank}. {name}** — {amount:,}")
        rank += 1

    leaderboard_text = "\n".join(leaderboard_lines) if leaderboard_lines else "_No contributors this war._"

    # ============================================================
    # 2️⃣ CLOSE ALL FACILITY DASHBOARDS
    # ============================================================

    info = dashboard_info.get(guild_id, {})
    facilities = info.get("facilities", {})

    for fac_name, fac_cfg in facilities.items():
        chan_id = fac_cfg.get("tunnel_channel")
        msg_id = fac_cfg.get("tunnel_message")

        if not chan_id or not msg_id:
            continue

        channel = guild.get_channel(chan_id)
        if not channel:
            continue

        try:
            msg = await channel.fetch_message(msg_id)
            closed_embed = discord.Embed(
                title="🛑 Facility Closed — End of War",
                description=(
                    f"**{fac_name}** has been closed.\n"
                    "All tunnels and supply data reset for the new war."
                ),
                color=discord.Color.red()
            )
            await msg.edit(embed=closed_embed, view=None)
        except:
            pass

    # ============================================================
    # 3️⃣ WIPE FACILITIES + TUNNEL DATA
    # ============================================================

    tunnels.clear()
    info["facilities"] = {}
    dashboard_info[guild_id] = info

    save_data(DATA_FILE, tunnels)
    save_data(DASH_FILE, dashboard_info)

    # ============================================================
    # 4️⃣ RESET CONTRIBUTIONS (BUT KEEP USERS)
    # ============================================================

    for uid in users:
        users[uid] = 0  # reset only contribution amount

    contributions.clear()

    save_data(USER_FILE, users)
    save_data(CONTRIB_FILE, contributions)

    # ============================================================
    # 5️⃣ WIPE ACTIVE ORDERS — PRESERVE ORDER DASHBOARD
    # ============================================================

    # Reset all active orders but keep the dashboard location
    global orders_data
    orders_data = {"next_id": 1, "orders": {}}
    save_orders()

    if "orders_channel" in info and "orders_message" in info:
        chan = guild.get_channel(info["orders_channel"])
        if chan:
            try:
                msg = await chan.fetch_message(info["orders_message"])
                view = OrderDashboardView()
                embed = build_clickable_order_dashboard()
                await msg.edit(embed=embed, view=view)
            except Exception:
                pass

    # ============================================================
    # 6️⃣ AUDIT LOG ENTRY (A2 FORMAT)
    # ============================================================

    await log_action(
        guild,
        interaction.user,
        action="ended the war",
        target_name=f"{facility_count} facilities, {tunnel_count} tunnels",
        details=f"Total supplies delivered: {total_supplies:,}"
    )

    # ============================================================
    # 7️⃣ PUBLIC END-OF-WAR SUMMARY POST
    # ============================================================

    summary_embed = discord.Embed(
        title="🏁 End of War — Final MSUPP Summary",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc)
    )

    summary_embed.add_field(name="🏭 Facilities Operated", value=str(facility_count), inline=True)
    summary_embed.add_field(name="🔧 Tunnels Managed", value=str(tunnel_count), inline=True)
    summary_embed.add_field(name="📦 Total Supplies Delivered", value=f"{total_supplies:,}", inline=False)
    summary_embed.add_field(name="🥇 Top Contributors", value=leaderboard_text, inline=False)

    # Post summary to leaderboard channel if configured
    lb_channel_id = info.get("leaderboard_channel")
    if lb_channel_id:
        ch = guild.get_channel(lb_channel_id)
        if ch:
            try:
                await ch.send(embed=summary_embed)
            except:
                pass

    # Ephemeral acknowledgement to officer
    await interaction.followup.send("🏁 End of war completed. All systems reset.", ephemeral=True)

@bot.tree.command(name="orders", description="Show the interactive order management dashboard.")
async def orders(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
        return

    view = OrderDashboardView()
    embed = build_clickable_order_dashboard()
    msg = await interaction.followup.send(embed=embed, view=view)

    guild_id = str(interaction.guild_id)
    if guild_id not in dashboard_info:
        dashboard_info[guild_id] = {}

    dashboard_info[guild_id]["orders_channel"] = msg.channel.id
    dashboard_info[guild_id]["orders_message"] = msg.id
    save_data(DASH_FILE, dashboard_info)

@bot.tree.command(name="setleaderboardchannel", description="Set the channel where weekly leaderboards will be posted.")
async def setleaderboardchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    # Defer early to prevent "Unknown interaction" timeout
    await interaction.response.defer(ephemeral=True)

    # Officer-only restriction
    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
        return

    gid = str(interaction.guild_id)

    if gid not in dashboard_info:
        dashboard_info[gid] = {}

    dashboard_info[gid]["leaderboard_channel"] = channel.id
    save_data(DASH_FILE, dashboard_info)

    await interaction.followup.send(
        f"✅ Weekly leaderboard channel set to {channel.mention}.",
        ephemeral=True
    )
    
@bot.tree.command(name="setlogchannel", description="Officer-only: Set the channel where FAC logs will be posted.")
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)

    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    if guild_id not in dashboard_info:
        dashboard_info[guild_id] = {}

    dashboard_info[guild_id]["log_channel"] = channel.id
    save_data(DASH_FILE, dashboard_info)

    await interaction.followup.send(f"✅ FAC logs will now post to {channel.mention}.", ephemeral=True)

@bot.tree.command(name="help", description="Show all available Foxhole FAC commands.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛠️ Foxhole FAC Bot Commands",
        description="A complete list of available commands and their purposes.",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="📦 Tunnel Management",
        value=(
            "**/addtunnel** — Add a new tunnel to the current facility.\n"
            "**/updatetunnel** — Update tunnel supplies, usage rate, or location.\n"
            "**/addsupplies** — Add supplies to a tunnel.\n"
            "**/deletetunnel** *(Officer only)* — Remove a tunnel.\n"
        ),
        inline=False
    )

    embed.add_field(
        name="🏭 MSUPP Facilities",
        value=(
            "**/msupp_dashboard** — Create or refresh a facility dashboard.\n"
            "**(auto facility binding based on thread)**\n"
        ),
        inline=False
    )

    embed.add_field(
        name="📊 Orders Dashboard",
        value=(
            "**/orders** — Show the interactive orders dashboard.\n"
            "**/order_dashboard** — Create/refresh the persistent order dashboard.\n"
        ),
        inline=False
    )

    embed.add_field(
        name="📦 Orders System",
        value=(
            "**/order_create** — Create a new production order.\n"
            "**/order_delete** *(Officer only)* — Delete an order.\n"
        ),
        inline=False
    )

    embed.add_field(
        name="📈 Contributions",
        value=(
            "**/leaderboard** — Show weekly top contributors.\n"
            "**/stats** — View your personal supply stats.\n"
        ),
        inline=False
    )

    embed.add_field(
        name="⚔️ War Management",
        value=(
            "**/endwar** *(Officer only)* — Post summary & reset all MSUPP data.\n"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ Configuration (Officer Only)",
        value=(
            "**/setleaderboardchannel** — Set weekly leaderboard channel.\n"
            "**/setlogchannel** — Set FAC audit log channel.\n"
        ),
        inline=False
    )

    embed.add_field(
        name="🧪 Utility",
        value=(
            "**/checkpermissions** — Check bot permissions.\n"
        ),
        inline=False
    )

    embed.set_footer(text="Use /help anytime for a clean list of available commands.")
    await interaction.response.send_message(embed=embed, ephemeral=True)    
    
@bot.tree.command(name="checkpermissions", description="Check the bot's permissions in this channel.")
async def checkpermissions(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
        return
        
    perms = interaction.channel.permissions_for(interaction.guild.me)
    results = [
        f"👁️ View Channel: {'✅' if perms.view_channel else '❌'}",
        f"💬 Send Messages: {'✅' if perms.send_messages else '❌'}",
        f"🔗 Embed Links: {'✅' if perms.embed_links else '❌'}",
        f"📜 Read History: {'✅' if perms.read_message_history else '❌'}",
        f"⚙️ Slash Commands: {'✅' if perms.use_application_commands else '❌'}",
    ]
    await interaction.response.send_message("\n".join(results), ephemeral=True)

@bot.tree.command(
    name="adjust_contribution",
    description="Officer-only: Correct a user's contribution stats."
)
@app_commands.describe(
    member="User whose contribution needs correction",
    amount="Positive or negative supply amount (e.g. -1500 or 500)",
    reason="Reason for the correction"
)
async def adjust_contribution(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: int,
    reason: str
):
    await interaction.response.defer(ephemeral=True)

    # Officer check (explicit, consistent with other admin commands)
    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send(
            "🚫 Only Officers can adjust contribution data.",
            ephemeral=True
        )
        return

    user_id = str(member.id)

    # Ensure user exists in records
    if user_id not in users:
        users[user_id] = 0

    if user_id not in contributions:
        contributions[user_id] = {
            "total_supplies": 0,
            "actions": []
        }

    # Apply correction
    before = users[user_id]
    users[user_id] = max(0, users[user_id] + amount)
    contributions[user_id]["total_supplies"] = users[user_id]

    # Log correction action
    now = datetime.now(timezone.utc).isoformat()
    contributions[user_id]["actions"].append({
        "timestamp": now,
        "action": "correction",
        "amount": amount,
        "reason": reason,
        "corrected_by": str(interaction.user.id)
    })

    # Persist changes
    save_data(USER_FILE, users)
    save_data(CONTRIB_FILE, contributions)

    # FAC audit log
    await log_action(
        interaction.guild,
        interaction.user,
        action="corrected contribution",
        target_name=member.display_name,
        details=f"{before:,} → {users[user_id]:,} ({amount:+,}) | {reason}"
    )

    # Confirmation
    await interaction.followup.send(
        (
            f"✅ Contribution corrected for **{member.display_name}**\n\n"
            f"**Change:** {amount:+,} supplies\n"
            f"**New Total:** {users[user_id]:,}\n"
            f"**Reason:** {reason}"
        ),
        ephemeral=True
    )


# ============================================================
# ORDERS SYSTEM
# ============================================================

def save_orders():
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders_data, f, indent=4)

orders_data = load_orders()

# ------------------------------------------------------------
# Create Order
# ------------------------------------------------------------
@bot.tree.command(name="order_create", description="Create a new order request.")
async def order_create(interaction: discord.Interaction, item: str, quantity: int, priority: str = "Normal", location: str = "Unknown"):
    await interaction.response.defer(ephemeral=True)

    if not has_authorized_role(interaction.user):
        await interaction.followup.send("🚫 You do not have permission to create orders.", ephemeral=True)
        return

    order_id = str(orders_data["next_id"])
    orders_data["next_id"] += 1

    orders_data["orders"][order_id] = {
        "item": item,
        "quantity": quantity,
        "priority": priority.capitalize(),
        "status": "Order Placed",
        "requested_by": str(interaction.user.id),
        "claimed_by": None,
        "location": location,
        "timestamps": {"created": datetime.now(timezone.utc).isoformat()},
    }

    save_orders()

    await log_action(
        interaction.guild,
        interaction.user,
        "placed new order",
        target_name=f"#{order_id}",
        amount=quantity,
        details=f"{item} ({priority})"
    )

    await interaction.followup.send(f"🧾 Order **#{order_id}** for **{item} x{quantity}** created successfully at **{location}**.", ephemeral=True)
    await refresh_order_dashboard(interaction.guild)

# ------------------------------------------------------------
# Delete Order
# ------------------------------------------------------------
@bot.tree.command(name="order_delete", description="Officer-only: Delete an order.")
async def order_delete(interaction: discord.Interaction, order_id: int):
    await interaction.response.defer(ephemeral=True)

    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 Only Officers can delete orders.", ephemeral=True)
        return

    order_id = str(order_id)
    if order_id not in orders_data["orders"]:
        await interaction.followup.send(f"❌ Order **#{order_id}** not found.", ephemeral=True)
        return

    deleted = orders_data["orders"].pop(order_id)
    save_orders()

    await log_action(
        interaction.guild,
        interaction.user,
        "deleted order",
        target_name=f"#{order_id}",
        details=f"{deleted['item']} x{deleted['quantity']}"
    )
    
    await interaction.followup.send(f"🗑️ Order **#{order_id}** deleted successfully.", ephemeral=True)

# ============================================================
# TASKS
# ============================================================

@tasks.loop(minutes=2)
async def refresh_dashboard_loop():
# apply usage decay first (per facility, tolerant of malformed data)
    for facility_name, facility_data in tunnels.items():
        tun_dict = facility_data.get("tunnels", {})
        if not isinstance(tun_dict, dict):
            continue

        for tdata in tun_dict.values():
            rate = tdata.get("usage_rate", 0)
            if rate > 0:
                # 2 minutes is 1/30th of an hour → rate/30
                tdata["total_supplies"] = max(
                    0,
                    tdata.get("total_supplies", 0) - (rate / 30)
                )

    save_data(DATA_FILE, tunnels)

    # update dashboards per facility
    for guild in bot.guilds:
        gid = str(guild.id)
        info = dashboard_info.get(gid, {})
        facilities = info.get("facilities", {})
        for facility_name in facilities.keys():
            await refresh_msupp_dashboard(guild, facility_name)

@tasks.loop(time=time(hour=12, tzinfo=timezone.utc))
async def weekly_leaderboard():
    now = datetime.now(timezone.utc)
    if now.weekday() != 6:
        return
    for guild in bot.guilds:
        info = dashboard_info.get(str(guild.id), {})
        channel = None

        # Check if custom channel is set
        if "leaderboard_channel" in info:
             channel = guild.get_channel(info["leaderboard_channel"])

        # Fallback if not set or missing
        if not channel:
             channel = discord.utils.get(guild.text_channels, name="logistics") or \
                   discord.utils.get(guild.text_channels, name="general")

        if not channel:
            continue
        if not users:
            await channel.send("📊 No contributions to report this week!")
            continue
        top = sorted(users.items(), key=lambda x: x[1], reverse=True)[:10]

        # Medal emojis for top 3 positions
        medals = ["🥇", "🥈", "🥉"]

        desc_lines = []
        for i, (uid, amt) in enumerate(top):
            user = await bot.fetch_user(int(uid))
            medal = medals[i] if i < 3 else f"**{i+1}.**"
            desc_lines.append(f"{medal} {user.display_name} — **{amt:,}**")

        desc = "\n".join(desc_lines) or "No contributions recorded."

        embed = discord.Embed(
            title="🏆 Weekly Contribution Leaderboard",
            description=desc,
            color=0xFFD700,
            timestamp=now,
        )
        embed.set_footer(text=f"Updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        await channel.send(embed=embed)
    # Reset weekly totals but keep user entries for war/lifetime stats
    for uid in list(users.keys()):
        users[uid] = 0
    save_data(USER_FILE, users)

@tasks.loop(minutes=5)
async def flush_log_buffer():
    await flush_supply_logs()

@flush_log_buffer.before_loop
async def before_flush_supply():
    await bot.wait_until_ready()

# ============================================================
# START
# ============================================================

bot.run(TOKEN)
