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
SUPPLY_INCREMENT = 1500

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("❌ No DISCORD_TOKEN found in environment variables.")

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

        await refresh_dashboard(interaction.guild)
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

        async def done_callback(inter: discord.Interaction):
            tunnels[self.tunnel]["total_supplies"] += SUPPLY_INCREMENT
            user_id = str(inter.user.id)
            users[user_id] = users.get(user_id, 0) + SUPPLY_INCREMENT
            save_data(DATA_FILE, tunnels)
            save_data(USER_FILE, users)
            await refresh_dashboard(inter.guild)
            await inter.response.edit_message(
                content=f"🪣 Added {SUPPLY_INCREMENT} supplies to **{self.tunnel}**!",
                view=None
            )

        async def stack_callback(inter: discord.Interaction):
            modal = StackSubmitModal(self.tunnel)
            await inter.response.send_modal(modal)

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

    for name, data in tunnels.items():
        usage = data.get("usage_rate", 0)
        supplies = data.get("total_supplies", 0)
        hours_left = supplies / usage if usage > 0 else 0
        # Round only for display — keep decimals in saved data
        display_supplies = int(supplies)  # round down
        display_usage = int(usage)
        display_hours = int(hours_left)
        # 🟢🟡🔴 Traffic light system
        if hours_left >= 24:
           status = "🟢"
        elif hours_left >= 4:
           status = "🟡"
        else:
            status = "🔴"

        embed.add_field(
           name=f"🔧 {name}",
           value=(
              f"Supplies: **{display_supplies:,}**\n"
              f"Usage: **{display_usage:,}/hr**\n"
              f"Duration: {status} **{display_hours} hrs**"
            ),
         inline=False,
    )
    return embed

async def refresh_dashboard(guild: discord.Guild):
    """Edit the persistent dashboard message if it exists."""
    dashboard_view.rebuild()
    if str(guild.id) not in dashboard_info:
        return
    info = dashboard_info[str(guild.id)]
    channel = guild.get_channel(info["channel"])
    if not channel:
        return
    try:
        msg = await channel.fetch_message(info["message"])
        await msg.edit(embed=build_dashboard_embed(), view=dashboard_view)
    except Exception:
        pass

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
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")
    weekly_leaderboard.start()
    refresh_dashboard_loop.start()

# ============================================================
# COMMANDS
# ============================================================

@bot.tree.command(name="addtunnel", description="Add a new tunnel.")
async def addtunnel(interaction: discord.Interaction, name: str, total_supplies: int, usage_rate: int):
    await interaction.response.defer()
    tunnels[name] = {
        "total_supplies": total_supplies,
        "usage_rate": usage_rate,
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
        await refresh_dashboard(interaction.guild)
        await interaction.followup.send(f"✅ Tunnel **{name}** added and dashboard updated.", ephemeral=True)

@bot.tree.command(name="addsupplies", description="Add supplies to a tunnel and record contribution.")
async def addsupplies(interaction: discord.Interaction, name: str, amount: int):
    await interaction.response.defer()
    if name not in tunnels:
        await interaction.followup.send(f"❌ Tunnel **{name}** not found.", ephemeral=True)
        return
    tunnels[name]["total_supplies"] += amount
    uid = str(interaction.user.id)
    users[uid] = users.get(uid, 0) + amount
    save_data(DATA_FILE, tunnels)
    save_data(USER_FILE, users)
    await refresh_dashboard(interaction.guild)
    await interaction.followup.send(f"🪣 Added {amount} supplies to **{name}**.", ephemeral=True)

@bot.tree.command(name="updatetunnel", description="Update tunnel values without affecting leaderboard.")
async def updatetunnel(interaction: discord.Interaction, name: str, supplies: int = None, usage_rate: int = None):
    await interaction.response.defer()

    if name not in tunnels:
        await interaction.followup.send(f"❌ Tunnel **{name}** not found.", ephemeral=True)
        return

    if supplies is not None:
        tunnels[name]["total_supplies"] = supplies
    if usage_rate is not None:
        tunnels[name]["usage_rate"] = usage_rate
    save_data(DATA_FILE, tunnels)
    await refresh_dashboard(interaction.guild)
    await interaction.followup.send(f"✅ Tunnel **{name}** updated successfully.", ephemeral=True)

@bot.tree.command(name="dashboard", description="Show or bind the persistent dashboard.")
async def dashboard(interaction: discord.Interaction):
    await interaction.response.defer()
    gid = str(interaction.guild_id)
    if gid in dashboard_info:
        await refresh_dashboard(interaction.guild)
        await interaction.followup.send("🔁 Dashboard refreshed.", ephemeral=True)
        return
    msg = await interaction.followup.send(embed=build_dashboard_embed(), view=dashboard_view)
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

@bot.tree.command(name="endwar", description="Officer-only: show totals and reset all tunnel and supply data.")
async def endwar(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)

    # Officer-only restriction
    officer_role = discord.utils.get(inter.guild.roles, name="Officer")
    if not officer_role or officer_role not in inter.user.roles:
        await inter.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
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
    embed.set_footer(text=f"Reset performed by {inter.user.display_name}")

    # Try to post summary to the leaderboard channel
    guild_id = str(inter.guild_id)
    info = dashboard_info.get(guild_id, {})
    leaderboard_channel = None

    if "leaderboard_channel" in info:
        leaderboard_channel = inter.guild.get_channel(info["leaderboard_channel"])

    # Fallback if not found
    if not leaderboard_channel:
        leaderboard_channel = discord.utils.get(inter.guild.text_channels, name="logistics") or \
                              discord.utils.get(inter.guild.text_channels, name="general")

    if leaderboard_channel:
        await leaderboard_channel.send(embed=embed)
    else:
        await inter.followup.send(
            "⚠️ Could not find a leaderboard or fallback channel to post the summary.",
            ephemeral=True
        )

    # ✅ Reset all tracked data
    tunnels.clear()
    users.clear()
    save_data(DATA_FILE, tunnels)
    save_data(USER_FILE, users)

    # Refresh dashboard to empty state
    await refresh_dashboard(inter.guild)

    # Private confirmation
    await inter.followup.send("✅ End of War complete. Data has been wiped clean.", ephemeral=True)

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
async def setleaderboardchannel(inter: discord.Interaction, channel: discord.TextChannel):
    # Defer early to prevent "Unknown interaction" timeout
    await inter.response.defer(ephemeral=True)

    # Officer-only restriction
    officer_role = discord.utils.get(inter.guild.roles, name="Officer")
    if not officer_role or officer_role not in inter.user.roles:
        await inter.followup.send("🚫 You do not have permission to use this command.", ephemeral=True)
        return

    gid = str(inter.guild_id)

    if gid not in dashboard_info:
        dashboard_info[gid] = {}

    dashboard_info[gid]["leaderboard_channel"] = channel.id
    save_data(DASH_FILE, dashboard_info)

    await inter.followup.send(
        f"✅ Weekly leaderboard channel set to {channel.mention}.",
        ephemeral=True
    )

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
        sorted_users = sorted(users.items(), key=lambda x: x[1], reverse=True)
        desc = "\n".join(
            [f"**{i+1}.** {(await bot.fetch_user(int(uid))).display_name} — {amt}" for i, (uid, amt) in enumerate(sorted_users[:10])]
        )
        embed = discord.Embed(
            title="🏆 Weekly Contribution Leaderboard",
            description=desc,
            color=0xFFD700,
            timestamp=now,
        )
        await channel.send(embed=embed)
    users.clear()
    save_data(USER_FILE, users)

# ============================================================
# START
# ============================================================

bot.run(TOKEN)
