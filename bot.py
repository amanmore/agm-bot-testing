import os
import asyncio
import logging
import json
import yaml
import shutil
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from poster_generator import generate_poster

from discord import app_commands
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

# --- Silence noisy libraries ---

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
ROLE_ADMIN = int(os.environ["ROLE_ADMIN"])
ROLE_TEMPLATE_EDITOR = int(os.environ["ROLE_TEMPLATE_EDITOR"])
TEMPLATES = {}
FIELD_MAP = {}
FILES = {}
HELP_TEXT = ""

# --- Constants ---
IST = timezone(timedelta(hours=5, minutes=30))
SEEN_FILE = "data/seen_entries.json"
ANNOUNCED_FILE = "data/announced_games.json"
SCHEDULE_FILE = "data/scheduled_activations.json"

# --- Notion Client ---
notion = AsyncClient(auth=NOTION_TOKEN)

# --- Bot ---
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
monitor_task = None
scheduler_task = None



# --- File Discovery ---
def discover_files():
    files = {}
    for filename in os.listdir("templates"):
        if filename.endswith(".txt") and not filename.endswith(".bak"):
            key = filename.removesuffix(".txt")
            files[key] = (f"templates/{filename}")
    for filename in os.listdir("config"):
        if filename.endswith(".yaml") and not filename.endswith(".bak"):
            key = filename.removesuffix(".yaml")
            files[key] = (f"config/{filename}")
    return files


def refresh_files():
    global FILES
    FILES = discover_files()


def get_file_path(file_key: str):
    return FILES.get(file_key)


# --- File Autocomplete
async def file_autocomplete(
        interaction: discord.Interaction,
        current: str
):
    refresh_files()
    valid_files = []
    for key in FILES:
        if has_file_permission(interaction, key):
            if current.lower() in key.lower():
                valid_files.append(
                    app_commands.Choice(
                        name=key,
                        value=key
                    )
                )

    return valid_files[:25]


# --- Role Filtering ---
def has_file_permission(
        interaction: discord.Interaction,
        file_key: str
):
    path = FILES.get(file_key)
    if not path:
        return False
    user_roles = {
        role.id
        for role in interaction.user.roles
    }

    # Config files → Admin only
    if path.startswith("config/"):
        return ROLE_ADMIN in user_roles

    # Template files → Admin OR Template Editor
    if path.startswith("templates/"):
        return bool({
                        ROLE_ADMIN,
                        ROLE_TEMPLATE_EDITOR
                    } & user_roles)

    return False


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

# --- Announcement Handling ---

# --- Announced Games Persistence ---

def load_announced():
    try:
        with open(ANNOUNCED_FILE, "r") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        ValueError
    ):
        return set()


def save_announced(announced):
    os.makedirs(os.path.dirname(ANNOUNCED_FILE), exist_ok=True)

    with open(ANNOUNCED_FILE, "w") as f:
        json.dump(list(announced), f)


# --- Activation Schedule Persistence ---

def load_schedule():
    try:
        with open(SCHEDULE_FILE, "r") as f:
            data = json.load(f)

            return (
                data if isinstance(data, list)
                else []
            )

    except (
        FileNotFoundError,
        json.JSONDecodeError,
        ValueError
    ):
        return []


def save_schedule(schedule):
    os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)

    with open(SCHEDULE_FILE, "w") as f:
        json.dump(schedule, f, indent=2)


# --- Template Loading ---

def load_templates():
    global TEMPLATES
    refresh_files()
    templates = {}
    for key, path in FILES.items():
        if path.startswith("templates/"):
            with open(path, "r", encoding="utf-8") as f:
                templates[key] = f.read()
    TEMPLATES = templates
    logger.info(
        f"Templates reloaded: {list(TEMPLATES.keys())}"
    )


# --- Field Mapping ---

def load_field_map():
    global FIELD_MAP
    try:
        with open(
                "config/field_map.yaml",
                "r",
                encoding="utf-8"
        ) as f:
            FIELD_MAP = yaml.safe_load(f)
        logger.info("Field map reloaded.")
    except Exception as e:
        logger.error(
            f"Failed to reload field map: {e}"
        )
        
