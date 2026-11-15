import os
import sqlite3
from collections import defaultdict, deque
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
TZ = ZoneInfo("Europe/Warsaw")

# ---------- KONFIGURACJA ANTY SPAMU ----------
SPAM_MSG_LIMIT = 5          # tyle wiadomości...
SPAM_MSG_WINDOW = 5         # ...w tylu sekundach
SPAM_DUP_LIMIT = 3          # tyle identycznych wiadomości...
SPAM_DUP_WINDOW = 15        # ...w tylu sekundach
SPAM_MAX_MENTIONS = 5       # max wzmianek w jednej wiadomości
SPAM_TIMEOUT_MIN = 5        # czas wyciszenia (minuty)

# ---------- BAZA DANYCH ----------
db = sqlite3.connect("bot.db")
db.executescript(
    """
CREATE TABLE IF NOT EXISTS verifications(
    message_id INTEGER PRIMARY KEY,
    guild_id INTEGER, channel_id INTEGER,
    before_role INTEGER, after_role INTEGER, emoji TEXT
);
CREATE TABLE IF NOT EXISTS welcome(
    guild_id INTEGER PRIMARY KEY, channel_id INTEGER, content TEXT
);
CREATE TABLE IF NOT EXISTS stats_channel(
    guild_id INTEGER PRIMARY KEY, channel_id INTEGER
);
CREATE TABLE IF NOT EXISTS voice_daily(
    guild_id INTEGER, day TEXT, user_id INTEGER, seconds INTEGER,
    PRIMARY KEY (guild_id, day, user_id)
);
"""
)
db.commit()

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

# (guild_id, user_id) -> datetime rozpoczęcia sesji na VC
voice_sessions: dict[tuple[int, int], datetime] = {}
# anty spam
msg_times: dict[tuple[int, int], deque] = defaultdict(deque)
msg_dups: dict[tuple[int, int], deque] = defaultdict(deque)


