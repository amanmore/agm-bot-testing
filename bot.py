import os
import asyncio
import logging
import json
from datetime import datetime, timezone, timedelta
from notion_client import AsyncClient

import discord
from discord.ext import commands

# --- Version ---
VERSION = "2026052501"

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# Silence noisy libraries

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

# --- Environment Variables ---
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
TABLES_DATA_SOURCE_ID = os.environ["TABLES_DATA_SOURCE_ID"]
SEATS_DATA_SOURCE_ID = os.environ["SEATS_DATA_SOURCE_ID"]

# --- Constants ---
IST = timezone(timedelta(hours=5, minutes=30))
SEEN_FILE = "data/seen_entries.json"
DATA_DIR = "data"

# --- Notion Client ---
notion = AsyncClient(auth=NOTION_TOKEN)

# --- Bot ---
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
monitor_task = None

# --- Seen Game Handling ---

def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()

def save_seen(seen):
    os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

# --- Discord Paginaton Helper ---

def chunk_text(text, limit=1900):

    chunks = []
    current = ""

    for line in text.split("\n"):

        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = (
                current + "\n" + line
                if current else line
            )

    if current:
        chunks.append(current)

    return chunks

# --- Notion Pagination Helper ---

async def fetch_all_rows(data_source_id):

    results = []
    cursor = None

    while True:

        response = await notion.data_sources.query(
            data_source_id=data_source_id,
            start_cursor=cursor
        )
        results.extend(response["results"])
        if not response["has_more"]:
            break
        cursor = response["next_cursor"]
    return results

# --- Fetch Data Sources ---

async def fetch_games():
    return await fetch_all_rows(TABLES_DATA_SOURCE_ID)

async def fetch_seats():
    return await fetch_all_rows(SEATS_DATA_SOURCE_ID)

# --- Helper functions ---

def clean_text(text):
    if not text:
        return ""

    return text.strip()

def italicize_lines(text):
    lines = text.strip().splitlines()

    if not text:
        return ""

    formatted = []
    for line in lines:
        stripped = line.strip()
        if stripped:
            formatted.append(f"_{stripped}_")
        else:
            formatted.append("")

    return "\n".join(formatted)

def get_title(prop):
    try:
        return prop["title"][0]["plain_text"].strip()
    except (KeyError, IndexError, TypeError):
        return ""

def get_rich_text(prop):
    try:
        return "".join(
            text["plain_text"]
            for text in prop["rich_text"]
        ).strip()
    except (KeyError, TypeError):
        return ""

def get_select(prop):
    try:
        return prop["select"]["name"].strip()
    except (KeyError, TypeError):
        return ""

def get_multi_select(prop):
    try:
        return ", ".join(
            item["name"]
            for item in prop["multi_select"]
        ).strip()
    except (KeyError, TypeError):
        return ""

def get_number(prop):
    try:
        return prop["number"]
    except (KeyError, TypeError):
        return None

def get_checkbox(prop):
    try:
        return prop["checkbox"]
    except (KeyError, TypeError):
        return False

def get_url(prop):
    try:
        return prop["url"]
    except (KeyError, TypeError):
        return ""

def get_date(prop):
    try:
        return prop["date"]
    except (KeyError, TypeError):
        return None

def format_date(date_string):
    if not date_string:
        return "Unknown Date"
    dt = datetime.fromisoformat(
        date_string.replace("Z", "+00:00")
    )
    dt = dt.astimezone(IST)
    return dt.strftime("%A, %B %d, %Y")

def format_time(date_string):
    if not date_string:
        return "Unknown Time"
    dt = datetime.fromisoformat(
        date_string.replace("Z", "+00:00")
    )
    dt = dt.astimezone(IST)
    return dt.strftime("%I:%M %p").lstrip("0")

# --- Fetch Data Function ---

async def fetch_data():
    games = await fetch_games()
    seats = await fetch_seats()
    seats_by_id = {
        seat["id"]: seat
        for seat in seats
    }
    return games, seats_by_id


# --- Seat Parsing Function ---

