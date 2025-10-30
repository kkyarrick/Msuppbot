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
SUPPLY_INCREMENT = 1500

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("❌ No DISCORD_TOKEN found. Please set it in your host environment variables.")

# ============================================================
# DATA MANAGEMENT
# ============================================================

def load_data(file_path, default_data):
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            return json.load(f)
    else:
        return default_data

def save_data(file_path, data):
    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)

tunnels = load_data(DATA_FILE, {})
users = load_data(USER_FILE, {})

# ============================================================
# VIEW & BUTTONS
# ============================================================

class TunnelButton(Button):
    def __init__(self, tunnel_name):
        super().__init__(label=f"{tunnel_name} +{SUPPLY_INCREMENT}", style=discord.ButtonStyle.green)
        self.tunnel_name = tunnel_name

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)

        if self.tunnel_name not in tunnels:
            await interaction.response.send_message(f"❌ Tunnel **{self.tunnel_name}** no longer exists.", ephemeral=True)
            return

        # Update tunnel and user contribution
        tunnels[self.tunnel_name]["total_supplies"] += SUPPLY_INCREMENT
        users[user_id] = users.get(user_id, 0) + SUPPLY_INCREMENT

        save_data(DATA_FILE, tunnels)
        save_data(USER_FILE, users)

        await interaction.response.send_message(
            f"🪣 Added {SUPPLY_INCREMENT} supplies to **{self.tunnel_name}**!", ephemeral=True
        )

        # Update dashboard in same channel
        await update_dashboard(interaction.channel)

class TunnelDashboard(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.refresh_buttons()

    def refresh_buttons(self):
        self.clear_items()
        for name in tunnels.keys():
            self.add_item(TunnelButton(name))

dashboard_view = TunnelDashboard()

# ============================================================
# EMBED BUILDER
# ============================================================

def build_dashboard_embed():
    embed = discord.Embed(
        title="🛠 Foxhole FAC Tunnels Dashboard",
        color=0x00ff99,
        timestamp=datetime.now(timezone.utc)
    )
    if not tunnels:
        embed.description = "No tunnels available. Use `/add_tunnel` to add one."
        return embed

    for name, data in tunnels.items():
        hours_left = data["total_supplies"] / data["usage_rate"] if data["usage_rate"] > 0 else 0
        embed.add_field(
            name=f"🔧 {name}",
            value=f"Supplies: **{data['total_supplies']}**\nUsage: **{data['usage_rate']}/hr**\nDuration: **{hours_left:.1f} hrs**",
            inline=False
        )
    return embed

async def update_dashboard(channel: discord.TextChannel):
    dashboard_view.refresh_buttons()
    embed = build_dashboard_embed()
    await channel.send(embed=embed, view=dashboard_view)

# ============================================================
# BOT EVENTS
# ============================================================

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    reduce_supplies.start()
    weekly_leaderboard.start()
    bot.add_view(dashboard_view)  # Persist buttons across restarts
    print("🕒 Tasks started and buttons restored successfully.")

# ============================================================
# COMMANDS
# ============================================================

@bot.tree.command(name="add_tunnel", description="Add a new maintenance tunnel.")
async def add_tunnel(interaction: discord.Interaction, name: str, total_supplies: int, usage_rate: int):
    tunnels[name] = {
        "total_supplies": total_supplies,
        "usage_rate": usage_rate,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    save_data(DATA_FILE, tunnels)
    dashboard_view.refresh_buttons()
    await interaction.response.send_message(f"✅ Tunnel **{name}** added successfully.")
    await update_dashboard(interaction.channel)

@bot.tree.command(name="add_supplies", description="Add supplies manually to a tunnel.")
async def add_supplies(interaction: discord.Interaction, name: str, amount: int):
    if name not in tunnels:
        await interaction.response.send_message(f"❌ Tunnel **{name}** not found.")
        return

    tunnels[name]["total_supplies"] += amount
    user_id = str(interaction.user.id)
    users[user_id] = users.get(user_id, 0) + amount

    save_data(DATA_FILE, tunnels)
    save_data(USER_FILE, users)

    await interaction.response.send_message(f"🪣 Added {amount} supplies to **{name}**.")
    await update_dashboard(interaction.channel)

@bot.tree.command(name="dashboard", description="Show the interactive tunnels dashboard.")
async def dashboard(interaction: discord.Interaction):
    dashboard_view.refresh_buttons()
    embed = build_dashboard_embed()
    await interaction.response.send_message(embed=embed, view=dashboard_view)

@bot.tree.command(name="leaderboard", description="Show weekly supply contributors.")
async def leaderboard(interaction: discord.Interaction):
    if not users:
        await interaction.response.send_message("No contributions yet!")
        return

    sorted_users = sorted(users.items(), key=lambda x: x[1], reverse=True)
    desc = ""
    for i, (user_id, total) in enumerate(sorted_users[:10], start=1):
        user = await bot.fetch_user(int(user_id))
        desc += f"**{i}.** {user.display_name} — {total} supplies\n"

    embed = discord.Embed(title="🏆 Weekly Contribution Leaderboard", description=desc, color=0xFFD700)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="end_of_war", description="Officer-only: Show total usage and top 5 contributors.")
async def end_of_war(interaction: discord.Interaction):
    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if officer_role not in interaction.user.roles:
        await interaction.response.send_message("🚫 You do not have permission to use this command.", ephemeral=True)
        return

    total_supplies = sum(u for u in users.values())
    top_5 = sorted(users.items(), key=lambda x: x[1], reverse=True)[:5]
    desc = "\n".join(
        [f"**{i+1}.** {(await bot.fetch_user(int(uid))).display_name} — {amt} supplies" for i, (uid, amt) in enumerate(top_5)]
    )

    embed = discord.Embed(
        title="⚔️ End of War Summary",
        description=f"Total supplies contributed: **{total_supplies}**\n\nTop 5 Contributors:\n{desc}",
        color=0x3498db
    )
    await interaction.response.send_message(embed=embed)

# ============================================================
# TASKS
# ============================================================

@tasks.loop(hours=1)
async def reduce_supplies():
    """Reduces supplies every hour based on usage rate."""
    for name, data in tunnels.items():
        used = data["usage_rate"]
        data["total_supplies"] = max(0, data["total_supplies"] - used)
    save_data(DATA_FILE, tunnels)

@tasks.loop(time=time(hour=12, tzinfo=timezone.utc))
async def weekly_leaderboard():
    """Posts leaderboard every Sunday at 12:00 UTC."""
    now = datetime.now(timezone.utc)
    if now.weekday() != 6:  # Sunday only
        return

    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name="logistics") or discord.utils.get(guild.text_channels, name="general")
        if not channel:
            continue

        if not users:
            await channel.send("📊 No contributions to report this week!")
            continue

        sorted_users = sorted(users.items(), key=lambda x: x[1], reverse=True)
        desc = ""
        for i, (user_id, total) in enumerate(sorted_users[:10], start=1):
            user = await bot.fetch_user(int(user_id))
            desc += f"**{i}.** {user.display_name} — {total} supplies\n"

        embed = discord.Embed(
            title="🏆 Weekly Contribution Leaderboard",
            description=desc,
            color=0xFFD700,
            timestamp=now
        )
        await channel.send(embed=embed)

    users.clear()
    save_data(USER_FILE, users)

# ============================================================
# START BOT
# ============================================================

bot.run(TOKEN)
