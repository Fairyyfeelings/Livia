"""
Livia Bot — Marble Isles TTRPG Assistant (discord.py, aiosqlite)

Core features
- Character creation with Mind/Body/Soul (5/3/1) and Origins (Noble, Citizen, Country, Streetrat)
- Core attributes: Sanity = 2*Mind, Health = 2*Body, Spirit = 2*Soul (tracked & clamped)
- Skills with caps (0–3). Stats add to relevant skills for rolls
- d20 roller: total = d20 + skill + governing stat (⭐ star next to total)
- Inventory & wallet (Doubloons). GM can grant money and items
- Shop & buying (deducts from wallet)
- Per-guild, per-user save data in SQLite

Deploying on Render
- requirements.txt:
    discord.py
    aiosqlite
- Start command: `python bot.py`
- Set env var: DISCORD_TOKEN

"""

import os
import random
from typing import Dict, Optional, List, Tuple

import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite

BOT_NAME = "Livia Bot"
LOCATION = "Marble Isles"
CURRENCY = "Doubloons"
STAR = "⭐"

# --- Skills & Stat mapping ---
MIND_SKILLS = ["lore", "streetwise", "persuasion", "ranged_weapons"]
BODY_SKILLS = ["melee_weapons", "dance", "evasion", "brawling"]
SOUL_SKILLS = ["religion", "clairvoyance", "drug_tolerance", "exorcism"]

SKILL_TO_STAT: Dict[str, str] = {
    **{s: "Mind" for s in MIND_SKILLS},
    **{s: "Body" for s in BODY_SKILLS},
    **{s: "Soul" for s in SOUL_SKILLS},
}

# Shop — simple starter catalogue
SHOP: Dict[str, int] = {
    "formal_outfit": 120,
    "common_outfit": 40,
    "work_outfit": 60,
    "ragged_outfit": 10,
    "pistol": 200,
    "dagger": 80,
    "healing_salves": 30,
}

DB_PATH = "data/livia.db"

# ---------------- DB helpers ----------------
CREATE_SQL = [
    """
    CREATE TABLE IF NOT EXISTS characters (
        guild_id INTEGER,
        user_id INTEGER,
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
        guild_id INTEGER,
        user_id INTEGER,
        skill TEXT,
        points INTEGER,
        PRIMARY KEY (guild_id, user_id, skill)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory (
        guild_id INTEGER,
        user_id INTEGER,
        item TEXT,
        qty INTEGER,
        PRIMARY KEY (guild_id, user_id, item)
    );
    """,
]

async def init_db():
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        for sql in CREATE_SQL:
            await db.execute(sql)
        await db.commit()

# Normalization for skill keys
def slug(s: str) -> str:
    return s.strip().lower().replace(" ", "_")

async def fetch_char(db: aiosqlite.Connection, guild_id: int, user_id: int) -> Optional[aiosqlite.Row]:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT * FROM characters WHERE guild_id=? AND user_id=?", (guild_id, user_id)
    ) as cur:
        return await cur.fetchone()

async def ensure_skills_row(db: aiosqlite.Connection, guild_id: int, user_id: int, skill: str):
    s = slug(skill)
    async with db.execute(
        "SELECT points FROM skills WHERE guild_id=? AND user_id=? AND skill=?",
        (guild_id, user_id, s),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        await db.execute(
            "INSERT INTO skills (guild_id, user_id, skill, points) VALUES (?, ?, ?, 0)",
            (guild_id, user_id, s),
        )
        await db.commit()

async def get_skill_points(db: aiosqlite.Connection, guild_id: int, user_id: int, skill: str) -> int:
    s = slug(skill)
    await ensure_skills_row(db, guild_id, user_id, s)
    async with db.execute(
        "SELECT points FROM skills WHERE guild_id=? AND user_id=? AND skill=?",
        (guild_id, user_id, s),
    ) as cur:
        row = await cur.fetchone()
        return int(row[0]) if row else 0

async def add_skill_points(db: aiosqlite.Connection, guild_id: int, user_id: int, skill: str, amount: int) -> Tuple[int, int]:
    """Returns (new_points, spent_from_pool). Caps at 3 per skill, pulls from unassigned_points."""
    s = slug(skill)
    await ensure_skills_row(db, guild_id, user_id, s)
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT unassigned_points FROM characters WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ) as cur:
        left_row = await cur.fetchone()
    if left_row is None:
        raise ValueError("Character not found")
    pool = int(left_row[0])

    async with db.execute(
        "SELECT points FROM skills WHERE guild_id=? AND user_id=? AND skill=?",
        (guild_id, user_id, s),
    ) as cur:
        row = await cur.fetchone()
    current = int(row[0]) if row else 0

    can_add = max(0, min(3 - current, amount, pool))
    new_points = current + can_add
    new_pool = pool - can_add

    await db.execute(
        "UPDATE skills SET points=? WHERE guild_id=? AND user_id=? AND skill=?",
        (new_points, guild_id, user_id, s),
    )
    await db.execute(
        "UPDATE characters SET unassigned_points=? WHERE guild_id=? AND user_id=?",
        (new_pool, guild_id, user_id),
    )
    await db.commit()
    return new_points, can_add

