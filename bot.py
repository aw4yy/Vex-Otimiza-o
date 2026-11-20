import os
import json
import tempfile
from contextlib import closing
from functools import wraps
from pathlib import Path
import hashlib
import hmac
import secrets
from html import escape
import asyncio
import threading
import sqlite3
import requests
import time
import urllib.parse
import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask, request, redirect, session
from werkzeug.serving import WSGIRequestHandler

# ── Servidor Web ──
# Cria a aplicação Flask que recebe os pedidos de verificação.
app = Flask(__name__)

# ── Base de dados SQLite ──
# Define onde fica o auth_tokens.db e guarda as autorizações e os fechos de tickets.
DATA_PATH = os.getenv("DATA_PATH")
if DATA_PATH:
    if not os.path.isdir(DATA_PATH):
        raise RuntimeError("DATA_PATH não aponta para uma pasta existente. Confirma o caminho da base de dados.")
    DB_FILE = os.path.join(DATA_PATH, "auth_tokens.db")
else:
    DB_FILE = "auth_tokens.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_tokens (
            user_id TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            expires_at REAL NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_closures (
            channel_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            closed_by TEXT NOT NULL,
            closed_at INTEGER NOT NULL,
            status TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_archives (
            channel_id TEXT PRIMARY KEY,
            log_url TEXT NOT NULL,
            archived_at INTEGER NOT NULL,
            closed_by TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_assignments (
            channel_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            staff_id TEXT NOT NULL,
            claimed_at INTEGER NOT NULL,
            panel_message_id TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS open_tickets (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            channel_id TEXT NOT NULL UNIQUE,
            PRIMARY KEY (guild_id, user_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ticket_cooldowns (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            next_allowed_at REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def save_user_tokens(user_id: str, access_token: str, refresh_token: str, expires_in: int):
    expires_at = time.time() + expires_in
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_tokens (user_id, access_token, refresh_token, expires_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            access_token=excluded.access_token,
            refresh_token=excluded.refresh_token,
            expires_at=excluded.expires_at
    ''', (str(user_id), access_token, refresh_token, expires_at))
    conn.commit()
    conn.close()

def get_all_tokens():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, access_token, refresh_token, expires_at FROM user_tokens')
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_user_token(user_id: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, access_token, refresh_token, expires_at FROM user_tokens WHERE user_id = ?', (str(user_id),))
    row = cursor.fetchone()
    conn.close()
    return row

# ── Verificação de membros (OAuth2) ──
# Inicia a autorização no Discord, valida a sessão e confirma a atribuição do cargo.
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

if CLIENT_SECRET:
    app.secret_key = os.getenv("FLASK_SECRET_KEY") or hmac.new(
        CLIENT_SECRET.encode(), b"vex-oauth-session-v1", hashlib.sha256
    ).hexdigest()
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax")

def get_verification_url():
    if not REDIRECT_URI:
        return None
    callback_url = urllib.parse.urlsplit(REDIRECT_URI)
    if callback_url.scheme != "https" or not callback_url.netloc:
        return None
    return urllib.parse.urlunsplit(
        (callback_url.scheme, callback_url.netloc, "/auth", "", "")
    )

@app.route('/')
def home():
    return "O bot e o servidor de verificação estão online."

@app.route('/auth')
def auth():
    if not CLIENT_ID or not CLIENT_SECRET or not get_verification_url():
        return "A verificação está temporariamente indisponível.", 503
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    session["oauth_started_at"] = time.time()
    query = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "identify guilds.join",
        "state": state,
    })
    return redirect(f"https://discord.com/oauth2/authorize?{query}")

@app.route('/callback')
def callback():
    expected_state = session.pop("oauth_state", None)
    started_at = session.pop("oauth_started_at", 0)
    supplied_state = request.args.get("state", "")
    if (
        not expected_state
        or not supplied_state.isascii()
        or not secrets.compare_digest(expected_state, supplied_state)
        or time.time() - started_at > 600
    ):
        return "Sessão inválida ou expirada. Volta a iniciar a verificação pelo painel do Discord.", 400

    if request.args.get("error"):
        return "Autorização cancelada ou recusada.", 400

    code = request.args.get("code")
    if not code:
        return "Código de autorização em falta. Volta a iniciar a verificação.", 400

    token_url = "https://discord.com/api/oauth2/token"
    payload = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT_URI
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}

    try:
        response = requests.post(token_url, data=payload, headers=headers, timeout=10)
        response.raise_for_status()
        token_data = response.json()
        access_token = token_data["access_token"]
        refresh_token = token_data["refresh_token"]
        expires_in = token_data["expires_in"]
        user_response = requests.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        user_response.raise_for_status()
        user_data = user_response.json()
        user_id = user_data["id"]
    except (requests.RequestException, ValueError, KeyError):
        app.logger.warning("Falhou a obtenção da autorização ou do perfil Discord.")
        return "Não foi possível confirmar a conta. Tenta novamente.", 502

    avatar_hash = user_data.get('avatar')

    if avatar_hash:
        user_avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=128"
    else:
        user_avatar_url = "https://cdn.discordapp.com/embed/avatars/0.png"

    bot_token = os.getenv("DISCORD_TOKEN")
    guild_id = os.getenv("GUILD_ID")
    if not guild_id and len(bot.guilds) == 1:
        guild_id = str(bot.guilds[0].id)
    if not guild_id or not bot_token:
        return "A verificação está temporariamente indisponível.", 503

    guild_url = f"https://discord.com/api/v10/guilds/{guild_id}"
    role_url = f"{guild_url}/members/{user_id}/roles/{ID_CARGO_MEMBRO}"
    bot_headers = {"Authorization": f"Bot {bot_token}"}
    try:
        role_response = requests.put(role_url, headers=bot_headers, timeout=10)
    except requests.RequestException:
        return "O Discord não respondeu ao pedido de atribuição do cargo. Tenta novamente.", 502
    if role_response.status_code != 204:
        app.logger.warning("A atribuição do cargo falhou (%s).", role_response.status_code)
        return "Não foi possível atribuir o cargo. Confirma se estás no servidor e contacta a equipa se o erro continuar.", 502

    try:
        save_user_tokens(user_id, access_token, refresh_token, expires_in)
    except sqlite3.Error:
        app.logger.exception("Não foi possível guardar a verificação.")
        return "O cargo foi atribuído, mas não foi possível guardar a verificação. Tenta novamente.", 503

    guild_icon_url = ""
    guild_banner_url = ""
    guild_name = "Vex Otimização"
    try:
        guild_response = requests.get(guild_url, headers=bot_headers, timeout=5)
        if guild_response.ok:
            guild_data = guild_response.json()
            guild_name = escape(guild_data.get("name", guild_name))
            if guild_data.get("icon"):
                guild_icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{guild_data['icon']}.png?size=128"
            if guild_data.get("banner"):
                guild_banner_url = f"https://cdn.discordapp.com/banners/{guild_id}/{guild_data['banner']}.png?size=600"
    except (requests.RequestException, ValueError):
        app.logger.warning("Não foi possível obter as imagens do servidor.")

    data_hora_atual = time.strftime("%d/%m/%Y, %H:%M")
    banner_style = f"background-image: url('{guild_banner_url}');" if guild_banner_url else "background: linear-gradient(135deg, #1f2235 0%, #0d0e14 100%);"
    server_icon_html = f"<img src='{guild_icon_url}' class='server-icon-img' alt='Logo'>" if guild_icon_url else "<div class='server-icon'>VEX</div>"

    return f"""
    <!DOCTYPE html>
    <html lang="pt">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Verificação Concluída - {guild_name}</title>
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; }}
            body {{
                background: linear-gradient(-45deg, #090a0f, #121520, #08090d, #1a1228);
                background-size: 400% 400%;
                animation: animateBackground 12s ease infinite;
                color: #ffffff; display: flex; justify-content: center; align-items: center;
                min-height: 100vh; padding: 20px; overflow: hidden; position: relative;
            }}
            @keyframes animateBackground {{
                0% {{ background-position: 0% 50%; }}
                50% {{ background-position: 100% 50%; }}
                100% {{ background-position: 0% 50%; }}
            }}
            .glow-1, .glow-2 {{
                position: absolute; width: 350px; height: 350px; border-radius: 50%;
                filter: blur(100px); opacity: 0.25; z-index: 0; animation: floatGlow 10s ease-in-out infinite alternate;
            }}
            .glow-1 {{ background: #5865f2; top: 15%; left: 20%; }}
            .glow-2 {{ background: #23a55a; bottom: 15%; right: 20%; animation-delay: -5s; }}
            @keyframes floatGlow {{
                0% {{ transform: translate(0, 0) scale(1); }}
                100% {{ transform: translate(40px, -50px) scale(1.3); }}
            }}
            .card {{
                position: relative; z-index: 1; background: rgba(20, 21, 26, 0.85);
                backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
                border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 20px;
                width: 100%; max-width: 400px; overflow: hidden;
                box-shadow: 0 20px 50px rgba(0, 0, 0, 0.8); text-align: center;
            }}
            .banner {{ width: 100%; height: 120px; {banner_style} background-size: cover; background-position: center; }}
            .avatar-container {{ position: relative; display: inline-block; margin-top: -34px; margin-bottom: 12px; }}
            .avatar {{ width: 68px; height: 68px; border-radius: 50%; border: 4px solid #14151a; object-fit: cover; box-shadow: 0 4px 15px rgba(0,0,0,0.5); background-color: #14151a; }}
            .badge {{ position: absolute; bottom: 2px; right: 2px; background-color: #23a55a; color: white; border-radius: 50%; width: 22px; height: 22px; display: flex; align-items: center; justify-content: center; font-size: 12px; border: 3px solid #14151a; }}
            .content {{ padding: 0 24px 26px 24px; }}
            .title {{ font-size: 20px; font-weight: 700; color: #ffffff; margin-bottom: 6px; }}
            .timestamp {{ font-size: 12px; color: #72767d; margin-bottom: 16px; }}
            .description {{ font-size: 13px; color: #96989d; line-height: 1.5; margin-bottom: 20px; }}
            .server-box {{ background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.06); border-radius: 12px; padding: 12px 14px; display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }}
            .server-info {{ display: flex; align-items: center; gap: 10px; }}
            .server-icon-img {{ width: 36px; height: 36px; border-radius: 10px; object-fit: cover; }}
            .server-icon {{ width: 36px; height: 36px; border-radius: 10px; background: #2b2d42; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 12px; }}
            .server-name {{ font-size: 13px; font-weight: 600; color: #ffffff; text-align: left; }}
            .verified-tag {{ font-size: 12px; color: #23a55a; font-weight: 600; }}
            .btn {{ display: block; width: 100%; padding: 13px; background: rgba(255, 255, 255, 0.08); color: #ffffff; text-decoration: none; font-weight: 600; font-size: 13px; border-radius: 10px; transition: all 0.2s ease; border: 1px solid rgba(255, 255, 255, 0.1); }}
            .btn:hover {{ background: rgba(255, 255, 255, 0.15); transform: translateY(-2px); }}
        </style>
    </head>
    <body>
        <div class="glow-1"></div>
        <div class="glow-2"></div>
        <div class="card">
            <div class="banner"></div>
            <div class="avatar-container">
                <img src="{user_avatar_url}" class="avatar" alt="Avatar">
                <div class="badge">✓</div>
            </div>
            <div class="content">
                <h1 class="title">Verificação concluída</h1>
                <div class="timestamp">{data_hora_atual}</div>
                <p class="description">A tua conta foi verificada e o cargo de acesso ao servidor <strong>{guild_name}</strong> foi atribuído.</p>
                <div class="server-box">
                    <div class="server-info">
                        {server_icon_html}
                        <div class="server-name">{guild_name}</div>
                    </div>
                    <div class="verified-tag">✓ Verificação concluída</div>
                </div>
                <a href="discord://" class="btn">Voltar ao Discord</a>
            </div>
        </div>
    </body>
    </html>
    """

# ── Arranque do servidor Web ──
# Inicia o Flask em paralelo com o bot para receber os pedidos de verificação.
class VexRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        original = self.requestline
        parts = original.split(" ", 2)
        if len(parts) == 3:
            parts[1] = parts[1].split("?", 1)[0]
            self.requestline = " ".join(parts)
        try:
            super().log_request(code, size)
        finally:
            self.requestline = original


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, request_handler=VexRequestHandler)

def keep_alive():
    t = threading.Thread(target=run_web_server)
    t.daemon = True
    t.start()

# ── Autorizações do Discord ──
# Renova tokens expirados e usa as autorizações guardadas para adicionar membros.
oauth_refresh_lock = threading.Lock()


def refresh_access_token(user_id: str, refresh_token: str):
    # Evita renovar duas vezes o mesmo token em pedidos simultâneos.
    with oauth_refresh_lock:
        row = get_user_token(user_id)
        if row:
            if row[3] > time.time() + 30:
                return row[1]
            refresh_token = row[2]
        payload = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        try:
            response = requests.post(
                "https://discord.com/api/oauth2/token", data=payload, timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            save_user_tokens(user_id, data["access_token"], data["refresh_token"], data["expires_in"])
            return data["access_token"]
        except (requests.RequestException, ValueError, KeyError):
            app.logger.warning("Não foi possível renovar a autorização do membro %s.", user_id)
            return None


def add_user_to_guild(access_token: str, guild_id: int, user_id: str, bot_token: str):
    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
    headers = {"Authorization": f"Bot {bot_token}"}
    try:
        response = requests.put(
            url, json={"access_token": access_token}, headers=headers, timeout=15,
        )
        if response.status_code == 429:
            app.logger.warning("Limite temporário do Discord ao adicionar o membro %s.", user_id)
        return response.status_code
    except requests.RequestException:
        app.logger.warning("Falhou a ligação ao Discord ao adicionar o membro %s.", user_id)
        return 0


# ── Configuração do servidor Discord ──
# Define os cargos, as categorias, os emojis e o horário de atendimento.
ID_CARGO_MEMBRO = 1550663372272566382
ID_CARGO_STAFF = 1550663253032566834
ID_CANAL_TICKET_LOGS = 1573283569365950616

# Espera de cinco minutos depois de um ticket ser fechado pela equipa.
TICKET_COOLDOWN_SECONDS = 300

CATEGORIAS_TICKET = {
    "diagnostico": 1572020350902861894,
    "adquirir": 1551424152622080122,
    "duvidas": 1551424186377965618,
    "reotimizar": 1551424281513304164,
}

EMOJIS_TICKET = {
    "adquirir": "<:adquirir:1551427917144260609>",
    "duvidas": "<:duvida:1551427863587069972>",
    "reotimizar": "<:eng:1551427790534877325>",
}

HORARIO_ATENDIMENTO = "📅 Segunda a Domingo das 7h às 00h"
BANNER_PATH = "banner.png"

# ── Canal de voz 24/7 ──
# Define o canal de voz ao qual o bot se liga automaticamente.
ID_CANAL_VOZ_24_7 = 1550659976005947412

# ── Equipa de suporte e mensagens dos tickets ──
# Identifica a equipa e prepara a mensagem inicial de cada tipo de pedido.
def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    cargo_staff = member.guild.get_role(ID_CARGO_STAFF)
    return cargo_staff is not None and cargo_staff in member.roles

def montar_embed_ticket(tipo: str, autor: discord.abc.User) -> discord.Embed:
    emoji = EMOJIS_TICKET.get(tipo, "🎫")
    textos = {
        "diagnostico": (
            "🖥️ Diagnóstico gratuito", discord.Color.blurple(),
            "Partilha aqui o resultado dos testes quando os fizeres.",
        ),
        "adquirir": ("<:adquirir:1551427917144260609> Adquirir Otimização", discord.Color.gold(), "🧑‍💼 **Bem-vindo ao Suporte de Aquisição!**\n\nDescreve aqui o que pretendes adquirir."),
        "duvidas": ("<:duvida:1551427863587069972> Dúvidas - Suporte Geral", discord.Color.blurple(), "🧑‍💼 **Bem-vindo ao Suporte de Dúvidas!**\n\nFica à vontade para perguntar."),
        "reotimizar": ("<:eng:1551427790534877325> Reotimizar", discord.Color.green(), "🧑‍💼 **Bem-vindo ao Suporte de Reotimização!**\n\nDescreve aqui o pedido para refazer a otimização."),
    }
    titulo, cor, descricao = textos.get(tipo, (f"{emoji} Ticket", discord.Color.dark_theme(), ""))
    embed = discord.Embed(title=titulo, description=descricao, color=cor)
    embed.add_field(name="🧑‍💼 Horário de Atendimento", value=HORARIO_ATENDIMENTO, inline=False)
    embed.set_author(name=f"Ticket de {autor.name}", icon_url=autor.display_avatar.url)
    embed.timestamp = discord.utils.utcnow()
    return embed


# ── Ligação ao canal de voz ──
# Liga o bot ao canal configurado quando ainda não está ligado.
voice_connect_lock = asyncio.Lock()


async def entrar_na_chamada_247():
    """Liga o bot ao canal de voz configurado, se ainda não estiver lá."""
    if not ID_CANAL_VOZ_24_7:
        return

    if voice_connect_lock.locked():
        return
    async with voice_connect_lock:
        canal = bot.get_channel(ID_CANAL_VOZ_24_7)
        if not isinstance(canal, discord.VoiceChannel):
            app.logger.warning("O canal de voz 24/7 não foi encontrado ou não é um canal de voz normal.")
            return
        voice = canal.guild.voice_client
        if voice is not None and voice.is_connected():
            return
        try:
            if voice is not None:
                await voice.disconnect(force=True)
            await canal.connect(reconnect=True, self_deaf=True)
            print(f"Entrei na chamada 24/7: {canal.name}")
        except Exception:
            app.logger.exception("Não foi possível ligar ao canal de voz 24/7.")


@tasks.loop(seconds=60)
async def manter_voz_247():
    await entrar_na_chamada_247()


@manter_voz_247.before_loop
async def aguardar_bot_voz():
    await bot.wait_until_ready()


# ── Sistema de tickets ──
# Controla aberturas e fechos simultâneos, identifica o autor e regista os fechos.
ticket_create_lock = asyncio.Lock()
ticket_close_lock = asyncio.Lock()
# Mantém os canais recém-criados disponíveis antes de chegar o evento do Discord.
open_ticket_cache = {}

def ticket_topic(user_id: int, tipo: str) -> str:
    return f"vex-ticket:{user_id}:{tipo}"

def is_ticket_owner(channel: discord.TextChannel, member: discord.Member) -> bool:
    topic = (channel.topic or "").split(":")
    if len(topic) == 3 and topic[0] == "vex-ticket":
        return topic[1] == str(member.id) and topic[2] in CATEGORIAS_TICKET
    # Tickets anteriores à atualização têm uma permissão explícita para o autor.
    if not any(channel.name.endswith(f"-{tipo}") for tipo in CATEGORIAS_TICKET):
        return False
    permissions = channel.overwrites_for(member)
    return permissions.view_channel is True and permissions.send_messages is True

def is_managed_ticket(channel: discord.TextChannel) -> bool:
    parts = (channel.topic or "").split(":")
    if len(parts) == 3 and parts[0] == "vex-ticket" and parts[1].isdigit():
        return parts[2] in CATEGORIAS_TICKET
    return (
        channel.category_id in CATEGORIAS_TICKET.values()
        and any(channel.name.endswith(f"-{tipo}") for tipo in CATEGORIAS_TICKET)
    )


def can_close_ticket(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.channel, discord.TextChannel)
        and is_managed_ticket(interaction.channel)
        and isinstance(interaction.user, discord.Member)
        and is_staff(interaction.user)
    )


def record_ticket_closure(channel: discord.TextChannel, member_id: int, status: str):
    with sqlite3.connect(DB_FILE) as conn:
        previous = conn.execute(
            "SELECT status FROM ticket_closures WHERE channel_id = ?", (str(channel.id),),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO ticket_closures
                (channel_id, guild_id, channel_name, closed_by, closed_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                status=excluded.status, closed_by=excluded.closed_by,
                closed_at=excluded.closed_at
            """,
            (str(channel.id), str(channel.guild.id), channel.name,
             str(member_id), int(time.time()), status),
        )
        if status == "fechado":
            tracked = conn.execute(
                "SELECT user_id FROM open_tickets WHERE channel_id = ?", (str(channel.id),),
            ).fetchone()
            owner_id = tracked[0] if tracked else ticket_owner_id(channel)
            if str(owner_id).isdigit() and (not previous or previous[0] != "fechado"):
                conn.execute(
                    "INSERT INTO ticket_cooldowns(guild_id, user_id, next_allowed_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                    "next_allowed_at = MAX(ticket_cooldowns.next_allowed_at, excluded.next_allowed_at)",
                    (str(channel.guild.id), str(owner_id), time.time() + TICKET_COOLDOWN_SECONDS),
                )
            conn.execute("DELETE FROM open_tickets WHERE channel_id = ?", (str(channel.id),))
    if status == "fechado":
        for key, value in list(open_ticket_cache.items()):
            if value == str(channel.id):
                open_ticket_cache.pop(key, None)


def ticket_next_allowed_at(guild_id: int, user_id: int) -> float:
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT next_allowed_at FROM ticket_cooldowns WHERE guild_id = ? AND user_id = ?",
            (str(guild_id), str(user_id)),
        ).fetchone()
    return row[0] if row else 0


# ── Tratamento de erros das interações ──
# Mostra uma resposta privada em vez de deixar o membro à espera.
async def comunicar_erro(interaction: discord.Interaction, mensagem: str):
    if interaction.is_expired():
        return
    try:
        if interaction.response.is_done():
            await interaction.followup.send(mensagem, ephemeral=True)
        else:
            await interaction.response.send_message(mensagem, ephemeral=True)
    except discord.HTTPException:
        app.logger.warning("Não foi possível enviar o aviso privado da interação %s.", interaction.id)


# ── Proteção contra cliques simultâneos ──
# Processa uma única ação de cada vez por membro, incluindo menus e formulários.
active_member_actions = set()


def one_action_per_member(callback):
    @wraps(callback)
    async def guarded(self, interaction, *args, **kwargs):
        key = (getattr(interaction, "guild_id", None), interaction.user.id)
        if key in active_member_actions:
            await comunicar_erro(
                interaction, "Já estou a tratar do teu pedido. Aguarda antes de clicar novamente.",
            )
            return
        active_member_actions.add(key)
        try:
            return await callback(self, interaction, *args, **kwargs)
        finally:
            active_member_actions.discard(key)
    return guarded


class VexView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        app.logger.error("Falha num botão ou menu.", exc_info=(type(error), error, error.__traceback__))
        await comunicar_erro(interaction, "Ocorreu um erro. Tenta novamente ou contacta a equipa.")


class VexModal(discord.ui.Modal):
    async def on_error(self, interaction, error):
        app.logger.error("Falha num formulário.", exc_info=(type(error), error, error.__traceback__))
        await comunicar_erro(interaction, "Não foi possível concluir o formulário. Tenta novamente.")


# ── Arquivo dos tickets ──
# Guarda as mensagens e cópias dos anexos antes de apagar o canal.
class TicketArchiveError(Exception):
    pass


def ticket_owner_id(channel: discord.TextChannel) -> str:
    parts = (channel.topic or "").split(":")
    if len(parts) == 3 and parts[0] == "vex-ticket" and parts[1].isdigit():
        return parts[1]
    for target, overwrite in channel.overwrites.items():
        if (
            isinstance(target, discord.Member)
            and not target.bot
            and not is_staff(target)
            and overwrite.view_channel is True
            and overwrite.send_messages is True
        ):
            return str(target.id)
    return "não identificado"


def validated_log_channel(guild: discord.Guild) -> discord.TextChannel:
    logs = guild.get_channel(ID_CANAL_TICKET_LOGS)
    if not isinstance(logs, discord.TextChannel):
        raise TicketArchiveError("Não encontrei o canal #ticket-logs configurado.")
    if guild.me is None:
        raise TicketArchiveError("O bot ainda está a preparar a ligação ao servidor.")
    perms = logs.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.attach_files and perms.read_message_history):
        raise TicketArchiveError(
            "O bot precisa de ver, enviar mensagens, anexar ficheiros e ler o histórico em #ticket-logs."
        )
    for role in guild.roles:
        if (
            role.id != ID_CARGO_STAFF
            and not role.is_bot_managed()
            and not role.permissions.administrator
            and logs.permissions_for(role).view_channel
        ):
            raise TicketArchiveError("Torna #ticket-logs privado para a equipa antes de usar os tickets.")
    for target in logs.overwrites:
        if isinstance(target, discord.Member) and not target.bot and not is_staff(target):
            if logs.permissions_for(target).view_channel:
                raise TicketArchiveError("Há membros fora da equipa com acesso a #ticket-logs.")
    return logs


def validate_ticket_creation(guild: discord.Guild, tipo: str):
    category_id = CATEGORIAS_TICKET.get(tipo)
    category = guild.get_channel(category_id) if category_id else None
    if not isinstance(category, discord.CategoryChannel):
        raise TicketArchiveError("A categoria deste tipo de ticket está indisponível. Contacta a equipa.")
    staff_role = guild.get_role(ID_CARGO_STAFF)
    if staff_role is None or guild.me is None:
        raise TicketArchiveError("A configuração da equipa de suporte está incompleta. Contacta a equipa.")
    if not category.permissions_for(guild.me).manage_channels:
        raise TicketArchiveError("O bot não tem permissão para gerir canais nesta categoria. Contacta a equipa.")
    validated_log_channel(guild)
    return category, staff_role


def file_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def verify_saved_file(logs: discord.TextChannel, sent: discord.Message, local_path: Path):
    # Lê a mensagem enviada, volta a descarregar o anexo e compara o conteúdo.
    stored = await logs.fetch_message(sent.id)
    expected_size = local_path.stat().st_size
    attachment = next(
        (item for item in stored.attachments if item.size == expected_size), None,
    )
    if attachment is None:
        raise TicketArchiveError("Não consegui confirmar o ficheiro enviado para #ticket-logs.")
    verification = local_path.with_name(local_path.name + ".verificar")
    try:
        await attachment.save(verification)
        expected, actual = await asyncio.gather(
            asyncio.to_thread(file_sha256, local_path),
            asyncio.to_thread(file_sha256, verification),
        )
        if expected != actual:
            raise TicketArchiveError("O ficheiro consultado em #ticket-logs não corresponde ao original.")
    finally:
        verification.unlink(missing_ok=True)


async def guardar_conversa(channel: discord.TextChannel, fechado_por: discord.Member) -> str:
    guild = channel.guild
    logs = validated_log_channel(guild)
    if logs.id == channel.id:
        raise TicketArchiveError("O canal de registos não pode ser o próprio ticket.")
    source_perms = channel.permissions_for(guild.me)
    if not (source_perms.view_channel and source_perms.read_message_history):
        raise TicketArchiveError("O bot precisa de ver o ticket e ler o histórico das mensagens.")

    owner_id = ticket_owner_id(channel)
    assignment = get_ticket_assignment(channel.id)
    assigned_staff = assignment[0] if assignment else "não atribuído"
    stamp = discord.utils.utcnow()
    # A marca delimita o fim desta cópia, incluindo todas as mensagens anteriores.
    marker = await channel.send(
        "📄 A guardar a conversa e os anexos antes de fechar este ticket…",
        allowed_mentions=discord.AllowedMentions.none(),
    )
    summary = (
        f"📄 Ticket #{channel.name} | ID {channel.id}\n"
        f"Membro (ID): {owner_id}\n"
        f"Responsável (ID): {assigned_staff}\n"
        f"Fecho pedido por: {fechado_por} | ID {fechado_por.id}\n"
        f"Estado: A ARQUIVAR\n"
        f"Cópia até à mensagem {marker.id}, em {stamp.isoformat()}."
    )
    header = await logs.send(summary, allowed_mentions=discord.AllowedMentions.none())
    message_count = 0
    attachments = []
    try:
        with tempfile.TemporaryDirectory(prefix="vex-ticket-") as folder:
            transcript = Path(folder) / f"ticket-{channel.id}.txt"
            with transcript.open("w", encoding="utf-8") as stream:
                stream.write(
                    f"VEX — REGISTO DE TICKET\nServidor: {guild.name} ({guild.id})\n"
                    f"Canal: {channel.name} ({channel.id})\nMembro (ID): {owner_id}\n"
                    f"Responsável (ID): {assigned_staff}\n"
                    f"Fecho pedido por: {fechado_por} ({fechado_por.id})\n"
                    f"Data UTC: {stamp.isoformat()}\n"
                    f"Última mensagem incluída: {marker.id}\n\n"
                )
                async for message in channel.history(
                    limit=None, oldest_first=True, before=discord.Object(id=marker.id + 1),
                ):
                    message_count += 1
                    stream.write(
                        f"[{message.created_at.isoformat()}] {message.author} "
                        f"(ID {message.author.id}) — mensagem {message.id}\n"
                    )
                    if message.edited_at:
                        stream.write(f"Editada em: {message.edited_at.isoformat()}\n")
                    if message.reference and message.reference.message_id:
                        stream.write(f"Resposta à mensagem: {message.reference.message_id}\n")
                    stream.write(message.content + "\n")
                    for embed in message.embeds:
                        stream.write("EMBED: " + json.dumps(embed.to_dict(), ensure_ascii=False) + "\n")
                    for attachment in message.attachments:
                        stream.write(
                            f"ANEXO: {attachment.filename} | {attachment.size} bytes | "
                            f"ID {attachment.id}\n{attachment.url}\n"
                        )
                        attachments.append((message.id, attachment))
                    for sticker in message.stickers:
                        stream.write(f"STICKER: {sticker.name} | {sticker.url}\n")
                    stream.write("\n")

            # Divide históricos longos em partes UTF-8 válidas dentro do limite de envio.
            limit = min(guild.filesize_limit, 8 * 1024 * 1024)
            with transcript.open(encoding="utf-8") as stream:
                part = 0
                while text := stream.read(max(1, (limit - 1024) // 4)):
                    part += 1
                    part_path = Path(folder) / f"ticket-{channel.id}-parte-{part}.txt"
                    part_path.write_text(text, encoding="utf-8")
                    with closing(discord.File(part_path, filename=part_path.name)) as upload:
                        sent = await logs.send(
                            content=f"Ticket {channel.id} | conversa, parte {part}",
                            file=upload, allowed_mentions=discord.AllowedMentions.none(),
                        )
                    await verify_saved_file(logs, sent, part_path)

            # Mantém cópias no canal de registos para sobreviver à eliminação do ticket.
            for message_id, attachment in attachments:
                if attachment.size > guild.filesize_limit:
                    raise TicketArchiveError(
                        f"O anexo {attachment.id} ultrapassa o limite atual de envio do Discord. "
                        "O ticket fica aberto para resolver o arquivo desse ficheiro."
                    )
                local_file = Path(folder) / f"anexo-{attachment.id}.bin"
                try:
                    await attachment.save(local_file)
                except discord.HTTPException:
                    await attachment.save(local_file, use_cached=True)
                filename = f"{attachment.id}-{Path(attachment.filename).name}"[:240]
                with closing(discord.File(local_file, filename=filename)) as upload:
                    sent = await logs.send(
                        content=f"Ticket {channel.id} | mensagem {message_id} | anexo {attachment.id}",
                        file=upload, allowed_mentions=discord.AllowedMentions.none(),
                    )
                await verify_saved_file(logs, sent, local_file)
                local_file.unlink()

        await header.edit(
            content=summary.replace("A ARQUIVAR", "ARQUIVADO — FECHO PENDENTE")
            + f"\nMensagens: {message_count} | Anexos copiados: {len(attachments)}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "INSERT INTO ticket_archives(channel_id, log_url, archived_at, closed_by) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(channel_id) DO UPDATE SET "
                "log_url=excluded.log_url, archived_at=excluded.archived_at, closed_by=excluded.closed_by",
                (str(channel.id), header.jump_url, int(time.time()), str(fechado_por.id)),
            )
        return header.jump_url
    except Exception:
        try:
            await header.edit(
                content=summary.replace("A ARQUIVAR", "INCOMPLETO — TICKET MANTIDO ABERTO"),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            app.logger.warning("Não foi possível atualizar o estado do arquivo %s.", header.id)
        raise


# ── Confirmação de fecho ──
# Confirma a autorização, arquiva a conversa e só depois apaga o canal.
class ConfirmarFechoView(VexView):
    def __init__(self, requester_id: int):
        super().__init__(timeout=60)
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id and can_close_ticket(interaction):
            return True
        await interaction.response.send_message("Só o away e a equipa podem fechar tickets.", ephemeral=True)
        return False

    @discord.ui.button(label="Confirmar fecho", style=discord.ButtonStyle.danger)
    @one_action_per_member
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        await interaction.edit_original_response(
            content="A guardar a conversa e os anexos antes de fechar…", view=None,
        )
        channel = interaction.channel
        async with ticket_close_lock:
            if channel.guild.get_channel(channel.id) is None:
                return
            if not can_close_ticket(interaction):
                await interaction.edit_original_response(content="Já não tens permissão para fechar este ticket.")
                return
            try:
                record_ticket_closure(channel, interaction.user.id, "pendente")
                log_url = await guardar_conversa(channel, interaction.user)
            except Exception as error:
                app.logger.exception("Não foi possível arquivar o ticket %s.", channel.id)
                try:
                    record_ticket_closure(channel, interaction.user.id, "arquivo_falhou")
                except sqlite3.Error:
                    app.logger.exception("Não foi possível atualizar o histórico do ticket.")
                reason = str(error) if isinstance(error, TicketArchiveError) else "Falhou o envio do registo ou de um anexo."
                await comunicar_erro(
                    interaction, f"{reason} O ticket continua aberto. Contacta a equipa ou tenta fechar novamente.",
                )
                return
            try:
                await channel.delete(reason=f"Ticket arquivado e fechado por {interaction.user.id}")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                record_ticket_closure(channel, interaction.user.id, "fecho_falhou")
                await comunicar_erro(interaction, "A conversa foi guardada, mas não consegui apagar o canal. Podes tentar novamente.")
                return
            try:
                record_ticket_closure(channel, interaction.user.id, "fechado")
            except sqlite3.Error:
                app.logger.exception("Ticket %s apagado após arquivo, mas falhou a atualização do histórico.", channel.id)
            try:
                message_id = int(log_url.rsplit("/", 1)[1])
                log_message = await channel.guild.get_channel(ID_CANAL_TICKET_LOGS).fetch_message(message_id)
                await log_message.edit(
                    content=log_message.content.replace("ARQUIVADO — FECHO PENDENTE", "ARQUIVADO — TICKET FECHADO"),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.HTTPException, AttributeError):
                app.logger.warning("O ticket %s foi fechado; o registo continua com fecho pendente.", channel.id)
            await comunicar_erro(interaction, "A conversa foi guardada no registo privado da equipa. Ticket fechado.")

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    @one_action_per_member
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Fecho cancelado.", view=None)
        self.stop()


# ── Atribuição do atendimento ──
# Uma reserva atómica impede que dois membros da equipa assumam o mesmo ticket.
# A base de dados mantém o responsável após reiniciar o bot.
def get_ticket_assignment(channel_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        return conn.execute(
            "SELECT staff_id, claimed_at, panel_message_id FROM ticket_assignments WHERE channel_id = ?",
            (str(channel_id),),
        ).fetchone()


def reserve_ticket_assignment(channel_id: int, guild_id: int, staff_id: int, message_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        closed = conn.execute(
            "SELECT status FROM ticket_closures WHERE channel_id = ?", (str(channel_id),),
        ).fetchone()
        if closed and closed[0] == "fechado":
            return None
        conn.execute(
            "INSERT OR IGNORE INTO ticket_assignments "
            "(channel_id, guild_id, staff_id, claimed_at, panel_message_id) VALUES (?, ?, ?, ?, ?)",
            (str(channel_id), str(guild_id), str(staff_id), int(time.time()), str(message_id)),
        )
        row = conn.execute(
            "SELECT staff_id, claimed_at, panel_message_id FROM ticket_assignments WHERE channel_id = ?",
            (str(channel_id),),
        ).fetchone()
    return row


def get_active_ticket_assignments():
    with sqlite3.connect(DB_FILE) as conn:
        return conn.execute(
            "SELECT a.channel_id, a.guild_id, a.staff_id, a.claimed_at, a.panel_message_id "
            "FROM ticket_assignments a LEFT JOIN ticket_closures c ON a.channel_id = c.channel_id "
            "WHERE c.status IS NULL OR c.status != 'fechado'"
        ).fetchall()


async def update_ticket_assignment_message(message: discord.Message, staff_id: str):
    embeds = [discord.Embed.from_dict(embed.to_dict()) for embed in message.embeds]
    if not embeds:
        embeds = [discord.Embed(title="Atendimento Vex", color=discord.Color.blurple())]
    embed = embeds[0]
    for index in range(len(embed.fields) - 1, -1, -1):
        if embed.fields[index].name == "Atendimento":
            embed.remove_field(index)
    embed.add_field(
        name="Atendimento",
        value=f"✅ O <@{staff_id}> está a acompanhar o teu pedido.",
        inline=False,
    )
    await message.edit(
        embeds=embeds,
        view=TicketOpcoesView(assigned=True),
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def restore_ticket_assignments():
    # Repõe a informação se um reinício interrompeu a atualização da mensagem.
    for channel_id, guild_id, staff_id, claimed_at, message_id in get_active_ticket_assignments():
        async with ticket_close_lock:
            guild = bot.get_guild(int(guild_id))
            channel = guild.get_channel(int(channel_id)) if guild else None
            if not isinstance(channel, discord.TextChannel) or not is_managed_ticket(channel):
                continue
            try:
                message = await channel.fetch_message(int(message_id))
                await update_ticket_assignment_message(message, staff_id)
            except discord.HTTPException:
                app.logger.warning("Não foi possível repor o atendimento do ticket %s.", channel_id)


class TicketOpcoesView(VexView):
    def __init__(self, assigned: bool = False):
        super().__init__(timeout=None)
        self.assumir.disabled = assigned
        self.assumir.label = "Atendimento assumido" if assigned else "Assumir atendimento"

    @discord.ui.button(
        label="Assumir atendimento",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="ticket_assumir_btn",
    )
    @one_action_per_member
    async def assumir(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        member = interaction.user
        if (
            not isinstance(member, discord.Member)
            or not is_staff(member)
            or not isinstance(channel, discord.TextChannel)
            or not is_managed_ticket(channel)
        ):
            await interaction.response.send_message(
                "Só a equipa pode assumir o atendimento de um ticket.", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        # O mesmo bloqueio do fecho evita atribuições durante o arquivo da conversa.
        async with ticket_close_lock:
            if channel.guild.get_channel(channel.id) is None:
                await comunicar_erro(interaction, "Este ticket já foi fechado.")
                return
            if not is_staff(member):
                await comunicar_erro(interaction, "Já não tens permissão para assumir o atendimento.")
                return
            if interaction.message is None:
                await comunicar_erro(interaction, "Não encontrei a mensagem inicial deste ticket.")
                return
            assignment = reserve_ticket_assignment(
                channel.id, channel.guild.id, member.id, interaction.message.id,
            )
            if assignment is None:
                await comunicar_erro(interaction, "Este ticket já foi fechado.")
                return
            staff_id, claimed_at, message_id = assignment
            if staff_id != str(member.id):
                await interaction.followup.send(
                    f"Este ticket já está a ser acompanhado por <@{staff_id}>.",
                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            try:
                await update_ticket_assignment_message(interaction.message, staff_id)
            except discord.HTTPException:
                app.logger.exception("Atendimento reservado, mas a mensagem do ticket %s não foi atualizada.", channel.id)
                await comunicar_erro(
                    interaction,
                    "O ticket ficou atribuído a ti, mas não consegui atualizar a mensagem. "
                    "Podes clicar novamente para tentar atualizar.",
                )
                return
        await interaction.followup.send("Assumiste o atendimento deste ticket.", ephemeral=True)

    @discord.ui.button(
        label="Fechar ticket",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_finalizar_btn",
    )
    @one_action_per_member
    async def finalizar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not can_close_ticket(interaction):
            await interaction.response.send_message("Só o away e a equipa podem fechar tickets.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Queres fechar este ticket? A conversa e os anexos serão guardados no registo privado da equipa antes de o canal ser apagado.",
            view=ConfirmarFechoView(interaction.user.id), ephemeral=True,
        )


# ── Um ticket aberto por membro ──
# O limite conta todos os tipos e continua válido após reiniciar o bot.
def remember_open_ticket(guild_id: int, user_id: int, channel_id: int):
    key = (str(guild_id), str(user_id))
    open_ticket_cache[key] = str(channel_id)
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO open_tickets(guild_id, user_id, channel_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET channel_id = excluded.channel_id",
            (str(guild_id), str(user_id), str(channel_id)),
        )


def forget_open_ticket(channel_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM open_tickets WHERE channel_id = ?", (str(channel_id),))
    for key, value in list(open_ticket_cache.items()):
        if value == str(channel_id):
            open_ticket_cache.pop(key, None)


async def find_open_ticket(guild: discord.Guild, member: discord.Member):
    key = (str(guild.id), str(member.id))
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT channel_id FROM open_tickets WHERE guild_id = ? AND user_id = ?", key,
        ).fetchone()
        closed = {
            item[0] for item in conn.execute(
                "SELECT channel_id FROM ticket_closures WHERE guild_id = ? AND status = 'fechado'",
                (str(guild.id),),
            )
        }
    tracked_id = row[0] if row else open_ticket_cache.get(key)
    if tracked_id:
        if tracked_id not in closed:
            try:
                existing = await guild.fetch_channel(int(tracked_id))
            except discord.NotFound:
                existing = None
            # Perder acesso ao canal é um erro, não autorização para abrir outro.
            if isinstance(existing, discord.TextChannel):
                return str(existing.id)
        forget_open_ticket(int(tracked_id))

    # Reconhece também tickets criados por versões anteriores do bot.
    for channel in guild.text_channels:
        if (
            str(channel.id) not in closed
            and is_managed_ticket(channel)
            and is_ticket_owner(channel, member)
        ):
            try:
                existing = await guild.fetch_channel(channel.id)
            except discord.NotFound:
                continue
            if isinstance(existing, discord.TextChannel):
                remember_open_ticket(guild.id, member.id, existing.id)
                return str(existing.id)
    return None


# ── Abertura de tickets privados ──
async def abrir_ticket(interaction: discord.Interaction, tipo: str, respostas=()):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("Abre o ticket dentro do servidor.", ephemeral=True)
        return
    topic = ticket_topic(interaction.user.id, tipo)
    async with ticket_create_lock:
        canal = None
        try:
            existing_id = await find_open_ticket(guild, interaction.user)
            if existing_id:
                await interaction.followup.send(
                    f"Já tens um ticket aberto: <#{existing_id}>. "
                    "Continua o teu pedido nesse canal. Só podes ter um ticket aberto de cada vez.",
                    ephemeral=True,
                )
                return
            next_allowed = ticket_next_allowed_at(guild.id, interaction.user.id)
            if next_allowed > time.time():
                await interaction.followup.send(
                    f"O teu último ticket foi fechado há pouco. "
                    f"Podes abrir um novo ticket <t:{int(next_allowed)}:R>.",
                    ephemeral=True,
                )
                return
            try:
                categoria, cargo_staff = validate_ticket_creation(guild, tipo)
            except TicketArchiveError as error:
                app.logger.warning("Configuração inválida ao abrir ticket: %s", error)
                await interaction.followup.send(str(error), ephemeral=True)
                return
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                ),
                cargo_staff: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                ),
            }
            canal = await guild.create_text_channel(
                f"{interaction.user.name}-{tipo}".lower(),
                category=categoria, overwrites=overwrites, topic=topic,
            )
            remember_open_ticket(guild.id, interaction.user.id, canal.id)
            embed = montar_embed_ticket(tipo, interaction.user)
            for label, value in respostas:
                embed.add_field(name=label, value=discord.utils.escape_markdown(value), inline=False)
            await canal.send(
                content=f"{interaction.user.mention} | {cargo_staff.mention}",
                embed=embed, view=TicketOpcoesView(),
            )
        except (discord.HTTPException, sqlite3.Error):
            app.logger.exception("Falhou a abertura do ticket do membro %s.", interaction.user.id)
            if canal is not None:
                try:
                    await canal.delete(reason="Não foi possível concluir a abertura do ticket")
                except discord.NotFound:
                    forget_open_ticket(canal.id)
                except discord.HTTPException:
                    app.logger.exception("Não foi possível limpar o ticket incompleto %s.", canal.id)
                else:
                    forget_open_ticket(canal.id)
            await interaction.followup.send(
                "Não foi possível abrir o ticket. Tenta novamente ou contacta a equipa.", ephemeral=True,
            )
            return
    await interaction.followup.send(f"O teu ticket foi criado aqui: {canal.mention}", ephemeral=True)


# ── Formulários e painel de tickets ──
# Recolhe dados para otimização e reotimização. Os tickets de dúvidas abrem diretamente.
class FormularioTicket(VexModal):
    def __init__(self, tipo: str):
        titulo = "Adquirir Otimização" if tipo == "adquirir" else "Reotimizar"
        super().__init__(title=titulo)
        self.tipo = tipo
        campos = (
            [("Processador", 80), ("Placa gráfica", 80), ("RAM", 40),
             ("Jogo principal", 80), ("Problema ou objetivo", 400)]
            if tipo == "adquirir"
            else [("O que mudou no PC ou no jogo?", 400), ("Quando voltou o problema?", 250)]
        )
        self.perguntas = []
        for label, limit in campos:
            field = discord.ui.TextInput(
                label=label, max_length=limit,
                style=discord.TextStyle.paragraph if limit > 80 else discord.TextStyle.short,
            )
            self.add_item(field)
            self.perguntas.append((label, field))

    @one_action_per_member
    async def on_submit(self, interaction: discord.Interaction):
        respostas = [(label, field.value.strip() or "Sem resposta") for label, field in self.perguntas]
        await abrir_ticket(interaction, self.tipo, respostas)


# ── Diagnóstico gratuito ──
# Mostra sugestões privadas e só cria um ticket a pedido do membro.
DIAGNOSTICOS = {
    "fps": (
        "FPS sempre baixos",
        "Ativa o contador de FPS. Baixa a resolução e repete o teste no mesmo "
        "sítio do jogo. Diz-nos quantos FPS tiveste antes e depois.",
    ),
    "quebras": (
        "Quebras ou soluços",
        "Vê se os FPS descem no momento do soluço. Se tiveres gravação automática "
        "de clipes ativa, testa uma partida com ela desligada e compara.",
    ),
    "ping": (
        "Ping alto ou lag online",
        "Vê se o ping sobe quando sentes o atraso. Experimenta outro jogo online "
        "e indica se também acontece lá.",
    ),
    "fecho": (
        "O jogo fecha sozinho",
        "Indica se fecha ao abrir, ao carregar uma partida ou durante o jogo. "
        "Se aparecer uma mensagem de erro, guarda o texto ou tira uma captura.",
    ),
    "outro": (
        "Outro problema",
        "Diz-nos quando começou, como podemos reproduzir o problema e se mudaste "
        "alguma definição, driver ou componente entretanto.",
    ),
}


class AbrirDiagnosticoView(VexView):
    def __init__(self, respostas):
        super().__init__(timeout=900)
        self.respostas = respostas

    @discord.ui.button(
        label="Abrir ticket de diagnóstico",
        emoji="🎫",
        style=discord.ButtonStyle.primary,
    )
    @one_action_per_member
    async def abrir(self, interaction: discord.Interaction, button: discord.ui.Button):
        await abrir_ticket(interaction, "diagnostico", self.respostas)


class FormularioDiagnostico(VexModal):
    jogo = discord.ui.TextInput(label="Jogo", max_length=80)
    grafica = discord.ui.TextInput(label="Placa gráfica", max_length=80)
    processador = discord.ui.TextInput(label="Processador", max_length=80)
    problema = discord.ui.TextInput(
        label="O que está a acontecer?",
        style=discord.TextStyle.paragraph,
        max_length=400,
    )

    def __init__(self, tipo):
        super().__init__(title="Diagnóstico gratuito")
        self.tipo = tipo

    @one_action_per_member
    async def on_submit(self, interaction: discord.Interaction):
        titulo, sugestao = DIAGNOSTICOS[self.tipo]
        respostas = [
            ("Tipo de problema", titulo),
            ("Jogo", self.jogo.value),
            ("Placa gráfica", self.grafica.value),
            ("Processador", self.processador.value),
            ("Descrição", self.problema.value),
        ]
        await interaction.response.send_message(
            f"**🖥️ Diagnóstico gratuito — {titulo}**\n\n"
            f"{sugestao}\n\n"
            "**🕒 Tempo de resposta**\n"
            "O diagnóstico inicial é gratuito. Damos prioridade aos pedidos de "
            "quem adquiriu um serviço de otimização, por isso a resposta da equipa "
            "ao teu ticket pode demorar um pouco mais. Assim que tivermos "
            "disponibilidade, damos seguimento ao teu pedido. "
            "Obrigado pela compreensão!",
            view=AbrirDiagnosticoView(respostas),
            ephemeral=True,
        )


class EscolherProblemaSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Escolhe o tipo de problema",
            options=[
                discord.SelectOption(label=titulo, value=tipo)
                for tipo, (titulo, _) in DIAGNOSTICOS.items()
            ],
        )

    @one_action_per_member
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(FormularioDiagnostico(self.values[0]))


class EscolherProblemaView(VexView):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(EscolherProblemaSelect())


class PainelTicketsSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Adquirir Otimização",
                description="Quero comprar uma otimização para o meu PC",
                value="adquirir",
                emoji=EMOJIS_TICKET["adquirir"],
            ),
            discord.SelectOption(
                label="Dúvidas",
                description="Perguntas sobre serviços, preços e marcações",
                value="duvidas",
                emoji=EMOJIS_TICKET["duvidas"],
            ),
            discord.SelectOption(
                label="Reotimizar",
                description="Já sou cliente e quero uma nova otimização",
                value="reotimizar",
                emoji=EMOJIS_TICKET["reotimizar"],
            ),
            discord.SelectOption(
                label="Diagnóstico gratuito",
                description="Quero ajuda inicial com um problema no PC",
                value="diagnostico",
                emoji="🖥️",
            ),
        ]
        super().__init__(
            placeholder="Escolhe o tipo de atendimento",
            options=options,
            custom_id="painel_tickets_select",
        )

    @one_action_per_member
    async def callback(self, interaction: discord.Interaction):
        tipo = self.values[0]
        if tipo == "diagnostico":
            await interaction.response.send_message(
                "Escolhe o problema que melhor descreve o que está a acontecer.",
                view=EscolherProblemaView(),
                ephemeral=True,
            )
        elif tipo == "duvidas":
            await abrir_ticket(interaction, tipo)
        else:
            await interaction.response.send_modal(FormularioTicket(tipo))

class PainelTicketsView(VexView):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PainelTicketsSelect())

# ── Botão de verificação ──
# Abre a página Web onde o membro inicia a autorização da conta Discord.
class VerifyAuthView(VexView):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Verificar conta", url=get_verification_url(), style=discord.ButtonStyle.link, emoji="✅"))

# ── Inicialização e eventos do bot ──
# Regista os comandos e mantém os botões dos tickets disponíveis após reiniciar.
class MeuBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(PainelTicketsView())
        self.add_view(TicketOpcoesView())
        for channel_id, guild_id, staff_id, claimed_at, message_id in get_active_ticket_assignments():
            self.add_view(TicketOpcoesView(assigned=True), message_id=int(message_id))
        await self.tree.sync()
        if not manter_voz_247.is_running():
            manter_voz_247.start()

bot = MeuBot()


@bot.tree.error
async def tratar_erro_comando(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await comunicar_erro(interaction, "Este comando é reservado à administração.")
        return
    if isinstance(error, app_commands.NoPrivateMessage):
        await comunicar_erro(interaction, "Usa este comando dentro do servidor.")
        return
    original = getattr(error, "original", error)
    app.logger.error(
        "Falha no comando %s.", getattr(interaction.command, "name", "?"),
        exc_info=(type(original), original, original.__traceback__),
    )
    await comunicar_erro(interaction, "Não foi possível concluir o comando. Consulta a consola ou contacta a equipa.")


@bot.event
async def on_ready():
    print(f'O bot {bot.user} arrancou e está pronto a usar!')
    await entrar_na_chamada_247()
    await restore_ticket_assignments()


@bot.event
async def on_voice_state_update(member, before, after):
    # Se o próprio bot saiu da chamada 24/7 sem ser por um comando nosso
    # (alguém expulsou-o, falha de rede, etc.), tenta voltar a entrar sozinho.
    if member.id == bot.user.id and before.channel is not None and after.channel is None:
        await asyncio.sleep(5)
        await entrar_na_chamada_247()


# ── Comandos do Discord ──
# Publica painéis e avisos e dá à equipa acesso ao histórico de fechos.
@bot.tree.command(name="setup_tickets", description="Cria o painel de tickets")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def setup_tickets(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    embed_painel = discord.Embed(
        title="<:Vex:1550695876769489088> Vex Otimização — Tickets",
        description=(
            "Escolhe abaixo a opção que corresponde ao teu pedido.\n\n"
            "```ansi\n"
            "\u001b[1;36mC:\\VEX> PAGAMENTOS E CONTACTO\u001b[0m\n\n"
            "\u001b[1;36m[DÚVIDAS E TERMOS]\u001b[0m\n"
            "Consulta o canal \u001b[1;33m#termos\u001b[0m.\n\n"
            "\u001b[1;36m[PAGAMENTOS]\u001b[0m\n"
            "Abre um \u001b[1;33mticket\u001b[0m para tratar do pagamento.\n\n"
            "\u001b[1;36m[MÉTODOS DISPONÍVEIS]\u001b[0m\n"
            "\u001b[1;32mPayPal\u001b[0m — Internacional.\n"
            "\u001b[1;32mMB WAY\u001b[0m — Portugal.\n\n"
            "\u001b[1;36m[PROBLEMAS COM O TICKET?]\u001b[0m\n"
            "Envia mensagem privada ao \u001b[1;33m@away\u001b[0m.\n\n"
            "\u001b[1;33mC:\\VEX> Confirma os dados antes de pagar.\u001b[0m\n"
            "```"
        ),
        color=discord.Color.dark_theme(),
    )
    if os.path.isfile(BANNER_PATH):
        ficheiro_banner = discord.File(BANNER_PATH, filename="banner.png")
        embed_painel.set_image(url="attachment://banner.png")
        await interaction.channel.send(embed=embed_painel, file=ficheiro_banner, view=PainelTicketsView())
    else:
        await interaction.channel.send(embed=embed_painel, view=PainelTicketsView())
    await interaction.followup.send("Painel criado!", ephemeral=True)

@bot.tree.command(name="aviso", description="Envia uma mensagem para o canal atual usando o bot")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(mensagem='Mensagem a publicar no canal atual')
async def aviso(interaction: discord.Interaction, mensagem: app_commands.Range[str, 1, 2000]):
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.channel.send(mensagem)
    await interaction.followup.send("Aviso enviado com sucesso!", ephemeral=True)

@bot.tree.command(name="setup_verify", description="Cria a mensagem de verificação (OAuth2)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def setup_verify(interaction: discord.Interaction):
    if not CLIENT_ID or not CLIENT_SECRET or not get_verification_url():
        await interaction.response.send_message(
            "A configuração OAuth2 está incompleta.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    embed = discord.Embed(
        title="🔒 Verificação de segurança",
        description="Clica no botão abaixo para verificares a tua conta e teres acesso ao servidor.",
        color=discord.Color.green(),
    )
    await interaction.channel.send(embed=embed, view=VerifyAuthView())
    await interaction.followup.send("Painel de verificação enviado!", ephemeral=True)

# ── Gestão de membros autorizados ──
# Adiciona membros com autorização guardada e mostra as estatísticas de verificação.
@bot.tree.command(name="pull_all", description="Adiciona os membros com autorização guardada")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def pull_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    tokens = get_all_tokens()
    if not tokens:
        await interaction.followup.send("Nenhum utilizador na base de dados.", ephemeral=True)
        return

    sucessos, ja_no_servidor, falhas = 0, 0, 0
    bot_token = os.getenv("DISCORD_TOKEN")
    guild_id = interaction.guild_id

    prazo = time.monotonic() + 12 * 60
    por_processar = 0
    for index, (user_id, access_token, refresh_token, expires_at) in enumerate(tokens):
        if time.monotonic() >= prazo:
            por_processar = len(tokens) - index
            break
        if time.time() >= expires_at:
            access_token = await asyncio.to_thread(refresh_access_token, user_id, refresh_token)
            if not access_token:
                falhas += 1
                continue
        status = await asyncio.to_thread(add_user_to_guild, access_token, guild_id, user_id, bot_token)
        if status in (201, 200): sucessos += 1
        elif status == 204: ja_no_servidor += 1
        else: falhas += 1
        await asyncio.sleep(1)

    resultado = f"📊 **Resultado:** Sucessos: {sucessos} | Já no servidor: {ja_no_servidor} | Falhas: {falhas}"
    if por_processar:
        resultado += f"\nPor processar: {por_processar}. Volta a executar o comando para continuar."
    await interaction.followup.send(resultado, ephemeral=True)

@bot.tree.command(name="pull_user", description="Adiciona um utilizador autorizado pelo respetivo ID")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def pull_user(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer(ephemeral=True)
    row = get_user_token(user_id)
    if not row:
        await interaction.followup.send("⚠️ Este utilizador não se encontra na base de dados.", ephemeral=True)
        return

    _, access_token, refresh_token, expires_at = row
    if time.time() >= expires_at:
        access_token = await asyncio.to_thread(refresh_access_token, user_id, refresh_token)
        if not access_token:
            await interaction.followup.send("❌ O token expirou e não foi possível renová-lo.", ephemeral=True)
            return

    bot_token = os.getenv("DISCORD_TOKEN")
    status = await asyncio.to_thread(add_user_to_guild, access_token, interaction.guild_id, user_id, bot_token)

    if status in (201, 200):
        await interaction.followup.send(f"✅ Utilizador `{user_id}` adicionado com sucesso!", ephemeral=True)
    elif status == 204:
        await interaction.followup.send(f"ℹ️ O utilizador `{user_id}` já está neste servidor.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Falha ao adicionar (Código API: {status}).", ephemeral=True)

@bot.tree.command(name="auth_stats", description="Mostra o número total de utilizadores autorizados na base de dados")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def auth_stats(interaction: discord.Interaction):
    tokens = get_all_tokens()
    await interaction.response.send_message(
        f"📈 **Estatísticas de Autorização:**\nTotal de membros autorizados na base de dados: `{len(tokens)}`", 
        ephemeral=True
    )

@bot.tree.command(name="ticket_historico", description="Consulta os últimos fechos de tickets")
@app_commands.guild_only()
async def ticket_historico(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("Este comando é reservado à equipa.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT c.channel_name, c.closed_by, c.closed_at, c.status, a.log_url "
            "FROM ticket_closures c LEFT JOIN ticket_archives a ON c.channel_id = a.channel_id "
            "WHERE c.guild_id = ? ORDER BY c.closed_at DESC LIMIT 10",
            (str(interaction.guild_id),),
        ).fetchall()
    lines = [
        f"**{discord.utils.escape_markdown(name[:60])}** · <@{member_id}>\n"
        f"<t:{closed_at}:f> · {status}"
        + (f" · [Conversa]({log_url})" if log_url else "")
        for name, member_id, closed_at, status, log_url in rows
    ]
    embed = discord.Embed(
        title="Histórico de tickets",
        description="\n\n".join(lines) if lines else "Ainda não há registos de fecho.",
        color=discord.Color.blurple(),
    )
    await interaction.followup.send(
        embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


# ── Arranque da aplicação ──
# Inicia o servidor Web e liga o bot ao Discord com o token configurado.
if __name__ == '__main__':
    keep_alive()
    TOKEN = os.getenv('DISCORD_TOKEN')
    if TOKEN:
        bot.run(TOKEN)
