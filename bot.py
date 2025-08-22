"""
Livia Bot — Marble Isles (discord.py + asyncpg Postgres)
- Persistent data in Postgres (DATABASE_URL env var)
- Mind/Body/Soul (5/3/1), Origins, cores, skills (0–3)
- Rolls: d20 + skill + governing stat  → ⭐ Total
- Wallet, shop, inventory
- GM tools (grant money/items) + JSON backup/restore
- Render (Free Web Service): uses keep_alive to bind $PORT
"""

import os, random, json, io
from typing import Dict, Optional, List, Tuple

import discord
from discord.ext import commands
from discord import app_commands
import asyncpg

# ---- Basic config
BOT_NAME = "Livia Bot"
LOCATION = "Marble Isles"
CURRENCY = "Doubloons"
STAR = "⭐"

# ---- Skills & governing stats
MIND_SKILLS = ["lore", "streetwise", "persuasion", "ranged_weapons"]
BODY_SKILLS = ["melee_weapons", "dance", "evasion", "brawling"]
SOUL_SKILLS = ["religion", "clairvoyance", "drug_tolerance", "exorcism"]

SKILL_TO_STAT: Dict[str, str] = {
    **{s: "Mind" for s in MIND_SKILLS},
    **{s: "Body" for s in BODY_SKILLS},
    **{s: "Soul" for s in SOUL_SKILLS},
}

# ---- Simple shop catalogue (edit as you like)
SHOP: Dict[str, int] = {
    "formal_outfit": 120,
    "common_outfit": 40,
    "work_outfit": 60,
    "ragged_outfit": 10,
    "pistol": 200,          # 1d6
    "dagger": 80,           # 1d6
    "healing_salves": 30,
}

def slug(s: str) -> str:
    return s.strip().lower().replace(" ", "_")

# ===================== DB (Postgres) =====================
CREATE_SQL = [
    # Discord IDs are 64-bit -> BIGINT
    """
    CREATE TABLE IF NOT EXISTS characters (
        guild_id BIGINT,
        user_id  BIGINT,
        name TEXT,
        mind INTEGER,
        body INTEGER,
        soul INTEGER,
        sanity INTEGER,
        health INTEGER,
        spirit INTEGER,
        max_sanity INTEGER,
        max_health INTEGER,
        max_spirit INTEGER,
        wallet INTEGER DEFAULT 0,
        unassigned_points INTEGER DEFAULT 10,
        PRIMARY KEY (guild_id, user_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS skills (
        guild_id BIGINT,
        user_id  BIGINT,
        skill TEXT,
        points INTEGER,
        PRIMARY KEY (guild_id, user_id, skill)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory (
        guild_id BIGINT,
        user_id  BIGINT,
        item TEXT,
        qty INTEGER,
        PRIMARY KEY (guild_id, user_id, item)
    );
    """,
]

async def init_db(pool: asyncpg.Pool):
    async with pool.acquire() as con:
        for sql in CREATE_SQL:
            await con.execute(sql)

async def fetch_char(pool: asyncpg.Pool, guild_id: int, user_id: int) -> Optional[asyncpg.Record]:
    async with pool.acquire() as con:
        return await con.fetchrow(
            "SELECT * FROM characters WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id
        )

async def ensure_skill_row(pool: asyncpg.Pool, guild_id: int, user_id: int, skill: str):
    s = slug(skill)
    async with pool.acquire() as con:
        r = await con.fetchrow(
            "SELECT points FROM skills WHERE guild_id=$1 AND user_id=$2 AND skill=$3",
            guild_id, user_id, s
        )
        if r is None:
            await con.execute(
                "INSERT INTO skills (guild_id, user_id, skill, points) VALUES ($1,$2,$3,0)",
                guild_id, user_id, s
            )

async def get_skill_points(pool: asyncpg.Pool, guild_id: int, user_id: int, skill: str) -> int:
    s = slug(skill)
    await ensure_skill_row(pool, guild_id, user_id, s)
    async with pool.acquire() as con:
        r = await con.fetchrow(
            "SELECT points FROM skills WHERE guild_id=$1 AND user_id=$2 AND skill=$3",
            guild_id, user_id, s
        )
        return int(r["points"]) if r else 0

