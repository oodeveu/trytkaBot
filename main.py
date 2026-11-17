import os
import sqlite3
from collections import defaultdict, deque
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from google import genai
from google.genai import types

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
TZ = ZoneInfo("Europe/Warsaw")

# ---------- LLM CHAT ----------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
AI_CHAT_CHANNEL = os.getenv("AI_CHAT_CHANNEL")
GEMINI_MODEL="gemini-3.8-flash"

AI_CHAT_CHANNEL_ID = int(AI_CHAT_CHANNEL) if AI_CHAT_CHANNEL else None
AI_HISTORY_LEN = 10
AI_COOLDOWN = 3
AI_SYSTEM_PROMPT = (
    "Jesteś pomocnym asystentem na serwerze Discord. Odpowiadaj po polsku, "
    "chyba że użytkownik pisze w innym języku. Pisz zwięźle i konkretnie."
)

gemini = (
    genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version="v1beta"),
    )
    if GEMINI_API_KEY
    else None
)
ai_history: dict[tuple[int, int], deque] = defaultdict(
    lambda: deque(maxlen=AI_HISTORY_LEN * 2)
)
ai_last_use: dict[int, float] = {}

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
CREATE TABLE IF NOT EXISTS voice_hub(
    guild_id INTEGER PRIMARY KEY, channel_id INTEGER, category_id INTEGER
);
CREATE TABLE IF NOT EXISTS temp_channels(
    channel_id INTEGER PRIMARY KEY, guild_id INTEGER, owner_id INTEGER
);
CREATE TABLE IF NOT EXISTS message_counts(
    guild_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
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
    daily_summary.start()


@bot.event
async def on_ready():
    # uwzględnij osoby, które już siedzą na VC w momencie startu bota
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for m in vc.members:
                if not m.bot:
                    voice_sessions.setdefault((guild.id, m.id), now())
    # usuń puste kanały tymczasowe pozostałe po restarcie bota
    for (channel_id,) in db.execute("SELECT channel_id FROM temp_channels").fetchall():
        ch = bot.get_channel(channel_id)
        if ch is None:
            db.execute("DELETE FROM temp_channels WHERE channel_id=?", (channel_id,))
            db.commit()
        else:
            await delete_if_empty_temp(ch)
    print(f"Zalogowano jako {bot.user}")


OWNER_PERMS = discord.PermissionOverwrite(
    view_channel=True,
    connect=True,
    speak=True,
    stream=True,
    use_voice_activation=True,
    priority_speaker=True,
    mute_members=True,
    deafen_members=True,
    move_members=True,
    manage_channels=True,
    manage_permissions=True,
    create_instant_invite=True,
)


async def handle_hub_join(member: discord.Member, category_id: int):
    """Tworzy prywatny kanał dla osoby, która weszła na kanał-hub, i ją tam przenosi."""
    guild = member.guild

    # jeśli osoba ma już swój kanał - przenieś ją tam zamiast tworzyć kolejny
    existing = db.execute(
        "SELECT channel_id FROM temp_channels WHERE guild_id=? AND owner_id=?",
        (guild.id, member.id),
    ).fetchone()
    if existing:
        ch = guild.get_channel(existing[0])
        if ch:
            try:
                await member.move_to(ch)
            except discord.HTTPException:
                pass
            return
        db.execute("DELETE FROM temp_channels WHERE channel_id=?", (existing[0],))
        db.commit()

    category = guild.get_channel(category_id)
    try:
        ch = await guild.create_voice_channel(
            name=f"Kanał {member.display_name}",
            category=category,
            overwrites={member: OWNER_PERMS},
            reason=f"Prywatny kanał dla {member}",
        )
    except discord.HTTPException as e:
        print(f"Nie udało się utworzyć kanału: {e}")
        return

    db.execute(
        "INSERT OR REPLACE INTO temp_channels VALUES(?,?,?)",
        (ch.id, guild.id, member.id),
    )
    db.commit()
    try:
        await member.move_to(ch)
    except discord.HTTPException:
        # osoba zdążyła wyjść - nie zostawiamy pustego kanału
        await ch.delete()
        db.execute("DELETE FROM temp_channels WHERE channel_id=?", (ch.id,))
        db.commit()


async def delete_if_empty_temp(channel: discord.VoiceChannel):
    row = db.execute(
        "SELECT 1 FROM temp_channels WHERE channel_id=?", (channel.id,)
    ).fetchone()
    if row and len(channel.members) == 0:
        try:
            await channel.delete(reason="Pusty kanał tymczasowy")
        except discord.HTTPException:
            pass
        db.execute("DELETE FROM temp_channels WHERE channel_id=?", (channel.id,))
        db.commit()


@bot.event
async def on_guild_channel_delete(channel):
    db.execute("DELETE FROM temp_channels WHERE channel_id=?", (channel.id,))
    db.commit()


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    # --- statystyki czasu na VC ---
    key = (member.guild.id, member.id)
    if before.channel is None and after.channel is not None:
        voice_sessions[key] = now()
    elif before.channel is not None and after.channel is None:
        start = voice_sessions.pop(key, None)
        if start:
            add_voice_time(member.guild.id, member.id, start, now())

    # --- kanały tymczasowe ---
    if after.channel and after.channel != before.channel:
        hub = db.execute(
            "SELECT channel_id, category_id FROM voice_hub WHERE guild_id=?",
            (member.guild.id,),
        ).fetchone()
        if hub and after.channel.id == hub[0]:
            await handle_hub_join(member, hub[1])

    if before.channel and before.channel != after.channel:
        await delete_if_empty_temp(before.channel)


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

    db.execute(
        """INSERT INTO message_counts(guild_id, user_id, count) VALUES(?,?,1)
           ON CONFLICT(guild_id, user_id) DO UPDATE SET count = count + 1""",
        (message.guild.id, message.author.id),
    )
    db.commit()

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

# ---------- LLM CHAT ----------
def split_message(text: str, limit: int = 2000) -> list[str]:
    """Dzieli długą odpowiedź na kawałki mieszczące się w limicie Discorda."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


async def ask_gemini(key: tuple[int, int], user_text: str) -> str:
    history = ai_history[key]
    user_content = types.Content(role="user", parts=[types.Part(text=user_text)])

    resp = await gemini.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=list(history) + [user_content],
        config=types.GenerateContentConfig(
            system_instruction=AI_SYSTEM_PROMPT,
            max_output_tokens=1024,
            temperature=0.8,
        ),
    )
    answer = (resp.text or "").strip()
    if answer:
        history.append(user_content)
        history.append(types.Content(role="model", parts=[types.Part(text=answer)]))
    return answer


@bot.listen("on_message")
async def ai_chat_listener(message: discord.Message):
    if message.author.bot or not message.guild or gemini is None:
        return
    if message.channel.id != AI_CHAT_CHANNEL_ID:
        return
    text = message.content.strip()
    if not text or text.startswith(bot.command_prefix):
        return

    t = now().timestamp()
    if t - ai_last_use.get(message.author.id, 0) < AI_COOLDOWN:
        return
    ai_last_use[message.author.id] = t

    key = (message.channel.id, message.author.id)
    no_pings = discord.AllowedMentions.none()
    async with message.channel.typing():
        try:
            answer = await ask_gemini(key, text)
        except Exception as e:
            print(f"Błąd Gemini: {e}")
            await message.reply(
                "Wystąpił błąd przy łączeniu z AI, spróbuj za chwilę.",
                mention_author=False,
            )
            return

    if not answer:
        await message.reply(
            "Nie udało mi się wygenerować odpowiedzi (możliwy filtr treści).",
            mention_author=False,
        )
        return

    for i, chunk in enumerate(split_message(answer)):
        if i == 0:
            await message.reply(chunk, mention_author=False, allowed_mentions=no_pings)
        else:
            await message.channel.send(chunk, allowed_mentions=no_pings)


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


@bot.tree.command(name="stats", description="Pokazuje łączne statystyki użytkownika")
@app_commands.describe(osoba="Użytkownik (domyślnie Ty)")
async def stats(interaction: discord.Interaction, osoba: discord.Member | None = None):
    member = osoba or interaction.user
    gid = interaction.guild_id

    rows = db.execute(
        "SELECT day, seconds FROM voice_daily WHERE guild_id=? AND user_id=?",
        (gid, member.id),
    ).fetchall()
    total = sum(s for _, s in rows)

    # doliczamy trwającą teraz sesję
    active = voice_sessions.get((gid, member.id))
    if active:
        total += int((now() - active).total_seconds())

    days = len(rows)
    avg = total // max(days, 1)
    best = max(rows, key=lambda r: r[1]) if rows else None

    ranking = db.execute(
        "SELECT user_id FROM voice_daily WHERE guild_id=? "
        "GROUP BY user_id ORDER BY SUM(seconds) DESC",
        (gid,),
    ).fetchall()
    place = next((i for i, (uid,) in enumerate(ranking, 1) if uid == member.id), None)

    msgs = db.execute(
        "SELECT count FROM message_counts WHERE guild_id=? AND user_id=?",
        (gid, member.id),
    ).fetchone()

    embed = discord.Embed(
        title=f"Statystyki - {member.display_name}", color=discord.Color.blurple()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Łączny czas na VC", value=fmt_duration(total) if total else "0s")
    embed.add_field(name="Miejsce w rankingu VC", value=f"#{place}" if place else "-")
    embed.add_field(name="Dni aktywności na VC", value=str(days))
    embed.add_field(name="Średnio dziennie", value=fmt_duration(avg) if total else "0s")
    if best:
        best_day = datetime.fromisoformat(best[0]).strftime("%d.%m.%Y")
        embed.add_field(
            name="Rekordowy dzień", value=f"{best_day} ({fmt_duration(best[1])})"
        )
    embed.add_field(name="Wiadomości", value=str(msgs[0] if msgs else 0))
    if member.joined_at:
        embed.add_field(
            name="Na serwerze od", value=discord.utils.format_dt(member.joined_at, "D")
        )
    embed.set_footer(text="Statystyki liczone od momentu dodania bota")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Pokazuje ranking użytkowników pod względem czasu spędzonego na VC")
async def leaderboard(interaction: discord.Interaction):
    gid = interaction.guild_id
    caller_id = interaction.user.id

    # Pobieramy sumę sekund z bazy i konwertujemy user_id na int
    rows = db.execute(
        "SELECT user_id, SUM(seconds) as total_seconds FROM voice_daily WHERE guild_id=? GROUP BY user_id",
        (gid,),
    ).fetchall()

    # Słownik z zabezpieczeniem, że klucz to zawsze int
    user_totals = {int(row[0]): row[1] for row in rows}

    # Doliczamy aktywne sesje z uwzględnieniem konwersji na int
    for (g_id, u_id), start_time in voice_sessions.items():
        if g_id == gid:
            active_secs = int((now() - start_time).total_seconds())
            u_id_int = int(u_id)
            user_totals[u_id_int] = user_totals.get(u_id_int, 0) + active_secs

    # Sortujemy użytkowników malejąco według czasu
    sorted_ranking = sorted(user_totals.items(), key=lambda item: item[1], reverse=True)

    # Szukamy miejsca osoby wywołującej komendę
    caller_place = next((i for i, (uid, _) in enumerate(sorted_ranking, 1) if uid == caller_id), None)

    # Bierzemy top 10
    top_10 = sorted_ranking[:10]

    embed = discord.Embed(
        title="🏆 Ranking aktywności na VC",
        color=discord.Color.gold()
    )

    if not top_10:
        embed.description = "Brak danych o aktywności na tym serwerze."
    else:
        desc_lines = []
        medals = ["🥇", "🥈", "🥉"]

        for index, (uid, total_secs) in enumerate(top_10, 1):
            prefix = medals[index - 1] if index <= 3 else f"`#{index}.`"
            member = interaction.guild.get_member(uid)
            name = member.mention if member else f"<@!{uid}>"
            desc_lines.append(f"{prefix} {name} — **{fmt_duration(total_secs)}**")

        embed.description = "\n".join(desc_lines)

    # Jeśli osoba wywołująca komendę jest poza top 10, dopisujemy jej pozycję w stopce
    if caller_place and caller_place > 10:
        caller_secs = user_totals.get(caller_id, 0)
        embed.set_footer(text=f"Twoje miejsce: #{caller_place} ({fmt_duration(caller_secs)})")
    else:
        embed.set_footer(text="Statystyki liczone od momentu dodania bota")

    await interaction.response.send_message(embed=embed)

@bot.tree.command(
    name="voice-setup",
    description="Ustawia kanał, który tworzy prywatne kanały głosowe",
)
@app_commands.describe(
    kanal="Kanał głosowy, na który wchodzi się, by stworzyć własny kanał",
    kategoria="Kategoria, w której będą tworzone kanały",
)
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
async def voice_setup(
    interaction: discord.Interaction,
    kanal: discord.VoiceChannel,
    kategoria: discord.CategoryChannel,
):
    db.execute(
        "INSERT OR REPLACE INTO voice_hub VALUES(?,?,?)",
        (interaction.guild_id, kanal.id, kategoria.id),
    )
    db.commit()
    await interaction.response.send_message(
        f"Wejście na {kanal.mention} utworzy kanał w kategorii **{kategoria.name}**.",
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

@bot.tree.command(name="ai-reset", description="Resetuje Twoją historię rozmowy z AI")
async def ai_reset(interaction: discord.Interaction):
    ai_history.pop((interaction.channel_id, interaction.user.id), None)
    await interaction.response.send_message("Historia rozmowy z AI wyczyszczona.", ephemeral=True)

if __name__ == "__main__":
    bot.run(TOKEN)