# --- Help Loader ---

def load_help():
    global HELP_TEXT

    path = "help/help.txt"

    try:
        with open(path, "r", encoding="utf-8") as f:
            HELP_TEXT = f.read()

        logger.info("Help text reloaded.")

    except Exception as e:
        logger.error(
            f"Failed to load help text: {e}"
        )


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

def escape_braces(text):
    if not isinstance(text, str):
        return text
    return (
        text
        .replace("{", "{{")
        .replace("}", "}}")
    )


def clean_text(text):
    if not text:
        return ""

    return text.strip()


def italicize_lines(text):
    if not text:
        return ""

    lines = text.strip().splitlines()

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
        text = prop["title"][0]["plain_text"].strip()
        return escape_braces(text)
    except (KeyError, IndexError, TypeError):
        return ""


def get_rich_text(prop):
    try:
        text = "".join(
            text["plain_text"]
            for text in prop["rich_text"]
        ).strip()
        return escape_braces(text)
    except (KeyError, TypeError):
        return ""


def get_select(prop):
    try:
        text = prop["select"]["name"].strip()
        return escape_braces(text)
    except (KeyError, TypeError):
        return ""


def get_multi_select(prop):
    try:
        text = ", ".join(
            item["name"]
            for item in prop["multi_select"]
        ).strip()
        return escape_braces(text)
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

def get_files(prop):
    try:
        files = prop.get("files", [])

        if not files:
            return ""

        file = files[0]

        if file["type"] == "external":
            return file["external"]["url"]

        if file["type"] == "file":
            return file["file"]["url"]

    except (
        KeyError,
        TypeError,
        IndexError,
        AttributeError
    ):
        pass

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

# --- Activation Scheduling Logic ---

def get_next_activation_time():

    now = datetime.now(IST)

    activation_time = now.replace(
        hour=21,
        minute=0,
        second=0,
        microsecond=0
    )

    # Before 8 PM IST → same day 9 PM
    if now.hour < 20:
        return activation_time

    # After 8 PM IST → next day 9 PM
    return activation_time + timedelta(days=1)

# --- Fetch Data Function ---

async def fetch_data():
    games = await fetch_games()
    seats = await fetch_seats()
    seats_by_id = {
        seat["id"]: seat
        for seat in seats
    }
    return games, seats_by_id


# --- Parsers ---

PARSERS = {
    "title": get_title,
    "rich_text": get_rich_text,
    "italic_rich_text": lambda p: italicize_lines(
        get_rich_text(p)
    ),
    "select": get_select,
    "multi_select": get_multi_select,
    "number": get_number,
    "checkbox": get_checkbox,
    "url": get_url,
    "date": get_date,
    "files": get_files,
}


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

    if show and not activate:  # Closed Game - 0
        return 0

    if not show and not activate:  # Hidden Game - Capacity
        return player_capacity

    if not show and activate:  # Error (Hidden Game Activated)
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
    parsed = {}

    for key, config in FIELD_MAP.items():

        notion_name = config["notion"]
        parser_name = config["parser"]

        parser = PARSERS[parser_name]

        value = parser(
            props.get(notion_name, {})
        )

        if (
                not value and
                "default" in config
        ):
            value = config["default"]

        parsed[key] = value

    # --- Derived fields ---

    if (
            parsed["system"] == "Other"
            and get_rich_text(props["Other System"])
    ):
        parsed["system"] = get_rich_text(
            props["Other System"]
        )

    if parsed["price_type"] == "Free":
        parsed["cost"] = "FREE"
    elif parsed["price_type"] == "Paid (Transport Fee only)":
        parsed["cost"] = "Transport Costs Shared"
    else:
        parsed["cost"] = (f"INR {parsed['cost_number']}")

    start = parsed["start_date"]
    end = parsed["end_date"]

    parsed["session_date"] = (
        format_date(start["start"])
        if start else "Unknown Date"
    )

    parsed["session_time"] = (
        f"{format_time(start['start'])} "
        f"to "
        f"{format_time(end['start'])}"
        if start and end
        else "Unknown Time"
    )

    parsed["open_seats"] = get_open_seats(
        props,
        seatdata
    )

    return parsed


