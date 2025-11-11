import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
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
SUPPLY_INCREMENT = 1500

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("❌ No DISCORD_TOKEN found in environment variables.")

for file, default in [
    (DATA_FILE, {}),
    (USER_FILE, {}),
    (DASH_FILE, {}),
    (ORDERS_FILE, {"next_id": 1, "orders": {}})
]:
    if not os.path.exists(file):
        save_data(file, default)

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

tunnels = load_data(DATA_FILE, {})
users = load_data(USER_FILE, {})
dashboard_info = load_data(DASH_FILE, {})  # {guild_id: {"channel": id, "message": id}}

def catch_up_tunnels():
    now = datetime.now(timezone.utc)
    updated = False

    for name, data in tunnels.items():
        usage = data.get("usage_rate", 0)
        last_str = data.get("last_updated")
        if not last_str:
            data["last_updated"] = now.isoformat()
            continue

        last = datetime.fromisoformat(last_str)
        hours_passed = (now - last).total_seconds() / 3600
        if hours_passed > 0 and usage > 0:
            data["total_supplies"] = max(0, data["total_supplies"] - (usage * hours_passed))
            data["last_updated"] = now.isoformat()
            updated = True

    if updated:
        save_data(DATA_FILE, tunnels)

# ============================================================
# DATA LOGGING
# ============================================================

async def log_action(
    guild: discord.Guild,
    actor: discord.User | discord.Member,
    action_type: str,
    target_name: str = None,
    amount: int | float | None = None,
    location: str | None = None,
    extra: str = None
):
    """Post structured audit log entries to the FAC log thread."""
    try:
        guild_id = str(guild.id)
        log_channel_id = dashboard_info.get(guild_id, {}).get("log_channel")
        if not log_channel_id:
            return

        log_channel = guild.get_channel(log_channel_id)
        if not log_channel:
            return

        # Look for or create the FAC Logs thread
        thread = discord.utils.get(log_channel.threads, name="FAC Logs")
        if not thread:
            thread = await log_channel.create_thread(
                name="FAC Logs",
                type=discord.ChannelType.public_thread
            )
            await thread.send("🧾 **FAC Audit Log Thread Created** — all actions will be recorded here.")

        # Build readable structured message
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        message_parts = [
            f"🕒 `{timestamp}`",
            f"👤 **{actor.display_name}**",
            f"🧭 *{action_type}*"
        ]
        if target_name:
            message_parts.append(f"→ **{target_name}**")
        if amount is not None:
            message_parts.append(f"📦 `{amount:,}` units")
        if location:
            message_parts.append(f"📍 `{location}`")
        if extra:
            message_parts.append(f"📝 {extra}")

        log_message = " | ".join(message_parts)
        await thread.send(log_message)

    except Exception as e:
        print(f"[LOGGING ERROR] {e}")

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
        tunnels[self.tunnel_name]["total_supplies"] += amount

        user_id = str(interaction.user.id)
        users[user_id] = users.get(user_id, 0) + amount
        save_data(DATA_FILE, tunnels)
        save_data(USER_FILE, users)

        await refresh_(interaction.guild)
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
            label=f"{tunnel} +{SUPPLY_INCREMENT}",
            style=discord.ButtonStyle.green,
            custom_id=f"tunnel_{tunnel.lower().replace(' ', '_')}"
        )
        self.tunnel = tunnel

    async def callback(self, interaction: discord.Interaction):
        view = discord.ui.View(timeout=30)

        async def done_callback(interaction: discord.Interaction):
            tunnels[self.tunnel]["total_supplies"] += SUPPLY_INCREMENT
            user_id = str(interaction.user.id)
            users[user_id] = users.get(user_id, 0) + SUPPLY_INCREMENT
            save_data(DATA_FILE, tunnels)
            save_data(USER_FILE, users)
            await refresh_dashboard(interaction.guild)
            await interaction.response.edit_message(
                content=f"🪣 Added {SUPPLY_INCREMENT} supplies to **{self.tunnel}**!",
                view=None
            )

        async def stack_callback(interaction: discord.Interaction):
            modal = StackSubmitModal(self.tunnel)
            await interaction.response.send_modal(modal)

        view.add_item(discord.ui.Button(label="1500 (Done)", style=discord.ButtonStyle.green))
        view.children[0].callback = done_callback
        view.add_item(discord.ui.Button(label="Submit Stacks (x100)", style=discord.ButtonStyle.blurple))
        view.children[1].callback = stack_callback

        await interaction.response.send_message(
            f"How much would you like to submit to **{self.tunnel}**?",
            ephemeral=True,
            view=view
        )