async def add_item(db: aiosqlite.Connection, guild_id: int, user_id: int, item: str, qty: int = 1):
    i = slug(item)
    await db.execute(
        "INSERT INTO inventory (guild_id, user_id, item, qty) VALUES (?, ?, ?, ?)\n         ON CONFLICT(guild_id, user_id, item) DO UPDATE SET qty = qty + excluded.qty",
        (guild_id, user_id, i, qty),
    )
    await db.commit()

async def list_inventory(db: aiosqlite.Connection, guild_id: int, user_id: int) -> List[aiosqlite.Row]:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT item, qty FROM inventory WHERE guild_id=? AND user_id=? ORDER BY item",
        (guild_id, user_id),
    ) as cur:
        return await cur.fetchall()

# ---------------- Bot ----------------
class Livia(commands.Bot):
    async def setup_hook(self):
        await init_db()
        await self.tree.sync()

bot = Livia(command_prefix="!", intents=discord.Intents.default())

@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=f"{LOCATION} • /sheet"))
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ---------------- Utilities ----------------

def is_gm(interaction: discord.Interaction) -> bool:
    m = interaction.user
    if isinstance(m, discord.Member):
        return m.guild_permissions.administrator or m.guild_permissions.manage_guild
    return False

async def ensure_character(inter: discord.Interaction) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await fetch_char(db, inter.guild_id, inter.user.id)  # type: ignore
        return row

# ---------------- Commands ----------------