# --- Announcement Message Formatting ---

def format_message(p):

    optional_sections = []

    if p["other_notes"] or p["tsl"] or p["experience"] or p["expectations"]:
        optional_sections.append(
            f"\n*Other Notes:*"
        )
        if len(p["experience"]) > 3:
        	optional_sections.append(
        		f"{p['experience']}\n"
        	)
        if len(p["expectations"]) > 3:
        	optional_sections.append(
        		f"{p['expectations']}\n"
        	)
        if len(p["tsl"]) > 3:
        	optional_sections.append(
        		f"{p['tsl']}\n"
        	)
        if len(p["other_notes"]) > 3:
        	optional_sections.append(
        		f"{p['other_notes']}\n"
        	)

    if p["campaign_link"]:
        optional_sections.append(
            f"\n*Campaign Link:* {p['campaign_link']}\n"
        )

    optional_text = "\n".join(optional_sections)

    template = TEMPLATES["announcement"]

    try:

        return template.format(
            **p,
            optional_text=optional_text
        )

    except KeyError as e:

        logger.error(
            f"Announcement template missing key: {e}"
        )

        return (
            "Template formatting error: "
            f"missing field {e}"
        )


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

    template = TEMPLATES["open_seats"]

    try:

        return template.format(
            **p,
            seat_text=seat_text
        )

    except KeyError as e:

        logger.error(
            f"Open seats template missing key: {e}"
        )

        return (
            "Template formatting error: "
            f"missing field {e}"
        )


def format_activation_announcement(games):
    line_template = TEMPLATES["activation_game_line"]

    lines = []

    for game in games:
        lines.append(
            line_template.format(**game)
        )

    games_list = "\n\n".join(lines)

    template = TEMPLATES["activation_announcement"]

    return template.format(
        games_list=games_list
    )

# --- Games List Generation ---
async def games_list():
    games, seats_by_id = await fetch_data()
    game_list = []
    i = 1
    for game in games:
        props = game["properties"]
        parsed_props = parse_props(props, seats_by_id)
        game_list.append(
            f"{i}. {parsed_props['title']} | {parsed_props['dm']} | {parsed_props['system']} | {parsed_props['location']} | {parsed_props['session_date']} | {parsed_props['session_time']} | {parsed_props['open_seats']} seats")
        i += 1
    return "\n".join(game_list)


# --- Fetch Specific Game ---

async def fetch_game(game_id: int = 1):
    games, seats_by_id = await fetch_data()
    if game_id < 1 or game_id > len(games):
        return "Invalid Game ID"
    game = games[game_id - 1]
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
                        view=CopyView(game["id"], parsed, message, parsed["cover_url"])
                    )
                    seen.add(game["id"])
                    save_seen(seen)

        except Exception:
            logger.exception("Error in monitor loop")

        await asyncio.sleep(600)

async def activation_scheduler():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    while True:
        try:
            now = datetime.now(IST)
            schedule = load_schedule()
            due = []
            remaining = []

            for entry in schedule:
                activate_at = datetime.fromisoformat(
                    entry["activate_at"]
                )
                if now >= activate_at:
                    due.append(entry)
                else:
                    remaining.append(entry)

            if due:
                games, seats_by_id = await fetch_data()
                game_lookup = {
                    game["id"]: game
                    for game in games
                }

                activated_games = []
                for entry in due:
                    game = game_lookup.get(
                        entry["game_id"]
                    )
                    if not game:
                        continue
                    parsed = parse_props(
                        game["properties"],
                        seats_by_id
                    )
                    activated_games.append(parsed)

                if activated_games:
                    message = format_activation_announcement(
                        activated_games
                    )
                    await channel.send(f"```{message}```")
                    logger.info(
                        f"Posted activation message for "
                        f"{len(activated_games)} game(s)"
                    )

            save_schedule(remaining)

        except Exception:
            logger.exception(
                "Error in activation scheduler"
            )

        await asyncio.sleep(60)

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
    if parsed.get("cover_url"):
        embed.set_image(url=parsed["cover_url"])

    return embed