class DashboardView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        for name in tunnels.keys():
            self.add_item(TunnelButton(name))

# ============================================================
# DASHBOARD PAGINATION SYSTEM
# ============================================================

class DashboardPaginator(discord.ui.View):
    """Combined paginated + interactive dashboard for tunnels."""
    def __init__(self, tunnels, per_page=8):
        super().__init__(timeout=None)
        self.tunnels = list(tunnels.items())
        self.per_page = per_page
        self.page = 0
        self.total_pages = max(1, -(-len(self.tunnels) // self.per_page))
        self.build_page_buttons()

    # -----------------------------------------
    # Build the embed for the current page
    # -----------------------------------------
    def build_page_embed(self):
        embed = discord.Embed(
            title=f"🛠 Foxhole FAC Dashboard — Page {self.page + 1}/{self.total_pages}",
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
            status = "🟢" if hours >= 24 else "🟡" if hours >= 4 else "🔴"
            embed.add_field(
                name=f"{name}",
                value=f"**Supplies:** {supplies:,} | **Usage:** {usage}/hr | {status} **{hours}h**",
                inline=False
            )

        embed.set_footer(text="Updated every 2 minutes. Use the buttons below to add supplies or navigate pages.")
        return embed

    # -----------------------------------------
    # Navigation Buttons
    # -----------------------------------------
    @discord.ui.button(label="⏮️", style=discord.ButtonStyle.gray)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 0
        self.build_page_buttons()
        await interaction.response.edit_message(embed=self.build_page_embed(), view=self)

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.gray)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        self.build_page_buttons()
        await interaction.response.edit_message(embed=self.build_page_embed(), view=self)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.gray)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages - 1:
            self.page += 1
        self.build_page_buttons()
        await interaction.response.edit_message(embed=self.build_page_embed(), view=self)

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.gray)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = self.total_pages - 1
        self.build_page_buttons()
        await interaction.response.edit_message(embed=self.build_page_embed(), view=self)

    # -----------------------------------------
    # Rebuild tunnel buttons dynamically
    # -----------------------------------------
    def build_page_buttons(self):
        """Clear and rebuild tunnel buttons for current page."""
        # remove old items except nav buttons (we rebuild all)
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
            # use your existing TunnelButton class
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

async def refresh_dashboard(guild: discord.Guild):
    """Edit the persistent dashboard message if it exists."""
    if str(guild.id) not in dashboard_info:
        return

    info = dashboard_info[str(guild.id)]
    channel = guild.get_channel(info["channel"])
    if not channel:
        return

    try:
        msg = await channel.fetch_message(info["message"])
        paginator = DashboardPaginator(tunnels)
        await msg.edit(embed=paginator.build_page_embed(), view=paginator)
    except Exception as e:
        print(f"[ERROR] Failed to refresh dashboard in {guild.name}: {e}")

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

    channel = guild.get_channel(channel_id)
    if not channel:
        print(f"[WARN] Orders channel missing for guild {guild.name}.")
        return

    try:
        msg = await channel.fetch_message(message_id)
        await msg.edit(embed=build_clickable_order_dashboard(), view=OrderDashboardView())
        print(f"[OK] Refreshed order dashboard for {guild.name}.")
    except discord.NotFound:
        # The message was deleted — recreate it
        new_msg = await channel.send(embed=build_clickable_order_dashboard(), view=OrderDashboardView())
        dashboard_info[gid]["orders_channel"] = channel.id
        dashboard_info[gid]["orders_message"] = new_msg.id
        save_data(DASH_FILE, dashboard_info)
        print(f"[INFO] Recreated order dashboard in {channel.name}.")
    except Exception as e:
        print(f"[ERROR] Failed to refresh order dashboard in {guild.name}: {e}")

