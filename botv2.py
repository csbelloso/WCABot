"""
Bot de Discord que anuncia nuevas competiciones de la WCA (World Cube Association)
en las provincias de: León, Palencia, Burgos, Soria, Segovia, Ávila, Salamanca,
Zamora y Valladolid (Castilla y León).

Fuente de datos: API pública de la WCA
https://www.worldcubeassociation.org/api/v0/competitions
"""

import os
import json
import logging
import unicodedata
from datetime import date, datetime

import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0") or "0")
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "30"))
SEEN_FILE = os.path.join(os.path.dirname(__file__), "seen_competitions.json")

WCA_API_BASE = "https://www.worldcubeassociation.org/api/v0"

# Provincias objetivo (Castilla y León)
PROVINCES = [
    "León", "Palencia", "Burgos", "Soria",
    "Segovia", "Ávila", "Salamanca", "Zamora", "Valladolid",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("wca-bot")


def normalize(text: str) -> str:
    """Quita tildes y pasa a minúsculas para comparar texto sin problemas de acentos."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


NORMALIZED_PROVINCES = {normalize(p): p for p in PROVINCES}

intents = discord.Intents.default()
intents.message_content = True  # necesario para los comandos con prefijo
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------------------
# Persistencia de competiciones ya anunciadas (para no repetir avisos)
# ---------------------------------------------------------------------------

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


seen_competitions = load_seen()


# ---------------------------------------------------------------------------
# Lógica de la API de la WCA
# ---------------------------------------------------------------------------

def matches_target_province(competition: dict):
    """Devuelve el nombre de la provincia si la competición pertenece a alguna
    de las provincias objetivo, o None si no coincide."""
    haystacks = [
        competition.get("city", "") or "",
        competition.get("venue_address", "") or "",
        competition.get("name", "") or "",
    ]
    haystack = normalize(" ".join(haystacks))
    for norm_prov, original in NORMALIZED_PROVINCES.items():
        if norm_prov in haystack:
            return original
    return None


async def fetch_spanish_competitions(session: aiohttp.ClientSession) -> list:
    """Descarga (paginando) las competiciones en España a partir de hoy."""
    competitions = []
    page = 1
    today = date.today().isoformat()

    while True:
        url = f"{WCA_API_BASE}/competitions"
        params = {"country_iso2": "ES", "start": today, "page": str(page)}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                logger.warning("La API de la WCA devolvió el estado %s", resp.status)
                break
            data = await resp.json()
            if not data:
                break
            competitions.extend(data)
            if len(data) < 25:  # la API pagina de 25 en 25; menos de 25 = última página
                break
            page += 1

    return competitions


def format_registration_open(comp: dict) -> str:
    """Formatea la fecha de apertura de inscripciones a AAAA-MM-DD HH:MM (UTC)."""
    raw = comp.get("registration_open")
    if not raw:
        return "Desconocida"
    try:
        # La API devuelve p. ej. "2026-01-01T12:00:00.000Z"
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y %H:%M UTC")
    except ValueError:
        return raw


def format_date_ddmmyyyy(raw: str) -> str:
    """Convierte una fecha en formato AAAA-MM-DD a DD/MM/AAAA. Si no se puede
    parsear, devuelve el valor original tal cual."""
    if not raw or raw == "?":
        return raw
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")
    except ValueError:
        return raw


def build_embed(comp: dict, province: str) -> discord.Embed:
    name = comp.get("name", "Competición sin nombre")
    city = comp.get("city", "Ciudad desconocida")
    start = format_date_ddmmyyyy(comp.get("start_date", "?"))
    end = format_date_ddmmyyyy(comp.get("end_date", "?"))
    comp_id = comp.get("id", "")
    url = f"https://www.worldcubeassociation.org/competitions/{comp_id}"

    fecha = start if start == end else f"{start} → {end}"
    registro = format_registration_open(comp)

    description = (
        f"📍 Localidad: {city}\n"
        f"🗺️ Provincia: {province}\n"
        f"📅 Fecha\n"
        f"{fecha}\n"
        f"Apertura de inscripciones: {registro}"
    )

    embed = discord.Embed(
        title=f"🎲 Nueva competición: {name}",
        url=url,
        description=description,
        color=discord.Color.blue(),
    )
    embed.set_footer(text="World Cube Association")
    return embed


async def check_new_competitions() -> int:
    """Consulta la API, anuncia las competiciones nuevas y actualiza el registro.
    Devuelve el número de competiciones nuevas anunciadas."""
    global seen_competitions

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        logger.error("No se pudo encontrar el canal con ID %s", CHANNEL_ID)
        return 0

    async with aiohttp.ClientSession() as session:
        try:
            competitions = await fetch_spanish_competitions(session)
        except Exception:
            logger.exception("Error al consultar la API de la WCA")
            return 0

    new_seen = set(seen_competitions)
    announced = 0

    for comp in competitions:
        comp_id = comp.get("id")
        if not comp_id or comp_id in seen_competitions:
            continue

        province = matches_target_province(comp)
        if not province:
            continue

        new_seen.add(comp_id)
        embed = build_embed(comp, province)
        try:
            await channel.send(embed=embed)
            announced += 1
            logger.info("Anunciada nueva competición: %s (%s)", comp.get("name"), province)
        except discord.DiscordException:
            logger.exception("Error al enviar el mensaje a Discord")

    if new_seen != seen_competitions:
        seen_competitions = new_seen
        save_seen(seen_competitions)

    return announced


# ---------------------------------------------------------------------------
# Tarea periódica
# ---------------------------------------------------------------------------

@tasks.loop(minutes=POLL_INTERVAL_MINUTES)
async def poll_wca():
    await check_new_competitions()


@poll_wca.before_loop
async def before_poll():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Eventos y comandos
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    logger.info("Conectado como %s (id: %s)", bot.user, bot.user.id)
    if not poll_wca.is_running():
        poll_wca.start()


@bot.command(name="comprobar")
async def comprobar(ctx: commands.Context):
    """Fuerza una comprobación manual de nuevas competiciones."""
    await ctx.send("🔍 Comprobando competiciones nuevas en la WCA...")
    n = await check_new_competitions()
    if n == 0:
        await ctx.send("✅ Comprobación completada. No hay competiciones nuevas.")
    else:
        await ctx.send(f"✅ Comprobación completada. Se han anunciado {n} competición(es) nueva(s).")


@bot.command(name="listar")
async def listar(ctx: commands.Context):
    """Lista todas las próximas competiciones detectadas en las provincias objetivo."""
    async with aiohttp.ClientSession() as session:
        competitions = await fetch_spanish_competitions(session)

    matches = [(c, matches_target_province(c)) for c in competitions]
    matches = [(c, p) for c, p in matches if p]

    if not matches:
        await ctx.send("No hay competiciones próximas detectadas en las provincias objetivo.")
        return

    lines = [
        f"• **{c.get('name')}** — {p} ({format_date_ddmmyyyy(c.get('start_date'))})"
        for c, p in matches
    ]
    await ctx.send("\n".join(lines))


@bot.command(name="provincias")
async def provincias(ctx: commands.Context):
    """Muestra la lista de provincias vigiladas por el bot."""
    await ctx.send("📍 Provincias vigiladas: " + ", ".join(PROVINCES))


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Falta DISCORD_TOKEN en el archivo .env")
    if not CHANNEL_ID:
        raise SystemExit("Falta CHANNEL_ID en el archivo .env")
    bot.run(DISCORD_TOKEN)