def now() -> datetime:
    return datetime.now(TZ)


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# ---------- STATYSTYKI VC: ZAPIS CZASU ----------
def add_voice_time(guild_id: int, user_id: int, start: datetime, end: datetime):
    """Dodaje czas do bazy, dzieląc sesję na dni (gdy trwa przez północ)."""
    cur = start
    while cur < end:
        next_midnight = (cur + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        part_end = min(end, next_midnight)
        secs = int((part_end - cur).total_seconds())
        if secs > 0:
            day = cur.date().isoformat()
            db.execute(
                """INSERT INTO voice_daily(guild_id, day, user_id, seconds)
                   VALUES(?,?,?,?)
                   ON CONFLICT(guild_id, day, user_id)
                   DO UPDATE SET seconds = seconds + excluded.seconds""",
                (guild_id, day, user_id, secs),
            )
        cur = part_end
    db.commit()


def flush_active_sessions():
    """Zapisuje trwające sesje i restartuje ich licznik od teraz."""
    t = now()
    for (gid, uid), start in list(voice_sessions.items()):
        add_voice_time(gid, uid, start, t)
        voice_sessions[(gid, uid)] = t


# ---------- ZDARZENIA ----------
@bot.event
async def setup_hook():
    await bot.tree.sync()
    daily_summary.start()


@bot.event
async def on_ready():
    # uwzględnij osoby, które już siedzą na VC w momencie startu bota
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for m in vc.members:
                if not m.bot:
                    voice_sessions.setdefault((guild.id, m.id), now())
    print(f"Zalogowano jako {bot.user}")


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    key = (member.guild.id, member.id)
    if before.channel is None and after.channel is not None:
        voice_sessions[key] = now()
    elif before.channel is not None and after.channel is None:
        start = voice_sessions.pop(key, None)
        if start:
            add_voice_time(member.guild.id, member.id, start, now())


@bot.event
async def on_member_join(member):
    row = db.execute(
        "SELECT channel_id, content FROM welcome WHERE guild_id=?", (member.guild.id,)
    ).fetchone()
    if not row:
        return
    channel = member.guild.get_channel(row[0])
    if not channel:
        return
    text = (
        row[1]
        .replace("{user}", member.mention)
        .replace("{server}", member.guild.name)
        .replace("{count}", str(member.guild.member_count))
    )
    embed = discord.Embed(description=text, color=discord.Color.green())
    embed.set_thumbnail(url=member.display_avatar.url)
    await channel.send(content=member.mention, embed=embed)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id or not payload.guild_id:
        return
    row = db.execute(
        "SELECT before_role, after_role, emoji FROM verifications WHERE message_id=?",
        (payload.message_id,),
    ).fetchone()
    if not row or str(payload.emoji) != row[2]:
        return
    guild = bot.get_guild(payload.guild_id)
    member = payload.member or guild.get_member(payload.user_id)
    before_role, after_role = guild.get_role(row[0]), guild.get_role(row[1])
    if not member or not before_role or not after_role:
        return
    try:
        if before_role in member.roles:
            await member.remove_roles(before_role, reason="Weryfikacja")
        await member.add_roles(after_role, reason="Weryfikacja")
    except discord.Forbidden:
        print("Brak uprawnień do nadania rang - sprawdź hierarchię ról bota.")
    # sprzątanie reakcji użytkownika
    try:
        channel = guild.get_channel(payload.channel_id)
        msg = channel.get_partial_message(payload.message_id)
        await msg.remove_reaction(payload.emoji, member)
    except (discord.HTTPException, AttributeError):
        pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    await bot.process_commands(message)

    perms = message.author.guild_permissions
    if perms.administrator or perms.manage_messages:
        return

    key = (message.guild.id, message.author.id)
    t = now().timestamp()

    times = msg_times[key]
    times.append(t)
    while times and t - times[0] > SPAM_MSG_WINDOW:
        times.popleft()

    dups = msg_dups[key]
    dups.append((t, message.content))
    while dups and t - dups[0][0] > SPAM_DUP_WINDOW:
        dups.popleft()
    dup_count = sum(1 for _, c in dups if c and c == message.content)

    reason = None
    if len(times) >= SPAM_MSG_LIMIT:
        reason = "zbyt szybkie wysyłanie wiadomości"
    elif dup_count >= SPAM_DUP_LIMIT:
        reason = "powtarzanie tych samych wiadomości"
    elif len(message.mentions) + len(message.role_mentions) >= SPAM_MAX_MENTIONS:
        reason = "masowe oznaczanie"

    if not reason:
        return

    times.clear()
    dups.clear()
    try:
        cutoff = discord.utils.utcnow() - timedelta(seconds=60)
        await message.channel.purge(
            limit=50,
            check=lambda m: m.author.id == message.author.id and m.created_at > cutoff,
        )
        await message.author.timeout(
            timedelta(minutes=SPAM_TIMEOUT_MIN), reason=f"Anty spam: {reason}"
        )
        await message.channel.send(
            f"{message.author.mention} został wyciszony na {SPAM_TIMEOUT_MIN} min "
            f"({reason}).",
            delete_after=10,
        )
    except discord.Forbidden:
        await message.channel.send(
            "Wykryto spam, ale bot nie ma uprawnień (Moderate Members / Manage Messages).",
            delete_after=10,
        )


# ---------- KOMENDY ----------
@bot.tree.command(
    name="verify-create",
    description="Tworzy wiadomość weryfikacyjną z reakcją nadającą rangę",
)
@app_commands.describe(
    ranga_przed="Ranga zabierana po weryfikacji (np. Niezweryfikowany)",
    ranga_po="Ranga nadawana po weryfikacji (np. Członek)",
    tresc="Treść wiadomości weryfikacyjnej",
    emoji="Emoji reakcji, którą trzeba kliknąć",
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.checks.has_permissions(manage_roles=True)
async def verify_create(
    interaction: discord.Interaction,
    ranga_przed: discord.Role,
    ranga_po: discord.Role,
    tresc: str,
    emoji: str,
):
    me = interaction.guild.me
    if ranga_po >= me.top_role or ranga_przed >= me.top_role:
        return await interaction.response.send_message(
            "Obie rangi muszą być **niżej** niż najwyższa ranga bota.", ephemeral=True
        )

    embed = discord.Embed(
        title="Weryfikacja", description=tresc, color=discord.Color.blurple()
    )
    await interaction.response.send_message("Tworzę weryfikację...", ephemeral=True)
    msg = await interaction.channel.send(embed=embed)
    try:
        await msg.add_reaction(emoji)
    except discord.HTTPException:
        await msg.delete()
        return await interaction.edit_original_response(
            content="Nieprawidłowe emoji. Użyj zwykłego emoji lub emoji z tego serwera."
        )

    db.execute(
        "INSERT OR REPLACE INTO verifications VALUES(?,?,?,?,?,?)",
        (
            msg.id,
            interaction.guild_id,
            interaction.channel_id,
            ranga_przed.id,
            ranga_po.id,
            emoji,
        ),
    )
    db.commit()
    await interaction.edit_original_response(content="Weryfikacja utworzona.")


@bot.tree.command(name="clear", description="Czyści wiadomości na kanale")
@app_commands.describe(ilosc="Ile wiadomości usunąć (1-500)")
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(
    interaction: discord.Interaction, ilosc: app_commands.Range[int, 1, 500]
):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=ilosc)
    await interaction.followup.send(f"Usunięto {len(deleted)} wiadomości.", ephemeral=True)


@bot.tree.command(
    name="welcome-setup", description="Ustawia wiadomość powitalną dla nowych osób"
)
@app_commands.describe(
    kanal="Kanał z powitaniami",
    tresc="Treść. Zmienne: {user} {server} {count}",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome_setup(
    interaction: discord.Interaction, kanal: discord.TextChannel, tresc: str
):
    db.execute(
        "INSERT OR REPLACE INTO welcome VALUES(?,?,?)",
        (interaction.guild_id, kanal.id, tresc),
    )
    db.commit()
    await interaction.response.send_message(
        f"Powitania będą wysyłane na {kanal.mention}.", ephemeral=True
    )


@bot.tree.command(
    name="stats-setup", description="Ustawia kanał z dziennym podsumowaniem VC"
)
@app_commands.describe(kanal="Kanał, na który trafi podsumowanie o północy")
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def stats_setup(interaction: discord.Interaction, kanal: discord.TextChannel):
    db.execute(
        "INSERT OR REPLACE INTO stats_channel VALUES(?,?)",
        (interaction.guild_id, kanal.id),
    )
    db.commit()
    await interaction.response.send_message(
        f"Podsumowania będą wysyłane na {kanal.mention} codziennie o 00:00.",
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "Nie masz uprawnień do tej komendy."
    else:
        msg = f"Wystąpił błąd: {error}"
        print(error)
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ---------- DZIENNE PODSUMOWANIE ----------
@tasks.loop(time=dtime(0, 0, tzinfo=TZ))
async def daily_summary():
    flush_active_sessions()
    yesterday = (now() - timedelta(days=1)).date()
    day = yesterday.isoformat()

    for guild_id, channel_id in db.execute(
        "SELECT guild_id, channel_id FROM stats_channel"
    ).fetchall():
        channel = bot.get_channel(channel_id)
        if not channel:
            continue
        rows = db.execute(
            "SELECT user_id, seconds FROM voice_daily WHERE guild_id=? AND day=? "
            "ORDER BY seconds DESC",
            (guild_id, day),
        ).fetchall()

        embed = discord.Embed(
            title=f"Podsumowanie VC - {yesterday.strftime('%d.%m.%Y')}",
            color=discord.Color.gold(),
        )
        if not rows:
            embed.description = "Wczoraj nikt nie siedział na kanałach głosowych."
        else:
            total = sum(s for _, s in rows)
            lines = [
                f"**{i}.** <@{uid}> - {fmt_duration(secs)}"
                for i, (uid, secs) in enumerate(rows[:25], 1)
            ]
            if len(rows) > 25:
                lines.append(f"...i {len(rows) - 25} więcej")
            embed.description = "\n".join(lines)
            embed.add_field(name="Łącznie osób na VC", value=str(len(rows)))
            embed.add_field(name="Łączny czas", value=fmt_duration(total))
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


@daily_summary.before_loop
async def before_daily_summary():
    await bot.wait_until_ready()


if __name__ == "__main__":
    bot.run(TOKEN)