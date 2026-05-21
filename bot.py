import json
import asyncio
import os
import logging
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands
import aiohttp


VERSION="202605211500"
# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
CHANNEL_ID = int(os.environ["CHANNEL_ID"])

URL = "https://adventuring-guild-mumbai.notion.site/api/v3/queryCollection"

HEADERS = {
    "Content-Type": "application/json",
    "notion-client-version": "23.13.20260422.1906",
    "Origin": "https://adventuring-guild-mumbai.notion.site",
    "Referer": "https://adventuring-guild-mumbai.notion.site/2292380d35ca80418cf4c8a1588a19dc?v=2292380d35ca809490d0000c32aaf74d",
}

PAYLOAD = {
    "collectionView": {"id": "2292380d-35ca-8094-90d0-000c32aaf74d", "spaceId": ""},
    "collectionViewBlock": {"id": "2292380d-35ca-8041-8cf4-c8a1588a19dc", "spaceId": ""},
    "clientType": "notion_app",
    "userTimeZone": "Asia/Kolkata",
    "isFullScreen": True,
    "isMobile": False,
}

SEATS_PAYLOAD = {
    "collectionView": {"id": "2292380d-35ca-8097-a7f8-000c060689e1", "spaceId": ""},
    "collectionViewBlock": {"id": "2292380d-35ca-80b9-b299-f60953997601", "spaceId": ""},
    "clientType": "notion_app",
    "userTimeZone": "Asia/Kolkata",
    "isFullScreen": True,
    "isMobile": False,
}

monitor_task = None

# --- Notion Session ---

notion_session: aiohttp.ClientSession = None

async def get_session():
    global notion_session
    if notion_session is None or notion_session.closed:
        notion_session = aiohttp.ClientSession()
    return notion_session

# --- Async Notion Fetch ---

async def fetch_entries():
    session = await get_session()
    async with session.post(URL, json=PAYLOAD, headers=HEADERS) as resp:
        data = await resp.json()
        return data["recordMap"]["block"]

async def fetch_seats():
    session = await get_session()
    async with session.post(URL, json=SEATS_PAYLOAD, headers=HEADERS) as resp:
        data = await resp.json()
        return {
            block_id: block_data["value"]["value"]
            for block_id, block_data in data["recordMap"]["block"].items()
            if block_data["value"]["value"].get("parent_table") == "collection"
            and block_data["value"]["value"].get("type") == "page"
        }

# --- Property Parsers ---

def get_text(props, key):
    try:
        return props[key][0][0]
    except (KeyError, IndexError, TypeError):
        return ""

def get_date(props, key):
    try:
        return props[key][0][1][0][1]
    except (KeyError, IndexError, TypeError):
        return {}

def get_system(props):
    system = get_text(props, "[zrW")
    other_system = get_text(props, ";XHz")
    return other_system if system == "Other" and other_system else system

def format_date(date_str, time_str="00:00"):
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        dt_utc = dt.replace(tzinfo=timezone.utc)
        dt_ist = dt_utc + timedelta(hours=5, minutes=30)
        return dt_ist.strftime("%A, %B %-d, %Y")
    except (ValueError, AttributeError):
        return date_str

def format_time(time_str, date_str="1970-01-01"):
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        dt_utc = dt.replace(tzinfo=timezone.utc)
        dt_ist = dt_utc + timedelta(hours=5, minutes=30)
        return dt_ist.strftime("%-I:%M %p")
    except (ValueError, AttributeError):
        return time_str

# --- Message Formatters ---

