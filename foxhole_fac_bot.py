import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from flask import Flask
from threading import Thread

# ================= CONFIG =================
TOKEN = "YOUR_DISCORD_BOT_TOKEN_HERE"
DB_FILE = "foxhole_fac.db"
STATE_FILE = "dashboard_state.json"
TUNNELS_PER_PAGE = 5
# ==========================================

# ---------- Discord Bot Setup ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Flask Keep Alive ----------
app = Flask('')

@app.route('/')
def home():
    return "Foxhole FAC Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ---------- Persistent State ----------
def save_dashboard_state(message_id, channel_id):
    data = {"message_id": message_id, "channel_id": channel_id}
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)

def load_dashboard_state():
    if not os.path.exists(STATE_FILE):
        return None, None
    with open(STATE_FILE, "r") as f:
        data = json.load(f)
        return data.get("message_id"), data.get("channel_id")

DASHBOARD_MESSAGE_ID, DASHBOARD_CHANNEL_ID = load_dashboard_state()

# ---------- Database Setup ----------
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tunnels (
                name TEXT PRIMARY KEY,
                supplies REAL NOT NULL,
                usage_rate REAL NOT NULL,
                last_updated TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS contributions (
                user_id INTEGER,
                supplies_added INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                PRIMARY KEY (user_id, timestamp)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()

# ---------- Usage Decay ----------
async def apply_usage_decay():
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT name, supplies, usage_rate, last_updated FROM tunnels") as cursor:
            tunnels = await cursor.fetchall()

        for name, supplies, usage_rate, last_updated in tunnels:
            if not last_updated:
                continue
            try:
                last_time = datetime.fromisoformat(last_updated)
            except Exception:
                continue

            elapsed_hours = (now - last_time).total_seconds() / 3600
            consumed = usage_rate * elapsed_hours
            if consumed > 0:
                new_supplies = max(0, supplies - consumed)
                if int(new_supplies) != int(supplies):
                    await db.execute(
                        "UPDATE tunnels SET supplies = ?, last_updated = ? WHERE name = ?",
                        (new_supplies, now.isoformat(), name)
                    )
        await db.commit()

# ---------- Slash Commands ----------
@bot.tree.command(name="tunnel_add", description="Add or update a maintenance tunnel.")
async def tunnel_add(interaction: discord.Interaction, name: str, supplies: int, usage_rate: float):
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO tunnels (name, supplies, usage_rate, last_updated)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name)
            DO UPDATE SET supplies=excluded.supplies, usage_rate=excluded.usage_rate, last_updated=excluded.last_updated
        """, (name, supplies, usage_rate, now.isoformat()))
        await db.commit()
    await interaction.response.send_message(f"✅ Tunnel **{name}** set with {supplies} supplies and usage rate {usage_rate}/hr.")
    await update_dashboard()

@bot.tree.command(name="tunnel_info", description="Show info for a tunnel.")
async def tunnel_info(interaction: discord.Interaction, name: str):
    await apply_usage_decay()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT supplies, usage_rate, last_updated FROM tunnels WHERE name = ?", (name,)) as cursor:
            row = await cursor.fetchone()
    if not row:
        await interaction.response.send_message(f"❌ Tunnel '{name}' not found.")
        return

    supplies, usage_rate, last_updated = row
    hours_left = supplies / usage_rate if usage_rate > 0 else float('inf')
    depletion_time = int(time.time() + hours_left * 3600)
    readable = f"{hours_left:.1f} hrs left" if hours_left != float('inf') else "∞"

    embed = discord.Embed(title=f"🧱 Tunnel: {name}", color=discord.Color.blue())
    embed.add_field(name="Supplies", value=str(int(supplies)))
    embed.add_field(name="Usage Rate", value=f"{usage_rate}/hr")
    embed.add_field(name="Time Left", value=readable)
    embed.add_field(name="Depletion", value=f"<t:{depletion_time}:R>")
    embed.set_footer(text=f"Last updated: {last_updated}")
    await interaction.response.send_message(embed=embed)

# ---------- Leaderboard Commands ----------
@bot.tree.command(name="leaderboard", description="Show top contributors.")
async def leaderboard(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("""
            SELECT user_id, SUM(supplies_added)
            FROM contributions
            GROUP BY user_id
            ORDER BY SUM(supplies_added) DESC
            LIMIT 10
        """) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await interaction.response.send_message("No contributions yet.")
        return

    embed = discord.Embed(title="🏆 Supply Leaderboard", color=discord.Color.gold())
    for i, (user_id, amount) in enumerate(rows, start=1):
        user = await bot.fetch_user(user_id)
        embed.add_field(name=f"#{i} {user.display_name}", value=f"{int(amount)} supplies", inline=False)
    await interaction.response.send_message(embed=embed)

# ---------- Dashboard UI ----------
from discord.ui import View, Button

class TunnelButton(Button):
    def __init__(self, tunnel_name: str, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.blurple)
        self.tunnel_name = tunnel_name

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT supplies FROM tunnels WHERE name = ?", (self.tunnel_name,)) as cursor:
                row = await cursor.fetchone()
            if not row:
                await interaction.response.send_message(f"❌ Tunnel '{self.tunnel_name}' not found.", ephemeral=True)
                return

            supplies = row[0]
            new_total = supplies + 1500

            await db.execute(
                "UPDATE tunnels SET supplies = ?, last_updated = ? WHERE name = ?",
                (new_total, now.isoformat(), self.tunnel_name)
            )
            now_iso = now.isoformat()
            await db.execute("""
                INSERT INTO contributions (user_id, supplies_added, timestamp)
                VALUES (?, 1500, ?)
            """, (user.id, now_iso))
            await db.commit()

        await interaction.response.send_message(f"🪣 +1500 supplies added to **{self.tunnel_name}** by {user.mention}! New total: {int(new_total)}.", ephemeral=True)
        await update_dashboard()

class PaginationButton(Button):
    def __init__(self, label: str, direction: str):
        super().__init__(label=label, style=discord.ButtonStyle.gray)
        self.direction = direction

    async def callback(self, interaction: discord.Interaction):
        view: TunnelDashboard = self.view
        if self.direction == "prev":
            view.page = max(view.page - 1, 0)
        elif self.direction == "next":
            view.page = min(view.page + 1, view.max_pages - 1)
        embed, new_view = await generate_dashboard_embed(page=view.page)
        await interaction.response.edit_message(embed=embed, view=new_view)

class TunnelDashboard(View):
    def __init__(self, tunnels, page=0):
        super().__init__(timeout=None)
        self.page = page
        self.max_pages = (len(tunnels) + TUNNELS_PER_PAGE - 1) // TUNNELS_PER_PAGE
        self.tunnels = tunnels

        start = page * TUNNELS_PER_PAGE
        end = start + TUNNELS_PER_PAGE
        tunnels_page = tunnels[start:end]

        for i, (name, _, _) in enumerate(tunnels_page):
            label = chr(65 + i + start)
            button = TunnelButton(name, label)
            self.add_item(button)

        if self.max_pages > 1:
            self.add_item(PaginationButton("⏮️ Prev", "prev"))
            self.add_item(PaginationButton("Next ⏭️", "next"))

async def generate_dashboard_embed(page=0):
    await apply_usage_decay()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT name, supplies, usage_rate FROM tunnels ORDER BY name ASC") as cursor:
            tunnels = await cursor.fetchall()

    embed = discord.Embed(
        title="🔧 Foxhole FAC Dashboard",
        description="Click a button below to add +1500 supplies to a tunnel.",
        color=discord.Color.dark_gray(),
        timestamp=datetime.now(timezone.utc)
    )

    if not tunnels:
        embed.add_field(name="No tunnels found", value="Use `/tunnel_add` to register a tunnel.", inline=False)
        return embed, None

    start = page * TUNNELS_PER_PAGE
    end = start + TUNNELS_PER_PAGE
    tunnels_page = tunnels[start:end]

    for i, (name, supplies, usage_rate) in enumerate(tunnels_page, start=start + 1):
        label = chr(64 + i)
        hours_left = supplies / usage_rate if usage_rate > 0 else float("inf")
        depletion_time = int(time.time() + hours_left * 3600)
        readable = f"{hours_left:.1f} hrs left" if hours_left != float("inf") else "∞"
        footer = f"🧱 Supplies: **{int(supplies)}** | 🔧 Rate: `{usage_rate}/hr`\n⏳ {readable}\n🕒 Depletes {f'<t:{depletion_time}:R>' if hours_left != float('inf') else '∞'}"
        embed.add_field(name=f"{label}. {name}", value=footer, inline=False)

    embed.set_footer(text=f"Page {page + 1}/{(len(tunnels) + TUNNELS_PER_PAGE - 1) // TUNNELS_PER_PAGE}")
    view = TunnelDashboard(tunnels, page)
    return embed, view

@bot.tree.command(name="tunnel_dashboard", description="Display the interactive, persistent tunnel dashboard.")
async def tunnel_dashboard(interaction: discord.Interaction):
    global DASHBOARD_MESSAGE_ID, DASHBOARD_CHANNEL_ID
    embed, view = await generate_dashboard_embed(page=0)
    await interaction.response.send_message(embed=embed, view=view)
    sent = await interaction.original_response()
    DASHBOARD_MESSAGE_ID = sent.id
    DASHBOARD_CHANNEL_ID = sent.channel.id
    save_dashboard_state(DASHBOARD_MESSAGE_ID, DASHBOARD_CHANNEL_ID)
    await interaction.followup.send("✅ Persistent dashboard initialized!", ephemeral=True)

# ---------- Weekly & War Summary ----------
async def get_log_channel():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT value FROM settings WHERE key = 'log_channel'") as cursor:
            row = await cursor.fetchone()
            if row:
                return int(row[0])
    return None

@bot.tree.command(name="set_log_channel", description="Set the channel where weekly leaderboards are posted.")
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO settings (key, value)
            VALUES ('log_channel', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (str(channel.id),))
        await db.commit()
    await interaction.response.send_message(f"✅ Weekly log channel set to {channel.mention}.")

@bot.tree.command(name="end_war_report", description="Generate an end-of-war contribution report (Officer only).")
async def end_war_report(interaction: discord.Interaction):
    officer_role = discord.utils.get(interaction.guild.roles, name="Officer")
    if officer_role not in interaction.user.roles:
        await interaction.response.send_message("🚫 You must have the 'Officer' role to use this command.", ephemeral=True)
        return

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT SUM(supplies_added) FROM contributions") as cursor:
            total_supplies = await cursor.fetchone()
        total_supplies = total_supplies[0] or 0

        async with db.execute("""
            SELECT user_id, SUM(supplies_added) as total
            FROM contributions
            GROUP BY user_id
            ORDER BY total DESC
            LIMIT 5
        """) as cursor:
            top = await cursor.fetchall()

    embed = discord.Embed(
        title="⚔️ End of War Report",
        description="Final supply totals for the campaign",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Total Supplies Used", value=f"{int(total_supplies)}", inline=False)
    for i, (user_id, total) in enumerate(top, start=1):
        user = await bot.fetch_user(user_id)
        embed.add_field(name=f"#{i} {user.display_name}", value=f"{int(total)} supplies", inline=False)

    await interaction.response.send_message(embed=embed)

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM contributions")
        await db.commit()

@tasks.loop(minutes=2)
async def refresh_dashboard_loop():
    global DASHBOARD_MESSAGE_ID, DASHBOARD_CHANNEL_ID
    await apply_usage_decay()
    if not DASHBOARD_MESSAGE_ID or not DASHBOARD_CHANNEL_ID:
        return
    try:
        channel = bot.get_channel(DASHBOARD_CHANNEL_ID)
        if not channel:
            return
        message = await channel.fetch_message(DASHBOARD_MESSAGE_ID)
        embed, view = await generate_dashboard_embed(page=0)
        await message.edit(embed=embed, view=view)
        print(f"🔁 Dashboard refreshed at {datetime.now(timezone.utc).isoformat()}")
    except Exception as e:
        print(f"⚠️ Dashboard refresh failed: {e}")

# Weekly leaderboard posting (Sunday 12:00 UTC)
@tasks.loop(hours=1)
async def weekly_leaderboard_check():
    now = datetime.now(timezone.utc)
    if now.weekday() == 6 and now.hour == 12:  # Sunday 12:00 UTC
        log_channel_id = await get_log_channel()
        if not log_channel_id:
            return
        channel = bot.get_channel(log_channel_id)
        if not channel:
            return

        one_week_ago = datetime.now(timezone.utc).timestamp() - 7 * 24 * 3600
        cutoff = datetime.fromtimestamp(one_week_ago, tz=timezone.utc).isoformat()

        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("""
                SELECT user_id, SUM(supplies_added)
                FROM contributions
                WHERE timestamp >= ?
                GROUP BY user_id
                ORDER BY SUM(supplies_added) DESC
                LIMIT 10
            """, (cutoff,)) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return

        embed = discord.Embed(
            title="📅 Weekly Supply Leaderboard",
            description="Top contributors from the past 7 days",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )

        for i, (user_id, amount) in enumerate(rows, start=1):
            user = await bot.fetch_user(user_id)
            embed.add_field(name=f"#{i} {user.display_name}", value=f"{int(amount)} supplies", inline=False)

        await channel.send(embed=embed)
        print(f"📊 Weekly leaderboard posted to #{channel.name}")

async def update_dashboard():
    global DASHBOARD_MESSAGE_ID, DASHBOARD_CHANNEL_ID
    if not DASHBOARD_MESSAGE_ID or not DASHBOARD_CHANNEL_ID:
        return
    try:
        channel = bot.get_channel(DASHBOARD_CHANNEL_ID)
        message = await channel.fetch_message(DASHBOARD_MESSAGE_ID)
        embed, view = await generate_dashboard_embed(page=0)
        await message.edit(embed=embed, view=view)
    except Exception as e:
        print(f"⚠️ Manual dashboard update failed: {e}")

@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    refresh_dashboard_loop.start()
    weekly_leaderboard_check.start()
    print(f"✅ Logged in as {bot.user} (Foxhole FAC Bot)")
    if DASHBOARD_MESSAGE_ID and DASHBOARD_CHANNEL_ID:
        print("📦 Restored dashboard from previous session!")

# ---------- Run Bot ----------
keep_alive()
bot.run(TOKEN)
