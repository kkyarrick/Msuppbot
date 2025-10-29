import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone
import json
import os

# ============================================================
# CONFIGURATION
# ============================================================
DATA_FILE = "tunnels.json"
USER_FILE = "users.json"

# Intents setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # Needed for Officer role checking
bot = commands.Bot(command_prefix="/", intents=intents)

# Load bot token securely from environment variable
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
# BOT EVENTS
# ============================================================

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    weekly_leaderboard.start()  # Start scheduled task

# ============================================================
# BOT COMMANDS
# ============================================================

@bot.tree.command(name="add_tunnel", description="Add a new maintenance tunnel.")
async def add_tunnel(interaction: discord.Interaction, name: str, total_supplies: int, usage_rate: int):
    tunnels[name] = {
        "total_supplies": total_supplies,
        "usage_rate": usage_rate,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    save_data(DATA_FILE, tunnels)
    await interaction.response.send_message(f"✅ Tunnel **{name}** added with {total_supplies} supplies and usage rate {usage_rate}/hr.")
    await update_dashboard(interaction)

@bot.tree.command(name="add_supplies", description="Add supplies to a tunnel and leaderboard.")
async def add_supplies(interaction: discord.Interaction, name: str, amount: int):
    if name not in tunnels:
        await interaction.response.send_message(f"❌ Tunnel **{name}** not found.")
        return

    tunnels[name]["total_supplies"] += amount
    save_data(DATA_FILE, tunnels)

    user_id = str(interaction.user.id)
    users[user_id] = users.get(user_id, 0) + amount
    save_data(USER_FILE, users)

    await interaction.response.send_message(f"🪣 Added {amount} supplies to **{name}**.")
    await update_dashboard(interaction)

@bot.tree.command(name="dashboard", description="Show the tunnels dashboard.")
async def dashboard(interaction: discord.Interaction):
    embed = discord.Embed(title="🛠 Foxhole FAC Tunnels", color=0x00ff99, timestamp=datetime.now(timezone.utc))
    for name, data in tunnels.items():
        hours_left = data["total_supplies"] / data["usage_rate"] if data["usage_rate"] > 0 else 0
        embed.add_field(
            name=f"🔧 {name}",
            value=f"Supplies: **{data['total_supplies']}**\nUsage: **{data['usage_rate']}/hr**\nDuration: **{hours_left:.1f} hrs**",
            inline=False
        )
    await interaction.response.send_message(embed=embed)

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
# UTILITY FUNCTIONS
# ============================================================

async def update_dashboard(interaction: discord.Interaction):
    embed = discord.Embed(title="🛠 Foxhole FAC Tunnels (Updated)", color=0x00ff99, timestamp=datetime.now(timezone.utc))
    for name, data in tunnels.items():
        hours_left = data["total_supplies"] / data["usage_rate"] if data["usage_rate"] > 0 else 0
        embed.add_field(
            name=f"🔧 {name}",
            value=f"Supplies: **{data['total_supplies']}**\nUsage: **{data['usage_rate']}/hr**\nDuration: **{hours_left:.1f} hrs**",
            inline=False
        )
    await interaction.channel.send(embed=embed)

# ============================================================
# TASKS
# ============================================================

@tasks.loop(hours=1)
async def reduce_supplies():
    """Reduces supplies every hour based on usage rate."""
    now = datetime.now(timezone.utc)
    for name, data in tunnels.items():
        used = data["usage_rate"]
        data["total_supplies"] = max(0, data["total_supplies"] - used)
    save_data(DATA_FILE, tunnels)

@tasks.loop(time=datetime.time(hour=12, tzinfo=timezone.utc))
async def weekly_leaderboard():
    """Posts leaderboard every Sunday at 12:00 UTC."""
    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name="logistics" or "general")
        if channel:
            sorted_users = sorted(users.items(), key=lambda x: x[1], reverse=True)
            desc = ""
            for i, (user_id, total) in enumerate(sorted_users[:10], start=1):
                user = await bot.fetch_user(int(user_id))
                desc += f"**{i}.** {user.display_name} — {total} supplies\n"

            embed = discord.Embed(title="🏆 Weekly Contribution Leaderboard", description=desc, color=0xFFD700)
            await channel.send(embed=embed)
    users.clear()
    save_data(USER_FILE, users)

# ============================================================
# START BOT
# ============================================================

reduce_supplies.start()
bot.run(TOKEN)