def get_open_seats(props, seatsdata):

    show = get_checkbox(
        props["Show"]
    )

    activate = get_checkbox(
        props["Activate"]
    )

    player_capacity = get_number(
        props["Player Capacity"]
    ) or 0

    # For inactive/hidden games,

    if show and not activate: # Closed Game - 0
        return 0

    if not show and not activate: # Hidden Game - Capacity
        return player_capacity

    if not show and activate: # Error (Hidden Game Activated)
        return 0

    # Otherwise count actual empty seats
    if show and activate:
        try:
            seat_relations = (props["Seats"]["relation"])
        except KeyError:
            return 0

        open_seats = 0

        for relation in seat_relations:
            seat_id = relation["id"]
            seat = seatsdata.get(seat_id)

            if not seat:
                continue

            occupant = (seat["properties"]["Player"]["relation"])

            if len(occupant) == 0:
                open_seats += 1

        return open_seats
    return 0

# --- Property Parsing Function ---

def parse_props(props, seatdata):

    system = get_select(props["System"])
    other_system = get_rich_text(props["Other System"])

    if system == "Other" and other_system:
        system = other_system

    price_type = get_select(props["Price Type"])
    cost_number = get_number(props["Cost"])

    start = get_date(props["Start Date"])
    end = get_date(props["End Date"])

    return {
        "title": get_title(props["Title"]),
        "dm": get_rich_text(props["DM Name"]),
        "system": system,
        "game_type": get_select(props["Game Type"]),
        "session_type": get_select(props["Session Type"]),
        "exp_level": get_select(props["Experience Level"]),
        "level": get_number(props["Level"]),
        "location": (
            get_rich_text(props["Location"])
            or "Online"
        ),
        "campaign_link": get_url(props["Campaign Link"]),
        "description": italicize_lines(
            get_rich_text(props["Description"])
        ),
        "art_credits": get_rich_text(props["Art Credits"]),
        "content_warnings": get_multi_select(
            props["Content Warnings"]
        ),
        "classes_allowed": get_rich_text(
            props["Classes Allowed"]
        ),
        "species_allowed": get_rich_text(
            props["Species Allowed"]
        ),
        "other_notes": get_rich_text(
            props["Other Notes"]
        ),
        "cost": (
            "FREE"
            if price_type == "Free"
            else f"INR {cost_number}"
        ),
        "session_date": format_date(
            start["start"]
        ) if start else "Unknown Date",
        "session_time": (
            f"{format_time(start['start'])} "
            f"to "
            f"{format_time(end['start'])}"
        ) if start and end else "Unknown Time",
        "open_seats": get_open_seats(props,seatdata),
        "show": get_checkbox(props["Show"]),
        "activate": get_checkbox(props["Activate"]),
    }

# --- Announcement Message Formatting ---

def format_message(p):
    optional_sections = []

    if p["other_notes"]:
        optional_sections.append(
            f"\n*Other Notes:*\n{p['other_notes']}\n"
        )

    if p["campaign_link"]:
        optional_sections.append(
            f"\n*Campaign Link:* {p['campaign_link']}\n"
        )

    optional_text = "\n".join(optional_sections)

    return f"""*{p['title']}*
_{p['game_type']} {p['session_type']}_ for *{p['exp_level']}*
*{p['session_date']}*
*{p['session_time']}*

{p['description']}

*CW:* {p['content_warnings']}

*DM:* {p['dm']}
*System:* {p['system']}
*Level:* {p['level']}
*Classes Allowed:* {p['classes_allowed']}
*Species Allowed:* {p['species_allowed']}
{optional_text}
*Session Type:* {p['game_type']} {p['session_type']}
*Venue:* {p['location']}
*Cost:* {p['cost']}
*Date:* {p['session_date']}
*Time:* {p['session_time']}

*Art Credits:* _{p['art_credits']}_

*!! Registrations open at 9pm through the link below !!*
https://adventuringguildmumbai.fillout.com/player-sign-up"""

# --- Open Seats Message Formatting ---
def format_open_seats_message(p):

    if p["open_seats"] == 1:
        seat_text = (
            "‼️ *1 seat available* ‼️"
        )
    else:
        seat_text = (
            f"‼️ *{p['open_seats']} seats available* ‼️"
        )

    return f"""*{p['title']}*
_{p['game_type']} {p['session_type']}_ for *{p['exp_level']}*

*{p['session_date']}*
*{p['session_time']}*

*DM:* {p['dm']}
*System:* {p['system']}
*Venue:* {p['location']}

{seat_text}"""