async def add_skill_points(pool: asyncpg.Pool, guild_id: int, user_id: int, skill: str, amount: int) -> Tuple[int,int]:
    """Returns (new_points, spent_from_pool). Caps at 3 per skill, draws from unassigned_points."""
    s = slug(skill)
    await ensure_skill_row(pool, guild_id, user_id, s)
    async with pool.acquire() as con:
        ch = await con.fetchrow(
            "SELECT unassigned_points FROM characters WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id
        )
        if ch is None:
            raise ValueError("Character not found")
        pool_pts = int(ch["unassigned_points"])
        r = await con.fetchrow(
            "SELECT points FROM skills WHERE guild_id=$1 AND user_id=$2 AND skill=$3",
            guild_id, user_id, s
        )
        current = int(r["points"]) if r else 0
        can_add = max(0, min(3 - current, amount, pool_pts))
        new_points = current + can_add
        new_pool = pool_pts - can_add
        await con.execute(
            "UPDATE skills SET points=$1 WHERE guild_id=$2 AND user_id=$3 AND skill=$4",
            new_points, guild_id, user_id, s
        )
        await con.execute(
            "UPDATE characters SET unassigned_points=$1 WHERE guild_id=$2 AND user_id=$3",
            new_pool, guild_id, user_id
        )
        return new_points, can_add