def format_message(props):
    title = get_text(props, "title")
    dm = get_text(props, "SPnV")
    system_display = get_system(props)
    game_type = get_text(props, ">}g{")
    session_type = get_text(props, "|fRn")
    exp_level = get_text(props, "|}tH")
    level = get_text(props, "mul~")
    location = get_text(props, "Plyx") or "Online"
    campaign_link = get_text(props, "]HCL")
    description = get_text(props, "BjJV")
    description = "\n".join(f"_{line.strip()}_" if line.strip() else "" for line in description.split("\n"))
    art_credits = get_text(props, "J?h{")
    content_warnings = get_text(props, "zjcl")
    content_warnings = ", ".join(cw.strip() for cw in content_warnings.split(","))
    classes_allowed = get_text(props, "{WSY")
    species_allowed = get_text(props, "Ixus")
    other_notes = get_text(props, "uXf_")
    price_type = get_text(props, "IiTI")
    cost = "FREE" if price_type == "Free" else f"INR {get_text(props, '|jEv')}"

    start = get_date(props, "k|VL")
    end = get_date(props, "To;]")
    session_date = format_date(start.get("start_date", ""), start.get("start_time", "00:00"))
    session_time = f"{format_time(start.get('start_time', ''), start.get('start_date', ''))} to {format_time(end.get('start_time', ''), end.get('start_date', ''))}"

    message = f"""*{title}*
_{game_type} {session_type}_ for *{exp_level}*
*{session_date}*
*{session_time}*

{description}

*CW:* {content_warnings}

*DM:* {dm}
*System:* {system_display}
*Level:* {level}
*Classes Allowed:* {classes_allowed}
*Species Allowed:* {species_allowed}
{f"\n*Other Notes:*\n{other_notes}\n" if other_notes else ""}{f"\n*Campaign Link:* {campaign_link}\n" if campaign_link else ""}
*Session Type:* {game_type} {session_type}
*Venue:* {location}
*Cost:* {cost}
*Date:* {session_date}
*Time:* {session_time}

*Art Credits:* _{art_credits}_

*!! Registrations open at 9pm through the link below !!*
https://adventuringguildmumbai.fillout.com/player-sign-up"""

    return {
    "title": title,
    "dm": dm,
    "date": session_date,
    "time": session_time,
    "message": message
    }

def make_game_embed(game):
    message = game["message"]

    if len(message) > 4000:
        message = message[:3975] + "\n...[truncated]"

    embed = discord.Embed(
        title=game["title"],
        description=f"```{message}```",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="DM",
        value=game["dm"] or "Unknown",
        inline=True
    )

    embed.add_field(
        name="Date",
        value=game["date"] or "Unknown",
        inline=True
    )

    embed.add_field(
        name="Time",
        value=game["time"] or "Unknown",
        inline=True
    )

    return embed

def format_open_seats_message(props, open_seats):
    title = get_text(props, "title")
    game_type = get_text(props, ">}g{")
    session_type = get_text(props, "|fRn")
    exp_level = get_text(props, "|}tH")
    location = get_text(props, "Plyx") or "Online"
    system = get_system(props)

    start = get_date(props, "k|VL")
    end = get_date(props, "To;]")
    session_date = format_date(start.get("start_date", ""), start.get("start_time", "00:00"))
    session_time = f"{format_time(start.get('start_time', ''), start.get('start_date', ''))} to {format_time(end.get('start_time', ''), end.get('start_date', ''))}"

    seat_text = f"‼️ *{open_seats} seat available* ‼️" if open_seats == 1 else f"‼️ *{open_seats} seats available* ‼️"

    return f"""*{title}*
_{game_type} {session_type}_ for *{exp_level}*
{session_date}
{session_time}
{location}
*System: {system}*
{seat_text}"""

async def get_open_seats():
    raw_entries = await fetch_entries()
    game_entries = {
        block_id: block_data["value"]["value"]
        for block_id, block_data in raw_entries.items()
        if block_data["value"]["value"].get("parent_table") == "collection"
        and block_data["value"]["value"].get("type") == "page"
    }

    open_games = {
        block_id: val
        for block_id, val in game_entries.items()
        if get_text(val["properties"], "u<SL") == "Yes"
        and get_text(val["properties"], "?:QW") == "Yes"
    }

    seat_blocks = await fetch_seats()

    empty_seats_by_game = {}
    for seat_id, seat in seat_blocks.items():
        p = seat.get("properties", {})
        game_id = p.get("]b~|", [[None, [[None, None]]]])[0][1][0][1]
        if game_id and "^IxV" not in p:
            empty_seats_by_game[game_id] = empty_seats_by_game.get(game_id, 0) + 1

    results = []
    for game_id, val in open_games.items():
        open_seats = empty_seats_by_game.get(game_id, 0)
        if open_seats > 0:
            results.append((val, open_seats))
    
    results.sort(key=lambda x: get_date(x[0]["properties"], "k|VL").get("start_date", ""))
    return results

# --- State Management ---

def load_seen():
    try:
        with open("seen_entries.json", "r") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()

def save_seen(seen):
    with open("seen_entries.json", "w") as f:
        json.dump(list(seen), f)

# --- Bot ---

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

@bot.event
async def on_ready():
    global notion_session, monitor_task
    
    if notion_session is None or notion_session.closed:
    	notion_session = aiohttp.ClientSession()
    
    logger.info(f"AGM Bot v{VERSION} starting up...")
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    
    guild = discord.Object(id=GUILD_ID)
    synced = await bot.tree.sync(guild=guild)
    
    logger.info(f"Synced commands to guild: {[cmd.name for cmd in synced]}")
    if monitor_task is None or monitor_task.done():
    	monitor_task = bot.loop.create_task(monitor())
    	logger.info("Started monitor task.")
    else:
    	logger.info("Monitor task already running.")