# --- Games List Generation ---
async def games_list():
    games, seats_by_id = await fetch_data()
    game_list = []
    i=1
    for game in games:
        props = game["properties"]
        parsed_props = parse_props(props, seats_by_id)
        game_list.append(f"{i}. {parsed_props['title']} | {parsed_props['dm']} | {parsed_props['system']} | {parsed_props['location']} | {parsed_props['session_date']} | {parsed_props['session_time']} | {parsed_props['open_seats']} seats")
        i+=1
    return "\n".join(game_list)

# --- Fetch Specific Game ---

async def fetch_game(game_id:int=1):
    games, seats_by_id = await fetch_data()
    if game_id < 1 or game_id > len(games):
        return "Invalid Game ID"
    game = games[game_id-1]
    props = game["properties"]
    parsed_props = parse_props(props, seats_by_id)
    message = format_message(parsed_props)
    return message

# --- Fetch Open Seats ---

async def fetch_open_seats():
    games, seats_by_id = await fetch_data()
    open_seats = []
    for game in games:
        props = game["properties"]
        parsed_props = parse_props(props, seats_by_id)
        if parsed_props["activate"] and parsed_props["show"]:
            message = format_open_seats_message(parsed_props)
            open_seats.append(message)

    return "\n\n======\n\n".join(open_seats)

async def monitor():
    channel = bot.get_channel(CHANNEL_ID)
    seen = load_seen()

    if not seen:
        games, seats_by_id = await fetch_data()
        seen = {game["id"] for game in games}
        save_seen(seen)
        logger.info(f"First run — seeded {len(seen)} existing entries.")
        return

    while True:
        try:
            games, seats_by_id = await fetch_data()
            current_ids = {game["id"] for game in games}
            new_ids = current_ids - seen

            for game in games:
                if game["id"] in new_ids:
                    props = game["properties"]
                    parsed = parse_props(props, seats_by_id)
                    logger.info(f"New entry: {parsed['title']}")
                    message = format_message(parsed)
                    await channel.send(
                        embed=make_game_embed(parsed, message),
                        view=CopyView(message)
                    )
                    seen.add(game["id"])
                    save_seen(seen)

        except Exception as e:
            logger.info(f"Error in monitor loop: {e}")

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

def make_game_embed(parsed, message):
    display_message = message
    if len(display_message) > 4000:
        display_message = display_message[:3975] + "\n...[truncated]"

    embed = discord.Embed(
        title=parsed["title"],
        description=f"```{display_message}```",
        color=discord.Color.blue()
    )
    embed.add_field(name="DM", value=parsed["dm"] or "Unknown", inline=True)
    embed.add_field(name="Date", value=parsed["session_date"] or "Unknown", inline=True)
    embed.add_field(name="Time", value=parsed["session_time"] or "Unknown", inline=True)
    return embed


class CopyView(discord.ui.View):
    def __init__(self, message: str):
        super().__init__()
        self.message = message

    @discord.ui.button(label="📋 Copy Text", style=discord.ButtonStyle.secondary)
    async def copy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"```{self.message}```", ephemeral=True)

@bot.event
async def on_ready():
    global monitor_task
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

@tree.command(name="list-games", description="List all games currently on Notion", guild=discord.Object(id=GUILD_ID))
async def list_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await games_list()
    chunks = chunk_text(result, limit=4000)
    view = ListingView(chunks)
    await interaction.followup.send(embed=view.make_embed(), view=view, ephemeral=True)

@tree.command(name="get-game", description="Get announcement block for a specific game", guild=discord.Object(id=GUILD_ID))
async def get_command(interaction: discord.Interaction, number: int):
    await interaction.response.defer(ephemeral=True)
    games, seats_by_id = await fetch_data()
    if number < 1 or number > len(games):
        await interaction.followup.send(f"Please enter a number between 1 and {len(games)}.", ephemeral=True)
        return
    game = games[number - 1]
    parsed = parse_props(game["properties"], seats_by_id)
    message = format_message(parsed)
    await interaction.followup.send(
        embed=make_game_embed(parsed, message),
        view=CopyView(message),
        ephemeral=True
    )

@tree.command(name="open-seats", description="Show all games with available seats", guild=discord.Object(id=GUILD_ID))
async def open_seats_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await fetch_open_seats()
    if not result:
        await interaction.followup.send("No games with open seats at the moment.", ephemeral=True)
        return
    chunks = chunk_text(result)

    for i, chunk in enumerate(chunks):
        await interaction.followup.send(
            f"```{chunk}```",
            ephemeral=True
        )

bot.run(DISCORD_TOKEN)