class CopyView(discord.ui.View):

    def __init__(self, game_id: str, parsed: dict, message: str, cover_url: str = ""):
        super().__init__(timeout=None)

        self.game_id = game_id
        self.parsed = parsed
        self.message = message
        self.cover_url = cover_url

    @discord.ui.button(label="Copy Text", style=discord.ButtonStyle.secondary)
    async def copy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        chunks = chunk_text(self.message, limit=1900)
        await interaction.response.defer(ephemeral=True)
        for chunk in chunks:
            await interaction.followup.send(f"```{chunk}```", ephemeral=True)
        if self.cover_url:
            await interaction.followup.send(self.cover_url, ephemeral=True)

    @discord.ui.button(label="Announce", style=discord.ButtonStyle.success)
    async def announce_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        announced = load_announced()
        if self.game_id in announced:
            await interaction.response.send_message(
                "This game has already been announced.",
                ephemeral=True
            )
            return
        announced.add(self.game_id)
        save_announced(announced)
        await interaction.response.send_message(
            (
                f"Game marked as announced.\n"
                f"Simulated: Show = True"
            ),
            ephemeral=True
        )

    @discord.ui.button(label="Announce & Schedule", style=discord.ButtonStyle.primary)
    async def schedule_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        announced = load_announced()
        if self.game_id in announced:
            await interaction.response.send_message(
                "This game has already been announced.",
                ephemeral=True
            )
            return

        announced.add(self.game_id)
        save_announced(announced)
        activate_at = get_next_activation_time()
        schedule = load_schedule()
        schedule.append({
            "game_id": self.game_id,
            "activate_at": activate_at.isoformat()
        })
        save_schedule(schedule)
        formatted_time = activate_at.strftime(
            "%A, %B %d at %I:%M %p IST"
        )
        await interaction.response.send_message(
            (
                "Game marked as announced.\n"
                "Simulated: Show = True\n\n"
                f"Registration scheduled for:\n"
                f"{formatted_time}"
            ),
            ephemeral=True
        )

    @discord.ui.button(label="Unannounce (DEBUG)", style=discord.ButtonStyle.danger)
    async def unannounce_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        announced = load_announced()
        if self.game_id not in announced:
            await interaction.response.send_message(
                "Game is not marked announced.",
                ephemeral=True
            )
            return
        announced.remove(self.game_id)
        save_announced(announced)
        schedule = load_schedule()
        schedule = [
            s for s in schedule
            if s["game_id"] != self.game_id
        ]
        save_schedule(schedule)
        await interaction.response.send_message(
            (
                "Announcement state cleared.\n"
                "Removed any scheduled activation."
            ),
            ephemeral=True
        )

async def watchdog():
    while True:
        start = asyncio.get_running_loop().time()
        await asyncio.sleep(1)
        delta = (asyncio.get_running_loop().time() - start)

        if delta > 2:
            logger.warning(f"Event loop blocked for {delta:.2f}s")

@bot.event
async def on_ready():
    global monitor_task, scheduler_task
    logger.info(f"AGM Bot v{VERSION} starting up...")
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    guild = discord.Object(id=GUILD_ID)
    synced = await bot.tree.sync(guild=guild)
    logger.info(f"Synced commands to guild: {[cmd.name for cmd in synced]}")
    load_field_map()
    load_templates()
    load_help()
    bot.loop.create_task(watchdog())
    refresh_files()
    if monitor_task is None or monitor_task.done():
        monitor_task = bot.loop.create_task(monitor())
        logger.info("Started monitor task.")
    else:
        logger.info("Monitor task already running.")
    
    if scheduler_task is None or scheduler_task.done():
    	scheduler_task = bot.loop.create_task(activation_scheduler())
    	logger.info("Started activation scheduler task.")


@tree.command(name="ping", description="Reply with Pong!", guild=discord.Object(id=GUILD_ID))
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)