@tree.command(name="ping", description="Reply with Pong!", guild=discord.Object(id=GUILD_ID))
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)

@tree.command(name="open-seats", description="Show all games with available seats", guild=discord.Object(id=GUILD_ID))
async def open_seats_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    results = await get_open_seats()
    if not results:
        await interaction.followup.send("No games with open seats at the moment.", ephemeral=True)
        return
    message = "\n\n".join(format_open_seats_message(val["properties"], open_seats) for val, open_seats in results)
    await interaction.followup.send(f"```{message}```", ephemeral=True)

async def monitor():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    seen = load_seen()

    if not seen:
        raw_entries = await fetch_entries()
        entries = {
            block_id: block_data["value"]["value"]
            for block_id, block_data in raw_entries.items()
            if block_data["value"]["value"].get("parent_table") == "collection"
            and block_data["value"]["value"].get("type") == "page"
        }
        seen = set(entries.keys())
        save_seen(seen)
        logger.info(f"First run — seeded {len(seen)} existing entries.")

    while True:
        try:
            raw_entries = await fetch_entries()
            entries = {
                block_id: block_data["value"]["value"]
                for block_id, block_data in raw_entries.items()
                if block_data["value"]["value"].get("parent_table") == "collection"
                and block_data["value"]["value"].get("type") == "page"
            }

            current_ids = set(entries.keys())
            new_ids = current_ids - seen

            for block_id in new_ids:
                block = entries[block_id]
                title = block["properties"]["title"][0][0]
                seen.add(block_id)
                save_seen(seen)
                logger.info(f"New entry: {title}")
                game = format_message(block["properties"])
                await channel.send(
                	embed=make_game_embed(game))

            save_seen(seen)

        except Exception as e:
            logger.error(f"Error in monitor loop: {e}")

        await asyncio.sleep(600)

class ListingView(discord.ui.View):
    def __init__(self, chunks):
        super().__init__()
        self.chunks = chunks
        self.current = 0

    def make_embed(self):
        embed = discord.Embed(
            title=f"Current Game Listings (Page {self.current + 1}/{len(self.chunks)})",
            description=self.chunks[self.current],
            color=discord.Color.blue()
        )
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current > 0:
            self.current -= 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current < len(self.chunks) - 1:
            self.current += 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

@tree.command(name="list-games", description="List all games currently on Notion", guild=discord.Object(id=GUILD_ID))
async def list_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    raw_entries = await fetch_entries()
    entries = {
        block_id: block_data["value"]["value"]
        for block_id, block_data in raw_entries.items()
        if block_data["value"]["value"].get("parent_table") == "collection"
        and block_data["value"]["value"].get("type") == "page"
    }
    sorted_entries = sorted(entries.items(), key=lambda x: x[1].get("created_time", 0), reverse=True)

    lines = [
        f"{i+1}. {get_text(val['properties'], 'title')} | {get_text(val['properties'], '>}g{')} {get_text(val['properties'], '|fRn')} | {get_system(val['properties'])} | DM: {get_text(val['properties'], 'SPnV')}"
        for i, (block_id, val) in enumerate(sorted_entries)
    ]

    chunks = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 4000:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)

    view = ListingView(chunks)
    await interaction.followup.send(embed=view.make_embed(), view=view, ephemeral=True)

@tree.command(name="get-game", description="Generate an announcement block for a specified game from the list", guild=discord.Object(id=GUILD_ID))
async def get_command(interaction: discord.Interaction, number: int):
    await interaction.response.defer(ephemeral=True)
    raw_entries = await fetch_entries()
    entries = {
        block_id: block_data["value"]["value"]
        for block_id, block_data in raw_entries.items()
        if block_data["value"]["value"].get("parent_table") == "collection"
        and block_data["value"]["value"].get("type") == "page"
    }
    sorted_entries = sorted(entries.items(), key=lambda x: x[1].get("created_time", 0), reverse=True)

    if number < 1 or number > len(sorted_entries):
        await interaction.followup.send(f"Please enter a number between 1 and {len(sorted_entries)}.", ephemeral=True)
        return

    block_id, val = sorted_entries[number - 1]
    game = format_message(val['properties'])
    
    await interaction.followup.send(
    	embed=make_game_embed(game),
    	ephemeral=True
    )

bot.run(DISCORD_TOKEN)