@bot.tree.command(description="Create your character (choose 5/3/1 stats and an Origin)")
@app_commands.describe(
    name="Character name",
    primary="Which stat is 5?",
    secondary="Which stat is 3? (the remaining becomes 1)",
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

    async with aiosqlite.connect(DB_PATH) as db:
        existing = await fetch_char(db, interaction.guild_id, interaction.user.id)  # type: ignore
        if existing:
            return await interaction.response.send_message("You already have a character. Use /sheet or ask a GM to reset.", ephemeral=True)

        stats = {"Mind": 1, "Body": 1, "Soul": 1}
        stats[primary.value] = 5
        stats[secondary.value] = 3
        # compute cores
        max_sanity = stats["Mind"] * 2
        max_health = stats["Body"] * 2
        max_spirit = stats["Soul"] * 2

        # starting wallet & items & skill bonuses by origin
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

        await db.execute(
            """
            INSERT INTO characters (guild_id, user_id, name, mind, body, soul, sanity, health, spirit, max_sanity, max_health, max_spirit, wallet)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild_id,
                interaction.user.id,
                name,
                stats["Mind"],
                stats["Body"],
                stats["Soul"],
                max_sanity,
                max_health,
                max_spirit,
                max_sanity,
                max_health,
                max_spirit,
                wallet,
            ),
        )

        # ensure all skills exist at 0 then add origin bonuses with cap 3
        for s in SKILL_TO_STAT.keys():
            await ensure_skills_row(db, interaction.guild_id, interaction.user.id, s)  # type: ignore
        for (sk, amt) in origin_bonuses:
            # add without consuming unassigned pool
            async with db.execute(
                "SELECT points FROM skills WHERE guild_id=? AND user_id=? AND skill=?",
                (interaction.guild_id, interaction.user.id, sk),
            ) as cur:
                row = await cur.fetchone()
            current = int(row[0]) if row else 0
            newv = min(3, current + amt)
            await db.execute(
                "UPDATE skills SET points=? WHERE guild_id=? AND user_id=? AND skill=?",
                (newv, interaction.guild_id, interaction.user.id, sk),
            )
        # starting items
        for (it, q) in start_items:
            await add_item(db, interaction.guild_id, interaction.user.id, it, q)

        await db.commit()

    await interaction.response.send_message(
        f"**{name}** is registered in the {LOCATION}! You have **10 skill points** to distribute with `/skill add`.",
        ephemeral=True,
    )

@bot.tree.command(description="View your character sheet")
async def sheet(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        ch = await fetch_char(db, interaction.guild_id, interaction.user.id)  # type: ignore
        if not ch:
            return await interaction.response.send_message("No character yet. Use /create first.", ephemeral=True)

        # gather skills
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT skill, points FROM skills WHERE guild_id=? AND user_id=? ORDER BY skill",
            (interaction.guild_id, interaction.user.id),
        ) as cur:
            rows = await cur.fetchall()
        skills_text = ", ".join([f"{r['skill']} {r['points']}" for r in rows if r['points'] > 0]) or "(none)"

        inv = await list_inventory(db, interaction.guild_id, interaction.user.id)  # type: ignore
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
    embed.set_footer(text=f"{LOCATION}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---- Skill management ----
@bot.tree.command(description="Add points to a skill (caps at 3, uses your pool)")
@app_commands.describe(skill="Skill name (e.g., exorcism)", amount="How many points to add")
async def skill_add(interaction: discord.Interaction, skill: str, amount: int):
    s = slug(skill)
    if s not in SKILL_TO_STAT:
        valid = ", ".join(sorted(SKILL_TO_STAT.keys()))
        return await interaction.response.send_message(f"Unknown skill. Try: {valid}", ephemeral=True)

    async with aiosqlite.connect(DB_PATH) as db:
        ch = await fetch_char(db, interaction.guild_id, interaction.user.id)  # type: ignore
        if not ch:
            return await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
        newv, spent = await add_skill_points(db, interaction.guild_id, interaction.user.id, s, amount)  # type: ignore
        await db.commit()
    await interaction.response.send_message(
        f"Added {spent} to **{s}** → now {newv}. ({STAR} points remaining: {ch['unassigned_points'] - spent if ch else '?'}).",
        ephemeral=True,
    )

# ---- Rolling ----
@bot.tree.command(description="Roll d20 + skill + governing stat")
@app_commands.describe(skill="Skill to roll (e.g., exorcism)")
async def roll(interaction: discord.Interaction, skill: str):
    s = slug(skill)
    if s not in SKILL_TO_STAT:
        valid = ", ".join(sorted(SKILL_TO_STAT.keys()))
        return await interaction.response.send_message(f"Unknown skill. Try: {valid}", ephemeral=True)

    async with aiosqlite.connect(DB_PATH) as db:
        ch = await fetch_char(db, interaction.guild_id, interaction.user.id)  # type: ignore
        if not ch:
            return await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
        pts = await get_skill_points(db, interaction.guild_id, interaction.user.id, s)  # type: ignore

    stat_name = SKILL_TO_STAT[s]
    stat_value = int(ch[stat_name.lower()])  # mind/body/soul fields
    d20 = random.randint(1, 20)
    total = d20 + pts + stat_value

    nat = " (CRIT!)" if d20 == 20 else (" (botch)" if d20 == 1 else "")

    embed = discord.Embed(title=f"{interaction.user.display_name} rolls {s}", color=discord.Color.dark_teal())
    embed.add_field(name="d20", value=str(d20) + nat)
    embed.add_field(name="Skill", value=str(pts))
    embed.add_field(name=stat_name, value=str(stat_value))
    embed.add_field(name=f"Total {STAR}", value=f"**{total}**", inline=False)
    embed.set_footer(text=f"{LOCATION}")
    await interaction.response.send_message(embed=embed)

# ---- Core attribute damage/heal ----
@bot.tree.command(description="Apply damage to Sanity/Health/Spirit")
@app_commands.describe(kind="Which attribute", amount="How much damage")
@app_commands.choices(kind=[
    app_commands.Choice(name="Sanity", value="sanity"),
    app_commands.Choice(name="Health", value="health"),
    app_commands.Choice(name="Spirit", value="spirit"),
])
async def damage(interaction: discord.Interaction, kind: app_commands.Choice[str], amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        ch = await fetch_char(db, interaction.guild_id, interaction.user.id)  # type: ignore
        if not ch:
            return await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
        field = kind.value
        newv = max(0, int(ch[field]) - amount)
        await db.execute(
            f"UPDATE characters SET {field}=? WHERE guild_id=? AND user_id=?",
            (newv, interaction.guild_id, interaction.user.id),
        )
        await db.commit()

    end_text = ""
    if field == "health" and newv == 0:
        end_text = " You **die**."  # per rules
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
    async with aiosqlite.connect(DB_PATH) as db:
        ch = await fetch_char(db, interaction.guild_id, interaction.user.id)  # type: ignore
        if not ch:
            return await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
        field = kind.value
        maxv = int(ch[f"max_{field}"])
        newv = min(maxv, int(ch[field]) + amount)
        await db.execute(
            f"UPDATE characters SET {field}=? WHERE guild_id=? AND user_id=?",
            (newv, interaction.guild_id, interaction.user.id),
        )
        await db.commit()
    await interaction.response.send_message(f"{kind.name} now **{newv}**/{maxv}.")

# ---- Wallet & Shop ----
@bot.tree.command(description="Check your wallet balance")
async def wallet(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        ch = await fetch_char(db, interaction.guild_id, interaction.user.id)  # type: ignore
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
    cost = SHOP[it] * max(1, qty)
    async with aiosqlite.connect(DB_PATH) as db:
        ch = await fetch_char(db, interaction.guild_id, interaction.user.id)  # type: ignore
        if not ch:
            return await interaction.response.send_message("Create a character first with /create.", ephemeral=True)
        if ch["wallet"] < cost:
            return await interaction.response.send_message("You can't afford that.", ephemeral=True)
        await db.execute(
            "UPDATE characters SET wallet = wallet - ? WHERE guild_id=? AND user_id=?",
            (cost, interaction.guild_id, interaction.user.id),
        )
        await add_item(db, interaction.guild_id, interaction.user.id, it, qty)  # type: ignore
        await db.commit()
    await interaction.response.send_message(f"Purchased **{qty}× {it}** for **{cost} {CURRENCY}**.")

@bot.tree.command(description="Show your inventory")
async def inventory(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        inv = await list_inventory(db, interaction.guild_id, interaction.user.id)  # type: ignore
    text = "\n".join([f"• {r['item']}×{r['qty']}" for r in inv]) or "(empty)"
    await interaction.response.send_message("**Inventory**\n" + text, ephemeral=True)

# ---- GM utilities ----

@bot.tree.command(description="[GM] Give money to a character")
@app_commands.describe(member="Who gets the money", amount="How many Doubloons")
async def gm_give(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not is_gm(interaction):
        return await interaction.response.send_message("GM only.", ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        ch = await fetch_char(db, interaction.guild_id, member.id)  # type: ignore
        if not ch:
            return await interaction.response.send_message("That user has no character.", ephemeral=True)
        await db.execute(
            "UPDATE characters SET wallet = wallet + ? WHERE guild_id=? AND user_id=?",
            (amount, interaction.guild_id, member.id),
        )
        await db.commit()
    await interaction.response.send_message(f"Granted **{amount} {CURRENCY}** to {member.display_name}.")

@bot.tree.command(description="[GM] Add item to a character's inventory")
@app_commands.describe(member="Target", item="Item name", qty="Quantity")
async def gm_additem(interaction: discord.Interaction, member: discord.Member, item: str, qty: int = 1):
    if not is_gm(interaction):
        return await interaction.response.send_message("GM only.", ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        ch = await fetch_char(db, interaction.guild_id, member.id)  # type: ignore
        if not ch:
            return await interaction.response.send_message("That user has no character.", ephemeral=True)
        await add_item(db, interaction.guild_id, member.id, item, qty)  # type: ignore
    await interaction.response.send_message(f"Gave **{qty}× {slug(item)}** to {member.display_name}.")

# ---- Token & run ----
TOKEN = os.getenv("DISCORD_TOKEN") or "PUT_TOKEN_HERE"

if __name__ == "__main__":
    if TOKEN == "PUT_TOKEN_HERE":
        print("[WARN] Set DISCORD_TOKEN env var")
    bot.run(TOKEN)
