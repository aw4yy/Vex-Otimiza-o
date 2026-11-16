import os
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
from discord.ext import commands
from discord import app_commands
from flask import Flask, request, redirect, session

# ── Servidor Web ──
# Cria a aplicação Flask que recebe os pedidos de verificação.
app = Flask(__name__)

# ── Base de dados SQLite ──
# Define onde fica o auth_tokens.db e guarda as autorizações e os fechos de tickets.
DATA_PATH = os.getenv("DATA_PATH")
if DATA_PATH and os.path.exists(DATA_PATH):
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
def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_web_server)
    t.daemon = True
    t.start()

# ── Autorizações do Discord ──
# Renova tokens expirados e usa as autorizações guardadas para adicionar membros.
def refresh_access_token(user_id: str, refresh_token: str):
    token_url = "https://discord.com/api/oauth2/token"
    payload = {'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET, 'grant_type': 'refresh_token', 'refresh_token': refresh_token}
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    response = requests.post(token_url, data=payload, headers=headers)
    if response.status_code == 200:
        data = response.json()
        save_user_tokens(user_id, data['access_token'], data['refresh_token'], data['expires_in'])
        return data['access_token']
    return None

def add_user_to_guild(access_token: str, guild_id: int, user_id: str, bot_token: str):
    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    response = requests.put(url, json={"access_token": access_token}, headers=headers)
    return response.status_code

# ── Configuração do servidor Discord ──
# Define os cargos, as categorias, os emojis e o horário de atendimento.
ID_CARGO_MEMBRO = 1550663372272566382
ID_CARGO_STAFF = 1550663253032566834

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
async def entrar_na_chamada_247():
    """Liga o bot ao canal de voz configurado, se ainda não estiver lá."""
    if not ID_CANAL_VOZ_24_7:
        return

    canal = bot.get_channel(ID_CANAL_VOZ_24_7)
    if canal is None:
        print("AVISO: não encontrei o canal de voz configurado em ID_CANAL_VOZ_24_7.")
        return

    if canal.guild.voice_client is not None:
        return  # já está ligado

    try:
        await canal.connect(reconnect=True, self_deaf=True)
        print(f"Entrei na chamada 24/7: {canal.name}")
    except Exception as e:
        print(f"Erro ao entrar na chamada 24/7: {e!r}")


# ── Sistema de tickets ──
# Controla aberturas e fechos simultâneos, identifica o autor e regista os fechos.
ticket_create_lock = asyncio.Lock()
ticket_close_lock = asyncio.Lock()

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

def can_close_ticket(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.channel, discord.TextChannel)
        and isinstance(interaction.user, discord.Member)
        and (is_staff(interaction.user) or is_ticket_owner(interaction.channel, interaction.user))
    )

def record_ticket_closure(channel: discord.TextChannel, member_id: int, status: str):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            '''
            INSERT INTO ticket_closures
                (channel_id, guild_id, channel_name, closed_by, closed_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                status=excluded.status, closed_by=excluded.closed_by,
                closed_at=excluded.closed_at
            ''',
            (str(channel.id), str(channel.guild.id), channel.name,
             str(member_id), int(time.time()), status),
        )

# ── Confirmação de fecho ──
# Pede confirmação ao autor ou à equipa antes de apagar o canal do ticket.
class ConfirmarFechoView(discord.ui.View):
    def __init__(self, requester_id: int):
        super().__init__(timeout=60)
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id and can_close_ticket(interaction):
            return True
        await interaction.response.send_message("Não tens permissão para fechar este ticket.", ephemeral=True)
        return False

    @discord.ui.button(label="Confirmar fecho", style=discord.ButtonStyle.danger)
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="A fechar o ticket…", view=None)
        self.stop()
        channel = interaction.channel
        async with ticket_close_lock:
            if channel.guild.get_channel(channel.id) is None:
                return
            try:
                record_ticket_closure(channel, interaction.user.id, "pendente")
            except sqlite3.Error:
                await interaction.followup.send("Não foi possível guardar o registo. O ticket continua aberto.", ephemeral=True)
                return
            try:
                await channel.delete(reason=f"Ticket fechado por {interaction.user.id}")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                record_ticket_closure(channel, interaction.user.id, "falhou")
                await interaction.followup.send("Não foi possível fechar o ticket. Tenta novamente.", ephemeral=True)
                return
            record_ticket_closure(channel, interaction.user.id, "fechado")

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Fecho cancelado.", view=None)
        self.stop()

class TicketOpcoesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fechar ticket", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="ticket_finalizar_btn")
    async def finalizar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not can_close_ticket(interaction):
            await interaction.response.send_message("Não tens permissão para fechar este ticket.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Queres fechar este ticket? O canal e as mensagens serão apagados.",
            view=ConfirmarFechoView(interaction.user.id), ephemeral=True,
        )

# ── Abertura de tickets privados ──
# Verifica se já existe um pedido do mesmo tipo e abre um canal privado de suporte.
async def abrir_ticket(interaction: discord.Interaction, tipo: str, respostas=()):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("Abre o ticket dentro do servidor.", ephemeral=True)
        return
    categoria = guild.get_channel(CATEGORIAS_TICKET[tipo])
    if not isinstance(categoria, discord.CategoryChannel):
        await interaction.followup.send("A categoria de suporte está indisponível. Contacta a equipa.", ephemeral=True)
        return
    cargo_staff = guild.get_role(ID_CARGO_STAFF)
    if cargo_staff is None or guild.me is None:
        await interaction.followup.send("O suporte está temporariamente indisponível. Contacta a equipa.", ephemeral=True)
        return
    topic = ticket_topic(interaction.user.id, tipo)
    async with ticket_create_lock:
        canal = None
        try:
            canais = guild.text_channels
            existente = next((
                item for item in canais if isinstance(item, discord.TextChannel) and (
                    item.topic == topic or (
                        item.category_id == categoria.id and not (item.topic or "").startswith("vex-ticket:")
                        and item.name.endswith(f"-{tipo}") and is_ticket_owner(item, interaction.user)
                    )
                )
            ), None)
            if existente:
                await interaction.followup.send(f"Já tens um ticket aberto: {existente.mention}", ephemeral=True)
                return
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                cargo_staff: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            canal = await guild.create_text_channel(
                f"{interaction.user.name}-{tipo}".lower(),
                category=categoria, overwrites=overwrites, topic=topic,
            )
            embed = montar_embed_ticket(tipo, interaction.user)
            for label, value in respostas:
                embed.add_field(name=label, value=discord.utils.escape_markdown(value), inline=False)
            await canal.send(
                content=f"{interaction.user.mention} | {cargo_staff.mention}",
                embed=embed, view=TicketOpcoesView(),
            )
        except discord.HTTPException:
            if canal is not None:
                try:
                    await canal.delete(reason="Não foi possível publicar a mensagem inicial do ticket")
                except discord.HTTPException:
                    app.logger.exception("Não foi possível limpar o ticket incompleto %s.", canal.id)
            await interaction.followup.send("Não foi possível abrir o ticket. Tenta novamente ou contacta a equipa.", ephemeral=True)
            return
    await interaction.followup.send(f"O teu ticket foi criado aqui: {canal.mention}", ephemeral=True)

# ── Formulários e painel de tickets ──
# Recolhe dados para otimização e reotimização. Os tickets de dúvidas abrem diretamente.
class FormularioTicket(discord.ui.Modal):
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


class AbrirDiagnosticoView(discord.ui.View):
    def __init__(self, respostas):
        super().__init__(timeout=900)
        self.respostas = respostas

    @discord.ui.button(
        label="Abrir ticket de diagnóstico",
        emoji="🎫",
        style=discord.ButtonStyle.primary,
    )
    async def abrir(self, interaction: discord.Interaction, button: discord.ui.Button):
        await abrir_ticket(interaction, "diagnostico", self.respostas)


class FormularioDiagnostico(discord.ui.Modal):
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

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(FormularioDiagnostico(self.values[0]))


class EscolherProblemaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(EscolherProblemaSelect())


class PainelTicketsSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Diagnóstico gratuito",
                description="Recebe sugestões para o teu PC ou jogo",
                value="diagnostico",
                emoji="🖥️",
            ),
            discord.SelectOption(label="Adquirir Otimização", description="Pede uma otimização para o teu PC", value="adquirir", emoji=EMOJIS_TICKET["adquirir"]),
            discord.SelectOption(label="Dúvidas", description="Suporte geral", value="duvidas", emoji=EMOJIS_TICKET["duvidas"]),
            discord.SelectOption(label="Reotimizar", description="Pede uma nova otimização", value="reotimizar", emoji=EMOJIS_TICKET["reotimizar"]),
        ]
        super().__init__(placeholder="Escolhe o tipo de atendimento", options=options, custom_id="painel_tickets_select")

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

class PainelTicketsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PainelTicketsSelect())

# ── Botão de verificação ──
# Abre a página Web onde o membro inicia a autorização da conta Discord.
class VerifyAuthView(discord.ui.View):
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
        await self.tree.sync()

bot = MeuBot()

@bot.event
async def on_ready():
    print(f'O bot {bot.user} arrancou e está pronto a usar!')
    await entrar_na_chamada_247()


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
async def setup_tickets(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    embed_painel = discord.Embed(
        title="<:Vex:1550695876769489088> Vex Otimização — Tickets",
        description="Escolhe abaixo a opção que corresponde ao teu pedido.",
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
async def aviso(interaction: discord.Interaction, mensagem: str):
    await interaction.channel.send(mensagem)
    await interaction.response.send_message("Aviso enviado com sucesso!", ephemeral=True)

@bot.tree.command(name="setup_verify", description="Cria a mensagem de verificação (OAuth2)")
@app_commands.default_permissions(administrator=True)
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
async def pull_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    tokens = get_all_tokens()
    if not tokens:
        await interaction.followup.send("Nenhum utilizador na base de dados.", ephemeral=True)
        return

    sucessos, ja_no_servidor, falhas = 0, 0, 0
    bot_token = os.getenv("DISCORD_TOKEN")
    guild_id = interaction.guild_id

    for user_id, access_token, refresh_token, expires_at in tokens:
        if time.time() >= expires_at:
            access_token = refresh_access_token(user_id, refresh_token)
            if not access_token:
                falhas += 1
                continue
        status = add_user_to_guild(access_token, guild_id, user_id, bot_token)
        if status in (201, 200): sucessos += 1
        elif status == 204: ja_no_servidor += 1
        else: falhas += 1
        await asyncio.sleep(1)

    await interaction.followup.send(f"📊 **Resultado:** Sucessos: {sucessos} | Já no Servidor: {ja_no_servidor} | Falhas: {falhas}", ephemeral=True)

@bot.tree.command(name="pull_user", description="Adiciona um utilizador autorizado pelo respetivo ID")
@app_commands.default_permissions(administrator=True)
async def pull_user(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer(ephemeral=True)
    row = get_user_token(user_id)
    if not row:
        await interaction.followup.send("⚠️ Este utilizador não se encontra na base de dados.", ephemeral=True)
        return

    _, access_token, refresh_token, expires_at = row
    if time.time() >= expires_at:
        access_token = refresh_access_token(user_id, refresh_token)
        if not access_token:
            await interaction.followup.send("❌ O token expirou e não foi possível renová-lo.", ephemeral=True)
            return

    bot_token = os.getenv("DISCORD_TOKEN")
    status = add_user_to_guild(access_token, interaction.guild_id, user_id, bot_token)

    if status in (201, 200):
        await interaction.followup.send(f"✅ Utilizador `{user_id}` adicionado com sucesso!", ephemeral=True)
    elif status == 204:
        await interaction.followup.send(f"ℹ️ O utilizador `{user_id}` já está neste servidor.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Falha ao adicionar (Código API: {status}).", ephemeral=True)

@bot.tree.command(name="auth_stats", description="Mostra o número total de utilizadores autorizados na base de dados")
@app_commands.default_permissions(administrator=True)
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
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT channel_name, closed_by, closed_at, status FROM ticket_closures "
            "WHERE guild_id = ? ORDER BY closed_at DESC LIMIT 10",
            (str(interaction.guild_id),),
        ).fetchall()
    lines = [
        f"`{discord.utils.escape_markdown(name[:70])}` · <@{member_id}> · <t:{closed_at}:f> · {status}"
        for name, member_id, closed_at, status in rows
    ]
    await interaction.response.send_message(
        "\n".join(lines) if lines else "Ainda não há registos de fecho.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )

# ── Arranque da aplicação ──
# Inicia o servidor Web e liga o bot ao Discord com o token configurado.
if __name__ == '__main__':
    keep_alive()
    TOKEN = os.getenv('DISCORD_TOKEN')
    if TOKEN:
        bot.run(TOKEN)