@tree.command(name="create-poster", description="Generate a poster for a specific game", guild=discord.Object(id=GUILD_ID))
async def create_poster(interaction: discord.Interaction, number: int, teaser: str, offset_x: int = None, offset_y: int = None):
    await interaction.response.defer(ephemeral=True)
    games, seats_by_id = await fetch_data()
    if number < 1 or number > len(games):
        await interaction.followup.send(f"Please enter a number between 1 and {len(games)}.", ephemeral=True)
        return
    game = games[number - 1]
    parsed = parse_props(game["properties"], seats_by_id)
    teaser = teaser.replace("|", "\n")

    path = generate_poster(parsed, teaser, offset_x, offset_y)
    await interaction.followup.send(file=discord.File(path), ephemeral=True)


@tree.command(name="list-games", description="List all games currently on Notion", guild=discord.Object(id=GUILD_ID))
async def list_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await games_list()
    chunks = chunk_text(result, limit=4000)
    view = ListingView(chunks)
    await interaction.followup.send(embed=view.make_embed(), view=view, ephemeral=True)


@tree.command(name="get-game", description="Get announcement block for a specific game",
              guild=discord.Object(id=GUILD_ID))
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
        view=CopyView(game["id"], parsed, message, parsed["cover_url"]),
        ephemeral=True
    )


@tree.command(name="open-seats", description="Show all games with available seats", guild=discord.Object(id=GUILD_ID))
async def open_seats_command(interaction: discord.Interaction):
    start = time.monotonic()
    logger.info("open-seats invoked")
    await interaction.response.defer(ephemeral=True)
    logger.info(f"Deferred after {time.monotonic() - start:.2f}s")
    result = await fetch_open_seats()
    logger.info(f"Fetched seats after {time.monotonic() - start:.2f}s")
    if not result:
        await interaction.followup.send("No games with open seats at the moment.", ephemeral=True)
        return
    chunks = chunk_text(result)

    for i, chunk in enumerate(chunks):
        await interaction.followup.send(
            f"```{chunk}```",
            ephemeral=True
        )


@tree.command(name="reload-config", description="Reload templates and parser configs",
              guild=discord.Object(id=GUILD_ID))
async def reload_config(interaction):
    if ROLE_ADMIN not in {
        role.id for role in interaction.user.roles
    }:
        await interaction.response.send_message("You do not have permission.", ephemeral=True)
        return
    load_templates()
    load_field_map()
    load_help()
    user = interaction.user
    logger.info(f"{user} (ID: {user.id}) reloaded config.")
    await interaction.response.send_message(
        "Config reloaded.",
        ephemeral=True
    )


@tree.command(name="download-file", description="Download a template or config file", guild=discord.Object(id=GUILD_ID))
@app_commands.autocomplete(file=file_autocomplete)
async def download_file(
        interaction: discord.Interaction,
        file: str
):
    refresh_files()
    if not has_file_permission(interaction, file):
        await interaction.response.send_message(
            "You do not have permission for that file.",
            ephemeral=True
        )
        return
    path = get_file_path(file)
    if not path or not os.path.exists(path):
        await interaction.response.send_message(
            "File not found.",
            ephemeral=True
        )
        return
    logger.info(
        f"{interaction.user} (ID: {interaction.user.id}) "
        f"downloaded {path}"
    )
    await interaction.response.send_message(
        file=discord.File(path),
        ephemeral=True
    )


@tree.command(name="upload-file", description="Upload a replacement template/config file",
              guild=discord.Object(id=GUILD_ID))