async def add_item(pool: asyncpg.Pool, guild_id: int, user_id: int, item: str, qty: int=1):
    i = slug(item)
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO inventory (guild_id, user_id, item, qty) VALUES ($1,$2,$3,$4)
            ON CONFLICT (guild_id, user_id, item)
            DO UPDATE SET qty = inventory.qty + EXCLUDED.qty
            """,
            guild_id, user_id, i, qty
        )

async def list_inventory(pool: asyncpg.Pool, guild_id: int, user_id: int) -> List[asyncpg.Record]:
    async with pool.acquire() as con:
        return await con.fetch(
            "SELECT item, qty FROM inventory WHERE guild_id=$1 AND user_id=$2 ORDER BY item",
            guild_id, user_id
        )

# ===================== Bot =====================
class Livia(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.pool: Optional[asyncpg.Pool] = None

    async def setup_hook(self):
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise RuntimeError("Set DATABASE_URL environment variable")
        # Render/Neon/Supabase require SSL
        if "sslmode" not in db_url:
            join = "&" if "?" in db_url else "?"
            db_url = db_url + f"{join}sslmode=require"
        self.pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)
        await init_db(self.pool)
        await self.tree.sync()

bot = Livia()

@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=f"{LOCATION} • /sheet"))
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ===================== Helpers =====================
def is_gm(interaction: discord.Interaction) -> bool:
    m = interaction.user
    if isinstance(m, discord.Member):
        p = m.guild_permissions
        return p.administrator or p.manage_guild
    return False

def pool() -> asyncpg.Pool:
    assert bot.pool is not None, "DB pool not ready"
    return bot.pool

# ===================== Commands =====================
@bot.tree.command(description="Create your character (5/3/1 + Origin)")
@app_commands.describe(
    name="Character name",
    primary="Which stat is 5?",
    secondary="Which stat is 3? (remaining becomes 1)",
    origin="Your Origin",
    streetrat_weapon="If Streetrat, pick a starting weapon"
)
@app_commands.choices(
    primary=[app_commands.Choice(name=x, value=x) for x in ["Mind", "Body", "Soul"]],
    secondary=[app_commands.Choice(name=x, value=x) for x in ["Mind", "Body", "Soul"]],
    origin=[
        app_commands.Choice(name="Noble", value="noble"),
        app_commands.Choice(name="Citizen", value="citizen"),
        app_commands.Choice(name="Country", value="country"),
        app_commands.Choice(name="Streetrat", value="streetrat"),
    ],
    streetrat_weapon=[
        app_commands.Choice(name="Pistol (1d6)", value="pistol"),
        app_commands.Choice(name="Dagger (1d6)", value="dagger"),
    ],
)
async def create(
    interaction: discord.Interaction,
    name: str,
    primary: app_commands.Choice[str],
    secondary: app_commands.Choice[str],
    origin: app_commands.Choice[str],
    streetrat_weapon: Optional[app_commands.Choice[str]] = None,
):
    if primary.value == secondary.value:
        return await interaction.response.send_message("Primary and secondary must be different.", ephemeral=True)

    p = pool()
    guild_id = interaction.guild_id
    user_id = interaction.user.id

    async with p.acquire() as con:
        existing = await fetch_char(p, guild_id, user_id)  # type: ignore
        if existing:
            return await interaction.response.send_message("You already have a character. Use /sheet or ask a GM to reset.", ephemeral=True)

        stats = {"Mind": 1, "Body": 1, "Soul": 1}
        stats[primary.value] = 5
        stats[secondary.value] = 3

        max_sanity = stats["Mind"] * 2
        max_health = stats["Body"] * 2
        max_spirit = stats["Soul"] * 2

        wallet = 0
        origin_text = origin.value
        origin_bonuses: List[Tuple[str, int]] = []
        start_items: List[Tuple[str, int]] = []

        if origin_text == "noble":
            wallet = 1000
            start_items.append(("formal_outfit", 1))
            origin_bonuses += [("persuasion", 1), ("dance", 1)]
        elif origin_text == "citizen":
            wallet = 400
            start_items.append(("common_outfit", 1))
            origin_bonuses += [("lore", 1), ("religion", 1), ("persuasion", 1)]
        elif origin_text == "country":
            wallet = 400
            start_items.append(("work_outfit", 1))
            origin_bonuses += [("ranged_weapons", 1), ("evasion", 1), ("drug_tolerance", 1)]
        elif origin_text == "streetrat":
            wallet = 10
            start_items.append(("ragged_outfit", 1))
            weap = streetrat_weapon.value if streetrat_weapon else "pistol"
            start_items.append((weap, 1))
            origin_bonuses += [("streetwise", 1), ("melee_weapons", 1), ("brawling", 1), ("drug_tolerance", 1)]

        await con.execute(
            """
            INSERT INTO characters (guild_id, user_id, name, mind, body, soul, sanity, health, spirit,
                                    max_sanity, max_health, max_spirit, wallet)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            """,
            guild_id, user_id, name,
            stats["Mind"], stats["Body"], stats["Soul"],
            max_sanity, max_health, max_spirit,
            max_sanity, max_health, max_spirit,
            wallet
        )

        # ensure skills at 0, then add origin bonuses (cap 3) without consuming pool
        for s in SKILL_TO_STAT.keys():
            await ensure_skill_row(p, guild_id, user_id, s)  # type: ignore
        for (sk, amt) in origin_bonuses:
            r = await con.fetchrow(
                "SELECT points FROM skills WHERE guild_id=$1 AND user_id=$2 AND skill=$3",
                guild_id, user_id, sk
            )
            current = int(r["points"]) if r else 0
            newv = min(3, current + amt)
            await con.execute(
                "UPDATE skills SET points=$1 WHERE guild_id=$2 AND user_id=$3 AND skill=$4",
                newv, guild_id, user_id, sk
            )
        for (it, q) in start_items:
            await add_item(p, guild_id, user_id, it, q)

    await interaction.response.send_message(
        f"**{name}** is registered in the {LOCATION}! You have **10 skill points** to distribute with `/skill_add`.",
        ephemeral=True,
    )

@bot.tree.command(description="View your character sheet")
async def sheet(interaction: discord.Interaction):
    p = pool()
    ch = await fetch_char(p, interaction.guild_id, interaction.user.id)  # type: ignore
    if not ch:
        return await interaction.response.send_message("No character yet. Use /create first.", ephemeral=True)

    async with p.acquire() as con:
        rows = await con.fetch(
            "SELECT skill, points FROM skills WHERE guild_id=$1 AND user_id=$2 ORDER BY skill",
            interaction.guild_id, interaction.user.id
        )
        skills_text = ", ".join([f"{r['skill']} {r['points']}" for r in rows if r["points"] > 0]) or "(none)"
        inv = await list_inventory(p, interaction.guild_id, interaction.user.id)  # type: ignore
        inv_text = ", ".join([f"{r['item']}×{r['qty']}" for r in inv]) or "(empty)"

    embed = discord.Embed(title=f"{BOT_NAME} — Character Sheet", color=discord.Color.purple())
    embed.add_field(name="Name", value=ch["name"], inline=True)
    embed.add_field(name="Wallet", value=f"{ch['wallet']} {CURRENCY}", inline=True)
    embed.add_field(name="Unspent Skill Pts", value=str(ch["unassigned_points"]))
    embed.add_field(name="Mind", value=str(ch["mind"]))
    embed.add_field(name="Body", value=str(ch["body"]))
    embed.add_field(name="Soul", value=str(ch["soul"]))
    embed.add_field(name="Sanity", value=f"{ch['sanity']}/{ch['max_sanity']}")
    embed.add_field(name="Health", value=f"{ch['health']}/{ch['max_health']}")
    embed.add_field(name="Spirit", value=f"{ch['spirit']}/{ch['max_spirit']}")
    embed.add_field(name="Skills", value=skills_text, inline=False)
    embed.add_field(name="Inventory", value=inv_text, inline=False)
    embed.set_footer(text=LOCATION)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(description="Add points to a skill (cap 3, spends your pool)")
@app_commands.describe(skill="Skill name (e.g., exorcism)", amount="How many points to add")
async def skill_add(interaction: discord.Interaction, skill: str, amount: int):
    s = slug(skill)
    if s not in SKILL_TO_STAT:
        valid = ", ".join(sorted(SKILL_TO_STAT.keys()))
        return await interaction.response.send_message(f"Unknown skill. Try: {valid}", ephemeral=True)

    p = pool()
    ch = await fetch_char(p, interaction.guild_id, interaction.user.id)  # type: ignore
    if not ch:
        return await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
    newv, spent = await add_skill_points(p, interaction.guild_id, interaction.user.id, s, amount)  # type: ignore
    await interaction.response.send_message(
        f"Added {spent} to **{s}** → now {newv}. ({STAR} your pool decreased.)",
        ephemeral=True,
    )

@bot.tree.command(description="Roll d20 + skill + governing stat")
@app_commands.describe(skill="Skill to roll (e.g., exorcism)")
async def roll(interaction: discord.Interaction, skill: str):
    s = slug(skill)
    if s not in SKILL_TO_STAT:
        valid = ", ".join(sorted(SKILL_TO_STAT.keys()))
        return await interaction.response.send_message(f"Unknown skill. Try: {valid}", ephemeral=True)

    p = pool()
    ch = await fetch_char(p, interaction.guild_id, interaction.user.id)  # type: ignore
    if not ch:
        return await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
    pts = await get_skill_points(p, interaction.guild_id, interaction.user.id, s)  # type: ignore

    stat_name = SKILL_TO_STAT[s]
    stat_value = int(ch[stat_name.lower()])  # mind/body/soul
    d20 = random.randint(1, 20)
    total = d20 + pts + stat_value
    nat = " (CRIT!)" if d20 == 20 else (" (botch)" if d20 == 1 else "")

    embed = discord.Embed(title=f"{interaction.user.display_name} rolls {s}", color=discord.Color.dark_teal())
    embed.add_field(name="d20", value=str(d20) + nat)
    embed.add_field(name="Skill", value=str(pts))
    embed.add_field(name=stat_name, value=str(stat_value))
    embed.add_field(name=f"Total {STAR}", value=f"**{total}**", inline=False)
    embed.set_footer(text=LOCATION)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(description="Apply damage to Sanity/Health/Spirit")
@app_commands.describe(kind="Which attribute", amount="How much damage")
@app_commands.choices(kind=[
    app_commands.Choice(name="Sanity", value="sanity"),
    app_commands.Choice(name="Health", value="health"),
    app_commands.Choice(name="Spirit", value="spirit"),
])
async def damage(interaction: discord.Interaction, kind: app_commands.Choice[str], amount: int):
    p = pool()
    ch = await fetch_char(p, interaction.guild_id, interaction.user.id)  # type: ignore
    if not ch:
        return await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
    field = kind.value
    newv = max(0, int(ch[field]) - amount)
    async with p.acquire() as con:
        await con.execute(
            f"UPDATE characters SET {field}=$1 WHERE guild_id=$2 AND user_id=$3",
            newv, interaction.guild_id, interaction.user.id
        )

    end_text = ""
    if field == "health" and newv == 0:
        end_text = " You **die**."
    elif field == "sanity" and newv == 0:
        end_text = " You go **insane**."
    elif field == "spirit" and newv == 0:
        end_text = " You become **possessed**."

    await interaction.response.send_message(f"{kind.name} now **{newv}**/{ch[f'max_{field}']}.")
    if end_text:
        await interaction.followup.send(end_text)

@bot.tree.command(description="Heal Sanity/Health/Spirit (clamped to max)")
@app_commands.describe(kind="Which attribute", amount="How much healing")
@app_commands.choices(kind=[
    app_commands.Choice(name="Sanity", value="sanity"),
    app_commands.Choice(name="Health", value="health"),
    app_commands.Choice(name="Spirit", value="spirit"),
])
async def heal(interaction: discord.Interaction, kind: app_commands.Choice[str], amount: int):
    p = pool()
    ch = await fetch_char(p, interaction.guild_id, interaction.user.id)  # type: ignore
    if not ch:
        return await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
    field = kind.value
    maxv = int(ch[f"max_{field}"])
    newv = min(maxv, int(ch[field]) + amount)
    async with p.acquire() as con:
        await con.execute(
            f"UPDATE characters SET {field}=$1 WHERE guild_id=$2 AND user_id=$3",
            newv, interaction.guild_id, interaction.user.id
        )
    await interaction.response.send_message(f"{kind.name} now **{newv}**/{maxv}.")

@bot.tree.command(description="Check your wallet balance")
async def wallet(interaction: discord.Interaction):
    p = pool()
    ch = await fetch_char(p, interaction.guild_id, interaction.user.id)  # type: ignore
    if not ch:
        return await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
    await interaction.response.send_message(f"You have **{ch['wallet']} {CURRENCY}**.")

@bot.tree.command(description="List shop items and prices")
async def shop(interaction: discord.Interaction):
    lines = [f"• {k} — {v} {CURRENCY}" for k, v in SHOP.items()]
    await interaction.response.send_message("**Shop**\n" + "\n".join(lines))

@bot.tree.command(description="Buy an item from the shop")
@app_commands.describe(item="Item name", qty="Quantity")
async def buy(interaction: discord.Interaction, item: str, qty: int = 1):
    it = slug(item)
    if it not in SHOP:
        return await interaction.response.send_message("That item isn't in stock.", ephemeral=True)
    qty = max(1, qty)
    cost = SHOP[it] * qty
    p = pool()
    ch = await fetch_char(p, interaction.guild_id, interaction.user.id)  # type: ignore
    if not ch:
        return await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
    if ch["wallet"] < cost:
        return await interaction.response.send_message("You can't afford that.", ephemeral=True)
    async with p.acquire() as con:
        await con.execute(
            "UPDATE characters SET wallet = wallet - $1 WHERE guild_id=$2 AND user_id=$3",
            cost, interaction.guild_id, interaction.user.id
        )
    await add_item(p, interaction.guild_id, interaction.user.id, it, qty)  # type: ignore
    await interaction.response.send_message(f"Purchased **{qty}× {it}** for **{cost} {CURRENCY}**.")

@bot.tree.command(description="Show your inventory")
async def inventory(interaction: discord.Interaction):
    inv = await list_inventory(pool(), interaction.guild_id, interaction.user.id)  # type: ignore
    text = "\n".join([f"• {r['item']}×{r['qty']}" for r in inv]) or "(empty)"
    await interaction.response.send_message("**Inventory**\n" + text, ephemeral=True)

# ---- GM utilities ----
@bot.tree.command(description="[GM] Give money to a character")
@app_commands.describe(member="Who gets the money", amount="How many Doubloons")
async def gm_give(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not is_gm(interaction):
        return await interaction.response.send_message("GM only.", ephemeral=True)
    p = pool()
    ch = await fetch_char(p, interaction.guild_id, member.id)  # type: ignore
    if not ch:
        return await interaction.response.send_message("That user has no character.", ephemeral=True)
    async with p.acquire() as con:
        await con.execute(
            "UPDATE characters SET wallet = wallet + $1 WHERE guild_id=$2 AND user_id=$3",
            amount, interaction.guild_id, member.id
        )
    await interaction.response.send_message(f"Granted **{amount} {CURRENCY}** to {member.display_name}.")

@bot.tree.command(description="[GM] Add item to a character's inventory")
@app_commands.describe(member="Target", item="Item name", qty="Quantity")
async def gm_additem(interaction: discord.Interaction, member: discord.Member, item: str, qty: int = 1):
    if not is_gm(interaction):
        return await interaction.response.send_message("GM only.", ephemeral=True)
    p = pool()
    ch = await fetch_char(p, interaction.guild_id, member.id)  # type: ignore
    if not ch:
        return await interaction.response.send_message("That user has no character.", ephemeral=True)
    await add_item(p, interaction.guild_id, member.id, item, qty)  # type: ignore
    await interaction.response.send_message(f"Gave **{qty}× {slug(item)}** to {member.display_name}.")

# ---- GM BACKUP / RESTORE (JSON file) ----
@bot.tree.command(description="[GM] Export all character data (JSON)")
async def gm_backup(interaction: discord.Interaction):
    if not is_gm(interaction):
        return await interaction.response.send_message("GM only.", ephemeral=True)

    p = pool()
    gid = interaction.guild_id
    data = {"characters": [], "skills": [], "inventory": []}
    async with p.acquire() as con:
        rows = await con.fetch("SELECT * FROM characters WHERE guild_id=$1", gid)
        data["characters"] = [dict(r) for r in rows]
        rows = await con.fetch("SELECT * FROM skills WHERE guild_id=$1", gid)
        data["skills"] = [dict(r) for r in rows]
        rows = await con.fetch("SELECT * FROM inventory WHERE guild_id=$1", gid)
        data["inventory"] = [dict(r) for r in rows]

    buf = io.BytesIO(json.dumps(data, indent=2).encode("utf-8"))
    buf.seek(0)
    await interaction.response.send_message(
        "Here is your backup. Keep it safe before redeploys!",
        file=discord.File(buf, filename="livia_backup.json"),
        ephemeral=True,
    )

@bot.tree.command(description="[GM] Restore data from backup JSON (overwrites this server)")
@app_commands.describe(file="Upload livia_backup.json")
async def gm_restore(interaction: discord.Interaction, file: discord.Attachment):
    if not is_gm(interaction):
        return await interaction.response.send_message("GM only.", ephemeral=True)
    if not file.filename.lower().endswith(".json"):
        return await interaction.response.send_message("Please upload a .json file.", ephemeral=True)

    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return await interaction.response.send_message(f"Invalid JSON: {e}", ephemeral=True)

    gid = interaction.guild_id
    p = pool()
    async with p.acquire() as con:
        await con.execute("DELETE FROM inventory WHERE guild_id=$1", gid)
        await con.execute("DELETE FROM skills WHERE guild_id=$1", gid)
        await con.execute("DELETE FROM characters WHERE guild_id=$1", gid)

        for row in data.get("characters", []):
            if row.get("guild_id") != gid: continue
            await con.execute("""
                INSERT INTO characters (guild_id, user_id, name, mind, body, soul,
                    sanity, health, spirit, max_sanity, max_health, max_spirit,
                    wallet, unassigned_points)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            """, row["guild_id"], row["user_id"], row["name"], row["mind"], row["body"], row["soul"],
                 row["sanity"], row["health"], row["spirit"], row["max_sanity"], row["max_health"], row["max_spirit"],
                 row.get("wallet", 0), row.get("unassigned_points", 10))

        for row in data.get("skills", []):
            if row.get("guild_id") != gid: continue
            await con.execute(
                "INSERT INTO skills (guild_id, user_id, skill, points) VALUES ($1,$2,$3,$4)",
                row["guild_id"], row["user_id"], row["skill"], row["points"]
            )

        for row in data.get("inventory", []):
            if row.get("guild_id") != gid: continue
            await con.execute(
                "INSERT INTO inventory (guild_id, user_id, item, qty) VALUES ($1,$2,$3,$4)",
                row["guild_id"], row["user_id"], row["item"], row["qty"]
            )

    await interaction.response.send_message("Restore complete for this server.", ephemeral=True)

# ---- Keep-alive HTTP server for Render
from keep_alive import start as keep_alive_start

# ---- Token & run
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

if __name__ == "__main__":
    keep_alive_start()  # bind $PORT so Render deploy succeeds
    bot.run(TOKEN)