# ------------------------------------------------------------
# Command to Create or Refresh Dashboard
# ------------------------------------------------------------
@bot.tree.command(name="order_dashboard", description="Show or bind the order management dashboard.")
async def order_dashboard(interaction: discord.Interaction):
    await interaction.response.defer()

    guild_id = str(interaction.guild_id)
    embed = build_order_dashboard()

    if guild_id in dashboard_info and "order_message" in dashboard_info[guild_id]:
        await refresh_order_dashboard(interaction.guild)
        await interaction.followup.send("🔁 Order dashboard refreshed.", ephemeral=True)
        return

    msg = await interaction.followup.send(embed=embed)
    dashboard_info[guild_id]["order_channel"] = msg.channel.id
    dashboard_info[guild_id]["order_message"] = msg.id
    save_data(DASH_FILE, dashboard_info)

    await interaction.followup.send("✅ Order dashboard created and bound to this channel.", ephemeral=True)

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

        channel = guild.get_channel(channel_id)
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
# INTERACTIVE ORDER DASHBOARD (CLICKABLE)
# ============================================================

class OrderStatusSelect(discord.ui.Select):
    def __init__(self, order_id: str):
        self.order_id = order_id

        options = [
            discord.SelectOption(label="Order Placed", emoji="📦"),
            discord.SelectOption(label="Order Claimed", emoji="🧰"),
            discord.SelectOption(label="Order Started", emoji="⚙️"),
            discord.SelectOption(label="In Progress", emoji="🚧"),
            discord.SelectOption(label="Ready for Collection", emoji="📦"),
            discord.SelectOption(label="Complete", emoji="✅")
        ]

        super().__init__(
            placeholder="Select new order status...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        new_status = self.values[0]
        order = orders_data["orders"].get(self.order_id)

        if not order:
            await interaction.response.send_message(f"❌ Order #{self.order_id} not found.", ephemeral=True)
            return

        order["status"] = new_status
        order["timestamps"]["last_update"] = datetime.now(timezone.utc).isoformat()
        save_orders()

        await log_action(interaction.guild, f"{interaction.user.display_name} updated order **#{self.order_id}** → **{new_status}**.")
        await refresh_order_dashboard(interaction.guild)

        await interaction.response.send_message(
            f"✅ Order **#{self.order_id}** updated to **{new_status}**.",
            ephemeral=True
        )

class OrderStatusSelectView(discord.ui.View):
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

        await log_action(interaction.guild, f"{interaction.user.display_name} claimed order **#{self.order_id}** ({order['item']} x{order['quantity']}).")
        await refresh_order_dashboard(interaction.guild)
        await interaction.followup.send(f"🛠 Order **#{self.order_id}** claimed successfully.", ephemeral=True)

    @discord.ui.button(label="Update Status", style=discord.ButtonStyle.green)
    @discord.ui.button(label="Update Status", style=discord.ButtonStyle.green)
    async def update_button(self, interaction: discord.Interaction, button: discord.ui.Button):
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
            target_name=f"**#{self.order_id}**",
            extra=f"{order['item']} x{order['quantity']}"
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
            "Deleted order",
            target_name=f"**#{self.order_id}**",
            extra=f"{order['item']} x{order['quantity']}"
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
# COMMAND TO SHOW THE CLICKABLE DASHBOARD
# ============================================================
@bot.tree.command(name="orders", description="Show the interactive order management dashboard.")
async def orders(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    view = OrderDashboardView()
    embed = build_clickable_order_dashboard()
    msg = await interaction.followup.send(embed=embed, view=view)

    guild_id = str(interaction.guild_id)
    if guild_id not in dashboard_info:
        dashboard_info[guild_id] = {}

    dashboard_info[guild_id]["orders_channel"] = msg.channel.id
    dashboard_info[guild_id]["orders_message"] = msg.id
    save_data(DASH_FILE, dashboard_info)

# ------------------------------------------------------------
# Command: /order_manage — Open interactive controls for one order
# ------------------------------------------------------------
@bot.tree.command(name="order_manage", description="Open an interactive view to manage a specific order.")
async def order_manage(interaction: discord.Interaction, order_id: int):
    await interaction.response.defer(ephemeral=True)

    order_id = str(order_id)
    order = orders_data["orders"].get(order_id)
    if not order:
        await interaction.followup.send(f"❌ Order **#{order_id}** not found.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"🧾 Order #{order_id}: {order['item']} x{order['quantity']}",
        color=discord.Color.blurple(),
        description=(
            f"**Priority:** {order['priority']}\n"
            f"**Status:** {order['status']}\n"
            f"**Requested by:** <@{order['requested_by']}>\n"
            f"**Claimed by:** {('<@' + order['claimed_by'] + '>') if order['claimed_by'] else '—'}"
        ),
        timestamp=datetime.now(timezone.utc)
    )

    await interaction.followup.send(embed=embed, view=SingleOrderView(order_id), ephemeral=True)


# ============================================================
# BOT EVENTS
# ============================================================

@bot.event
async def on_ready():
    global dashboard_view
    if 'dashboard_view' not in globals() or dashboard_view is None:
        dashboard_view = DashboardView()
    catch_up_tunnels()  # ✅ simulate supply loss while offline
    bot.add_view(dashboard_view)
    bot.add_view(OrderDashboardView())
    await bot.tree.sync()
    print(f"🔁 Synced slash commands for {len(bot.tree.get_commands())} commands.")
    print(f"✅ Logged in as {bot.user}")
    weekly_leaderboard.start()
    refresh_dashboard_loop.start()
    refresh_orders_loop.start()
    bot.add_view(DashboardPaginator(tunnels))

# ============================================================
# COMMANDS
# ============================================================

@bot.tree.command(name="addtunnel", description="Add a new tunnel.")
async def addtunnel(interaction: discord.Interaction, name: str, total_supplies: int, usage_rate: int, location: str = "Unknown"):
    await interaction.response.defer(ephemeral=True)
    tunnels[name] = {
        "total_supplies": total_supplies,
        "usage_rate": usage_rate,
        "location": location,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_data(DATA_FILE, tunnels)
    dashboard_view.rebuild()

    guild_id = str(interaction.guild_id)
    if guild_id not in dashboard_info:
        # First dashboard instance: create and store it
        msg = await interaction.followup.send(embed=build_dashboard_embed(), view=dashboard_view)
        dashboard_info[guild_id] = {"channel": msg.channel.id, "message": msg.id}
        save_data(DASH_FILE, dashboard_info)
    else:
        await log_action(
            interaction.guild,
            interaction.user,
            "added new tunnel",
            target_name=name,
            amount=total_supplies,
            location=location,
            extra=f"Usage: {usage_rate}/hr"
        )

        await refresh_dashboard(interaction.guild)
        await interaction.followup.send(f"✅ Tunnel **{name}** added and dashboard updated.", ephemeral=True)

@bot.tree.command(name="addsupplies", description="Add supplies to a tunnel and record contribution.")
async def addsupplies(interaction: discord.Interaction, name: str, amount: int):
    await interaction.response.defer(ephemeral=True)

    if name not in tunnels:
        await interaction.followup.send(f"❌ Tunnel **{name}** not found.", ephemeral=True)
        return

    tunnels[name]["total_supplies"] += amount
    uid = str(interaction.user.id)
    users[uid] = users.get(uid, 0) + amount

    save_data(DATA_FILE, tunnels)
    save_data(USER_FILE, users)
    await refresh_dashboard(interaction.guild)

    # ✅ Correct logging call
    await log_action(
        interaction.guild,
        interaction.user,
        "added supplies",
        target_name=name,
        amount=amount
    )

    await interaction.followup.send(f"🪣 Added {amount:,} supplies to **{name}**.", ephemeral=True)

@bot.tree.command(name="updatetunnel", description="Update tunnel values without affecting leaderboard.")
async def updatetunnel(
    interaction: discord.Interaction,
    name: str,
    supplies: int = None,
    usage_rate: int = None,
    location: str = None
):
    await interaction.response.defer(ephemeral=True)

    if name not in tunnels:
        await interaction.followup.send(f"❌ Tunnel **{name}** not found.", ephemeral=True)
        return

    # Update only provided fields
    if supplies is not None:
        tunnels[name]["total_supplies"] = supplies
    if usage_rate is not None:
        tunnels[name]["usage_rate"] = usage_rate
    if location is not None:
        tunnels[name]["location"] = location

    # Read back safe values
    total_supplies = tunnels[name].get("total_supplies", 0)
    current_rate   = tunnels[name].get("usage_rate", 0)
    current_loc    = tunnels[name].get("location", "Unknown")

    save_data(DATA_FILE, tunnels)
    await refresh_dashboard(interaction.guild)

    await log_action(
        interaction.guild,
        interaction.user,
        "updated tunnel",
        target_name=name,
        amount=tunnels[name]["total_supplies"],
        location=tunnels[name]["location"],
        extra=f"Rate: {tunnels[name]['usage_rate']}/hr"
    )


    await interaction.followup.send(f"✅ Tunnel **{name}** updated successfully.", ephemeral=True)

@bot.tree.command(name="dashboard", description="Show or bind the persistent dashboard.")
async def dashboard(interaction: discord.Interaction):
    await interaction.response.defer()
    gid = str(interaction.guild_id)
    if gid in dashboard_info:
        await refresh_dashboard(interaction.guild)
        await interaction.followup.send("🔁 Dashboard refreshed.", ephemeral=True)
        return
    paginator = DashboardPaginator(tunnels)
    msg = await interaction.followup.send(embed=paginator.build_page_embed(), view=paginator)
    dashboard_info[gid] = {"channel": msg.channel.id, "message": msg.id}
    save_data(DASH_FILE, dashboard_info)
    await interaction.followup.send("✅ Dashboard created and bound to this channel.", ephemeral=True)

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

@bot.tree.command(name="deletetunnel", description="Officer-only: Delete a tunnel from the dashboard.")
async def deletetunnel(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)

    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
        return

    if name not in tunnels:
        await interaction.followup.send(f"❌ Tunnel **{name}** not found.", ephemeral=True)
        return

    del tunnels[name]
    save_data(DATA_FILE, tunnels)
    await refresh_dashboard(interaction.guild)

    await log_action(
        interaction.guild,
        interaction.user,
        "deleted tunnel",
        target_name=name
    )


    await interaction.followup.send(f"🗑️ Tunnel **{name}** deleted successfully and dashboard updated.", ephemeral=True)


@bot.tree.command(name="endwar", description="Officer-only: show totals and reset all tunnel and supply data.")
async def endwar(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # Officer-only restriction
    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if not officer_role or officer_role not in interaction.user.roles:
        await interaction.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
        return

    # Compute totals before reset
    total_supplies = sum(users.values()) if users else 0
    top_contributors = sorted(users.items(), key=lambda x: x[1], reverse=True)[:5]
    desc = "\n".join(
        [f"**{i+1}.** {(await bot.fetch_user(int(uid))).display_name} — {amt:,}" for i, (uid, amt) in enumerate(top_contributors)]
    ) or "No contributions recorded."

    embed = discord.Embed(
        title="⚔️ End of War Summary",
        description=f"📦 **Total supplies contributed:** {total_supplies:,}\n\n🏅 **Top 5 Contributors:**\n{desc}",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"Reset performed by {interaction.user.display_name}")

    # Try to post summary to the leaderboard channel
    guild_id = str(interaction.guild_id)
    info = dashboard_info.get(guild_id, {})
    leaderboard_channel = None

    if "leaderboard_channel" in info:
        leaderboard_channel = interaction.guild.get_channel(info["leaderboard_channel"])

    # Fallback if not found
    if not leaderboard_channel:
        leaderboard_channel = discord.utils.get(interaction.guild.text_channels, name="logistics") or \
                              discord.utils.get(interaction.guild.text_channels, name="general")

    if leaderboard_channel:
        await leaderboard_channel.send(embed=embed)
    else:
        await interaction.followup.send(
            "⚠️ Could not find a leaderboard or fallback channel to post the summary.",
            ephemeral=True
        )

    # ✅ Reset all tracked data
    tunnels.clear()
    users.clear()
    save_data(DATA_FILE, tunnels)
    save_data(USER_FILE, users)

    # Refresh dashboard to empty state
    await refresh_dashboard(interaction.guild)

    await log_action(interaction.guild, f"{interaction.user.display_name} executed `/endwar` — data wiped and summary posted.")

    # Private confirmation
    await interaction.followup.send("✅ End of War complete. Data has been wiped clean.", ephemeral=True)

@bot.tree.command(name="checkpermissions", description="Check the bot's permissions in this channel.")
async def checkpermissions(interaction: discord.Interaction):
    perms = interaction.channel.permissions_for(interaction.guild.me)
    results = [
        f"👁️ View Channel: {'✅' if perms.view_channel else '❌'}",
        f"💬 Send Messages: {'✅' if perms.send_messages else '❌'}",
        f"🔗 Embed Links: {'✅' if perms.embed_links else '❌'}",
        f"📜 Read History: {'✅' if perms.read_message_history else '❌'}",
        f"⚙️ Slash Commands: {'✅' if perms.use_application_commands else '❌'}",
    ]
    await interaction.response.send_message("\n".join(results), ephemeral=True)

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
        description="A full list of available commands and their uses.",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="📦 Tunnel Management",
        value=(
            "**/addtunnel** — Add a new tunnel with total supplies and usage rate.\n"
            "**/addsupplies** — Add a specific amount of supplies to a tunnel.\n"
            "**/updatetunnel** — Update tunnel values (no leaderboard impact).\n"
        ),
        inline=False
    )

    embed.add_field(
        name="📊 Dashboard & Info",
        value=(
            "**/dashboard** — Display or refresh the dashboard.\n"
            "**/checkpermissions** — Check bot permissions in this channel.\n"
        ),
        inline=False
    )

    embed.add_field(
        name="🏅 Leaderboards",
        value=(
            "**/leaderboard** — Show the current top contributors.\n"
            "📅 **Weekly Leaderboard** — Auto-posts every Sunday 12:00 UTC.\n"
            "**/setleaderboardchannel** *(Officer only)* — Choose where leaderboards and end-of-war summaries post.\n"
        ),
        inline=False
    )

    embed.add_field(
        name="⚔️ War Management",
        value=(
            "**/endwar** *(Officer only)* — Post end-of-war summary, wipe all tunnel & supply data.\n"
        ),
        inline=False
    )

    embed.add_field(
        name="🔁 Automatic Systems",
        value=(
            "• Dashboard refreshes every 2 minutes.\n"
            "• Supply depletion continues accurately after restarts.\n"
            "• Dashboard buttons: **1500 (Done)** or **Submit Stacks (x100)**.\n"
        ),
        inline=False
    )

    embed.set_footer(text="Use /help anytime for a quick reference.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================
# ORDERS SYSTEM
# ============================================================

def load_orders():
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r") as f:
            return json.load(f)
    return {"next_id": 1, "orders": {}}

def save_orders():
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders_data, f, indent=4)

orders_data = load_orders()

def has_authorized_role(member: discord.Member):
    """Check if the user has one of the authorized roles."""
    allowed_roles = ["FAC Cert", "Logi Cert", "NCO", "Officer"]
    return any(r.name in allowed_roles for r in member.roles)

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
        extra=f"{item} ({priority})"
    )

    await interaction.followup.send(f"🧾 Order **#{order_id}** for **{item} x{quantity}** created successfully at **{location}**.", ephemeral=True)
    await refresh_order_dashboard(interaction.guild)

# ------------------------------------------------------------
# Claim Order
# ------------------------------------------------------------
@bot.tree.command(name="order_claim", description="Claim an order for production.")
async def order_claim(interaction: discord.Interaction, order_id: int):
    await interaction.response.defer(ephemeral=True)

    if not has_authorized_role(interaction.user):
        await interaction.followup.send("🚫 You are not authorized to claim orders.", ephemeral=True)
        return

    order_id = str(order_id)
    order = orders_data["orders"].get(order_id)
    if not order:
        await interaction.followup.send(f"❌ Order **#{order_id}** not found.", ephemeral=True)
        return

    order["claimed_by"] = str(interaction.user.id)
    order["status"] = "Order Claimed"
    order["timestamps"]["claimed"] = datetime.now(timezone.utc).isoformat()
    save_orders()

    await log_action(
        interaction.guild,
        interaction.user,
        "claimed order",
        target_name=f"**#{order_id}**",
        extra=f"{order['item']} x{order['quantity']}"
    )
    await interaction.followup.send(f"🛠 Order **#{order_id}** claimed successfully.", ephemeral=True)

# ------------------------------------------------------------
# Update Order Status
# ------------------------------------------------------------
@bot.tree.command(name="order_update", description="Update an order’s status manually.")
async def order_update(interaction: discord.Interaction, order_id: int, status: str):
    await interaction.response.defer(ephemeral=True)

    if not has_authorized_role(interaction.user):
        await interaction.followup.send("🚫 You are not authorized to update orders.", ephemeral=True)
        return

    order_id = str(order_id)
    order = orders_data["orders"].get(order_id)
    if not order:
        await interaction.followup.send(f"❌ Order **#{order_id}** not found.", ephemeral=True)
        return

    valid_statuses = [
        "Order Placed", "Order Claimed", "Order Started",
        "In Progress", "Ready for Collection", "Complete"
    ]
    if status not in valid_statuses:
        await interaction.followup.send(f"⚠️ Invalid status. Choose one of: {', '.join(valid_statuses)}", ephemeral=True)
        return

    order["status"] = status
    order["timestamps"]["last_update"] = datetime.now(timezone.utc).isoformat()
    save_orders()

    await log_action(interaction.guild, f"{interaction.user.display_name} updated order **#{order_id}** → **{status}**.")
    await interaction.followup.send(f"✅ Order **#{order_id}** marked as **{status}**.", ephemeral=True)

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

    await log_action(interaction.guild, f"{interaction.user.display_name} deleted order **#{order_id}** ({deleted['item']} x{deleted['quantity']}).")
    await interaction.followup.send(f"🗑️ Order **#{order_id}** deleted successfully.", ephemeral=True)

# ------------------------------------------------------------
# List Orders
# ------------------------------------------------------------
@bot.tree.command(name="order_list", description="List all current orders grouped by status.")
async def order_list(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    if not orders_data["orders"]:
        await interaction.followup.send("📦 No active orders found.", ephemeral=True)
        return

    grouped = {}
    for oid, o in orders_data["orders"].items():
        grouped.setdefault(o["status"], []).append((oid, o))

    embed = discord.Embed(
        title="📋 Active FAC Orders",
        color=discord.Color.teal(),
        timestamp=datetime.now(timezone.utc)
    )

    for status, entries in grouped.items():
        lines = []
        for oid, o in entries:
            requester = await bot.fetch_user(int(o["requested_by"]))
            claimed = f" — Claimed by {(await bot.fetch_user(int(o['claimed_by']))).display_name}" if o["claimed_by"] else ""
            lines.append(f"**#{oid}** — {o['item']} x{o['quantity']} ({o['priority']}){claimed} — by {requester.display_name}")
        embed.add_field(name=f"🔹 {status} ({len(entries)})", value="\n".join(lines), inline=False)

    embed.set_footer(text="Use /order_update or /order_claim to modify orders.")
    await interaction.followup.send(embed=embed)



# ============================================================
# TASKS
# ============================================================

@tasks.loop(minutes=2)
async def refresh_dashboard_loop():
    """Refresh dashboard and apply light supply drain every 2 minutes."""
    for name, data in tunnels.items():
        rate = data.get("usage_rate", 0)
        if rate > 0:
            # Subtract usage per 2-minute tick (usage_rate per hour → divide by 30)
            data["total_supplies"] = max(0, data["total_supplies"] - rate / 30)

    save_data(DATA_FILE, tunnels)

    # Update all dashboards
    for guild in bot.guilds:
        await refresh_dashboard(guild)

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
    users.clear()
    save_data(USER_FILE, users)

# ============================================================
# START
# ============================================================

bot.run(TOKEN)