@app_commands.autocomplete(file=file_autocomplete)
async def upload_file(
        interaction: discord.Interaction,
        file: str,
        attachment: discord.Attachment
):
    refresh_files()
    if not has_file_permission(interaction, file):
        await interaction.response.send_message(
            "You do not have permission for that file.",
            ephemeral=True
        )
        return
    path = get_file_path(file)
    if not path:
        await interaction.response.send_message(
            "Invalid file.",
            ephemeral=True
        )
        return

    # Validate extension matches
    expected_ext = os.path.splitext(path)[1]
    uploaded_ext = os.path.splitext(attachment.filename)[1]

    if expected_ext != uploaded_ext:
        await interaction.response.send_message(
            f"Expected a {expected_ext} file.",
            ephemeral=True
        )
        return

    temp_path = f"{path}.tmp"

    try:

        await attachment.save(temp_path)
        # Validate YAML before replacing
        if path.endswith(".yaml"):

            with open(temp_path, "r", encoding="utf-8") as f:
                loaded_yaml = yaml.safe_load(f)

            required_keys = {"notion", "parser"}

            for key, value in loaded_yaml.items():

                if not isinstance(value, dict):
                    raise ValueError(
                        f"{key} must contain a mapping."
                    )

                if not required_keys.issubset(value):
                    raise ValueError(
                        f"{key} missing required keys: "
                        f"{required_keys}"
                    )

                if value["parser"] not in PARSERS:
                    raise ValueError(
                        f"{key} uses invalid parser: "
                        f"{value['parser']}"
                    )

        # Validate template formatting
        if path.endswith(".txt"):
            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            if "{" not in content:
                logger.warning(
                    f"Template {path} contains no placeholders."
                )

        # Create backup and upload new file
        # Create backup first
        if os.path.exists(path):
            original = Path(path)

            timestamp = datetime.now().strftime(
                "%Y%m%d%H%M%S"
            )

            backup_name = (
                f"{original.stem}-"
                f"{timestamp}"
                f"{original.suffix}.bak"
            )

            backup_path = (
                    original.parent / backup_name
            )

            shutil.copy2(path, backup_path)

            logger.info(
                f"Created backup: {backup_path}"
            )

        # Replace live file
        os.replace(temp_path, path)

        # Reload caches
        load_templates()
        load_field_map()

        logger.info(
            f"{interaction.user} (ID: {interaction.user.id}) "
            f"uploaded new version of {path}"
        )

        await interaction.response.send_message(
            f"Updated `{file}` successfully.",
            ephemeral=True
        )

    except Exception as e:

        logger.exception(
            f"Upload failed for {path}"
        )

        await interaction.response.send_message(
            f"Upload failed:\n```{e}```",
            ephemeral=True
        )

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

@tree.command(name="get-props", description="Show parsed template properties for a game", guild=discord.Object(id=GUILD_ID))
async def get_props_command(
        interaction: discord.Interaction,
        number: int
):
    await interaction.response.defer(ephemeral=True)

    games, seats_by_id = await fetch_data()

    if number < 1 or number > len(games):
        await interaction.followup.send(
            f"Please enter a number between 1 and {len(games)}.",
            ephemeral=True
        )
        return

    game = games[number - 1]

    parsed = parse_props(
        game["properties"],
        seats_by_id
    )

    lines = []

    for key, value in parsed.items():

        value_type = type(value).__name__

        if isinstance(value, dict):
            value = json.dumps(
                value,
                indent=2,
                default=str
            )

        lines.append(
            f"{{{key}}} (type: {value_type}) (len: {len(str(value))}) = {value}"
        )

    output = "\n".join(lines)

    chunks = chunk_text(output)

    for chunk in chunks:
        await interaction.followup.send(
            f"```{chunk}```",
            ephemeral=True
        )

@tree.command(name="help", description="Show AGM Bot help", guild=discord.Object(id=GUILD_ID))
async def help_command(
        interaction: discord.Interaction
):

    chunks = chunk_text(
        HELP_TEXT,
        limit=1975
    )

    await interaction.response.defer(
        ephemeral=True
    )

    for chunk in chunks:
        await interaction.followup.send(
            f"```{chunk}```",
            ephemeral=True
        )
        
@bot.event
async def on_disconnect():
    logger.warning("Disconnected from Discord")

@bot.event
async def on_connect():
    logger.info("Connected to Discord")

@bot.event
async def on_resumed():
    logger.info("Discord session resumed")
    
from discord.errors import NotFound

@tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    original = getattr(error, "original", error)

    if isinstance(original, NotFound):
        logger.warning(
            "Interaction expired before response "
            "(likely reconnect/network issue)"
        )
        return

    logger.exception("Unhandled app command error", exc_info=error)

bot.run(DISCORD_TOKEN)

