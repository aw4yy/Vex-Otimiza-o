import os
import asyncio
import threading
import sqlite3
import requests
import time
import urllib.parse
import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask, request, redirect

# ================= 🗄️ BASE DE DADOS PERSISTENTE (SQLITE) =================
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

# ================= 🌐 CONFIGURAÇÕES OAUTH2 & WEB SERVER =================
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI") # Ex: https://vex-otimiza-o-production.up.railway.app/callback

def get_discord_auth_url():
    if not CLIENT_ID or not REDIRECT_URI:
        return "https://discord.com"
    redirect_encoded = urllib.parse.quote(REDIRECT_URI, safe='')
    return f"https://discord.com/oauth2/authorize?client_id={CLIENT_ID}&response_type=code&redirect_uri={redirect_encoded}&scope=guilds.join%20identify"

app = Flask(__name__)

@app.route('/')
def home():
    return "O bot e o servidor Auth estão online e operacionais 24/7!"

@app.route('/auth')
def auth():
    if not CLIENT_ID or not REDIRECT_URI:
        return "Erro interno: CLIENT_ID ou REDIRECT_URI não definidos no Railway.", 500
    return redirect(get_discord_auth_url())

@app.route('/callback')
def callback():
    error = request.args.get('error')
    if error:
        return f"Autorização cancelada ou recusada. Motivo: {error}", 400

    code = request.args.get('code')
    if not code:
        return "Erro: Código de autorização não fornecido.", 400

    token_url = "https://discord.com/api/oauth2/token"
    payload = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT_URI
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    
    response = requests.post(token_url, data=payload, headers=headers)
    if response.status_code != 200:
        return f"Erro ao obter token do Discord: {response.text}", 400

    token_data = response.json()
    access_token = token_data['access_token']
    refresh_token = token_data['refresh_token']
    expires_in = token_data['expires_in']

    # Obter ID do utilizador
    user_info_url = "https://discord.com/api/users/@me"
    user_headers = {'Authorization': f"Bearer {access_token}"}
    user_response = requests.get(user_info_url, headers=user_headers)
    
    if user_response.status_code != 200:
        return "Erro ao obter informações do perfil de utilizador.", 400

    user_data = user_response.json()
    user_id = user_data['id']

    # 1. Guardar tokens na DB
    save_user_tokens(user_id, access_token, refresh_token, expires_in)

    # 2. Atribuir automaticamente o cargo de Membro no Discord
    try:
        bot_token = os.getenv("DISCORD_TOKEN")
        guild_id = os.getenv("GUILD_ID")
        
        # Se GUILD_ID não estiver no Railway, tenta usar o servidor onde o bot está
        if not guild_id and bot.guilds:
            guild_id = str(bot.guilds[0].id)
            
        if guild_id and bot_token:
            role_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles/{ID_CARGO_MEMBRO}"
            role_headers = {"Authorization": f"Bot {bot_token}"}
            res = requests.put(role_url, headers=role_headers)
            print(f"Atribuição do cargo {ID_CARGO_MEMBRO} ao utilizador {user_id}: Status {res.status_code}")
    except Exception as e:
        print(f"Erro ao atribuir cargo no callback: {e}")

    data_hora_atual = time.strftime("%d/%m/%Y, %H:%M")

    # 🎨 HTML DA PÁGINA WEB COM FUNDO ANIMADO EM MOVIMENTO
    return f"""
    <!DOCTYPE html>
    <html lang="pt">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Verificação Concluída - Vex Otimização</title>
        <style>
            * {{
                box-sizing: border-box;
                margin: 0;
                padding: 0;
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            }}
            
            body {{
                background: linear-gradient(-45deg, #090a0f, #121520, #08090d, #1a1228);
                background-size: 400% 400%;
                animation: animateBackground 12s ease infinite;
                color: #ffffff;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                padding: 20px;
                overflow: hidden;
                position: relative;
            }}

            @keyframes animateBackground {{
                0% {{ background-position: 0% 50%; }}
                50% {{ background-position: 100% 50%; }}
                100% {{ background-position: 0% 50%; }}
            }}

            .glow-1, .glow-2 {{
                position: absolute;
                width: 350px;
                height: 350px;
                border-radius: 50%;
                filter: blur(100px);
                opacity: 0.25;
                z-index: 0;
                animation: floatGlow 10s ease-in-out infinite alternate;
            }}
            .glow-1 {{
                background: #5865f2;
                top: 15%;
                left: 20%;
            }}
            .glow-2 {{
                background: #23a55a;
                bottom: 15%;
                right: 20%;
                animation-delay: -5s;
            }}

            @keyframes floatGlow {{
                0% {{ transform: translate(0, 0) scale(1); }}
                100% {{ transform: translate(40px, -50px) scale(1.3); }}
            }}

            .card {{
                position: relative;
                z-index: 1;
                background: rgba(20, 21, 26, 0.85);
                backdrop-filter: blur(16px);
                -webkit-backdrop-filter: blur(16px);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 20px;
                width: 100%;
                max-width: 400px;
                overflow: hidden;
                box-shadow: 0 20px 50px rgba(0, 0, 0, 0.8);
                text-align: center;
            }}

            .banner {{
                width: 100%;
                height: 120px;
                background: linear-gradient(135deg, #1f2235 0%, #0d0e14 100%);
                background-size: cover;
                background-position: center;
            }}

            .avatar-container {{
                position: relative;
                display: inline-block;
                margin-top: -34px;
                margin-bottom: 12px;
            }}

            .avatar {{
                width: 68px;
                height: 68px;
                border-radius: 50%;
                border: 4px solid #14151a;
                background: linear-gradient(135deg, #2b2d42, #181924);
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: 800;
                font-size: 18px;
                color: #fff;
                box-shadow: 0 4px 15px rgba(0,0,0,0.5);
            }}

            .badge {{
                position: absolute;
                bottom: 2px;
                right: 2px;
                background-color: #23a55a;
                color: white;
                border-radius: 50%;
                width: 22px;
                height: 22px;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 12px;
                border: 3px solid #14151a;
            }}

            .content {{
                padding: 0 24px 26px 24px;
            }}

            .title {{
                font-size: 20px;
                font-weight: 700;
                color: #ffffff;
                margin-bottom: 6px;
            }}

            .timestamp {{
                font-size: 12px;
                color: #72767d;
                margin-bottom: 16px;
            }}

            .description {{
                font-size: 13px;
                color: #96989d;
                line-height: 1.5;
                margin-bottom: 20px;
            }}

            .server-box {{
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 12px;
                padding: 12px 14px;
                display: flex;
                align-items: center;
                justify-content: space-between;
                margin-bottom: 20px;
            }}

            .server-info {{
                display: flex;
                align-items: center;
                gap: 10px;
            }}

            .server-icon {{
                width: 36px;
                height: 36px;
                border-radius: 10px;
                background: #2b2d42;
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: bold;
                font-size: 12px;
            }}

            .server-name {{
                font-size: 13px;
                font-weight: 600;
                color: #ffffff;
                text-align: left;
            }}

            .verified-tag {{
                font-size: 12px;
                color: #23a55a;
                font-weight: 600;
            }}

            .btn {{
                display: block;
                width: 100%;
                padding: 13px;
                background: rgba(255, 255, 255, 0.08);
                color: #ffffff;
                text-decoration: none;
                font-weight: 600;
                font-size: 13px;
                border-radius: 10px;
                transition: all 0.2s ease;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }}

            .btn:hover {{
                background: rgba(255, 255, 255, 0.15);
                transform: translateY(-2px);
            }}
        </style>
    </head>
    <body>
        <div class="glow-1"></div>
        <div class="glow-2"></div>

        <div class="card">
            <div class="banner"></div>
            <div class="avatar-container">
                <div class="avatar">VEX</div>
                <div class="badge">✓</div>
            </div>
            <div class="content">
                <h1 class="title">Verificação concluída</h1>
                <div class="timestamp">{data_hora_atual}</div>
                <p class="description">
                    Sua identidade foi confirmada com segurança e vinculada ao servidor <strong>Vex Otimização</strong>. O cargo de acesso já foi processado.
                </p>
                <div class="server-box">
                    <div class="server-info">
                        <div class="server-icon">VEX</div>
                        <div class="server-name">Vex Otimização</div>
                    </div>
                    <div class="verified-tag">✓ Verified</div>
                </div>
                <a href="discord://" class="btn">Acessar servidor</a>
            </div>
        </div>
    </body>
    </html>
    """

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_web_server)
    t.daemon = True
    t.start()

# ================= 🔄 REFRESH & PULL DE MEMBROS =================
def refresh_access_token(user_id: str, refresh_token: str):
    token_url = "https://discord.com/api/oauth2/token"
    payload = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    response = requests.post(token_url, data=payload, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        new_access = data['access_token']
        new_refresh = data['refresh_token']
        expires_in = data['expires_in']
        save_user_tokens(user_id, new_access, new_refresh, expires_in)
        return new_access
    return None

def add_user_to_guild(access_token: str, guild_id: int, user_id: str, bot_token: str):
    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json"
    }
    payload = {"access_token": access_token}
    response = requests.put(url, json=payload, headers=headers)
    return response.status_code

# ================= 🤖 CONFIGURAÇÕES DO BOT =================
ID_CARGO_MEMBRO = 1550663372272566382
ID_CARGO_STAFF = 1550663253032566834

CATEGORIAS_TICKET = {
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

# ================= 🛠️ FUNÇÕES AUXILIARES =================
def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    cargo_staff = member.guild.get_role(ID_CARGO_STAFF)
    return cargo_staff is not None and cargo_staff in member.roles

def montar_embed_ticket(tipo: str, autor: discord.abc.User) -> discord.Embed:
    emoji = EMOJIS_TICKET.get(tipo, "🎫")
    textos = {
        "adquirir": (
            "<:adquirir:1551427917144260609> Adquirir Otimização",
            discord.Color.gold(),
            "🧑‍💼 **Bem-vindo ao Suporte de Aquisição!**\n\n"
            "Estamos aqui para otimizar sua experiência. Descreve aqui o que pretendes "
            "adquirir e a nossa equipa trata do resto o mais rápido possível.",
        ),
        "duvidas": (
            "<:duvida:1551427863587069972> Dúvidas - Suporte Geral",
            discord.Color.blurple(),
            "🧑‍💼 **Bem-vindo ao Suporte de Dúvidas!**\n\n"
            "Estamos aqui para otimizar sua experiência. Se tiver alguma dúvida ou "
            "precisar de assistência, fique à vontade para perguntar. Nosso time de "
            "especialistas está pronto para fornecer soluções rápidas e eficazes!",
        ),
        "reotimizar": (
            "<:eng:1551427790534877325> Reotimizar",
            discord.Color.green(),
            "🧑‍💼 **Bem-vindo ao Suporte de Reotimização!**\n\n"
            "Pediste para refazer a tua Otimização Exclusiva. Descreve aqui o pedido e a "
            "nossa equipa vai analisar e tratar disso o mais rápido possível.",
        ),
    }
    titulo, cor, descricao = textos.get(tipo, (f"{emoji} Ticket", discord.Color.dark_theme(), ""))
    embed = discord.Embed(title=titulo, description=descricao, color=cor)
    embed.add_field(name="🧑‍💼 Horário de Atendimento", value=HORARIO_ATENDIMENTO, inline=False)
    embed.add_field(
        name="\u200b",
        value=(
            "🧑‍💼 Durante o horário de atendimento, nossa equipe estará **100% disponível** "
            "para te ajudar com dúvidas, solicitações e suporte!"
        ),
        inline=False,
    )
    embed.set_author(name=f"Ticket de {autor.name}", icon_url=autor.display_avatar.url)
    embed.timestamp = discord.utils.utcnow()
    return embed

# ================= 🎫 VIEWS =================
class TicketOpcoesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Finalizar Ticket",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_finalizar_btn",
    )
    async def finalizar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Ticket será encerrado em alguns segundos...")
        await asyncio.sleep(3)
        await interaction.channel.delete()

    @discord.ui.button(
        label="Opções",
        emoji="⚙️",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_opcoes_btn",
    )
    async def opcoes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("🚫 Apenas responsáveis.", ephemeral=True)
            return
        await interaction.response.send_message("⚙️ Opções da equipa (por definir).", ephemeral=True)

class PainelTicketsSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Adquirir Otimização",
                description="Garanta sua Otimização",
                value="adquirir",
                emoji="<:adquirir:1551427917144260609>",
            ),
            discord.SelectOption(
                label="Dúvidas",
                description="Suporte geral",
                value="duvidas",
                emoji="<:duvida:1551427863587069972>",
            ),
            discord.SelectOption(
                label="Reotimizar",
                description="Refazer Otimização Exclusiva",
                value="reotimizar",
                emoji="<:eng:1551427790534877325>",
            ),
        ]
        super().__init__(
            placeholder="Selecione o atendimento que melhor atende à sua necessidade...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="painel_tickets_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        tipo = self.values[0]
        guild = interaction.guild
        categoria_id = CATEGORIAS_TICKET.get(tipo)
        categoria = discord.utils.get(guild.categories, id=categoria_id) if categoria_id else None
        nome_canal = f"{interaction.user.name}-{tipo}".lower()
        existente = discord.utils.get(guild.text_channels, name=nome_canal)

        if existente:
            await interaction.followup.send(
                f"Já tens um ticket aberto: {existente.mention}", ephemeral=True
            )
            return

        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            }
            cargo_staff = guild.get_role(ID_CARGO_STAFF)
            if cargo_staff:
                overwrites[cargo_staff] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

            canal = await guild.create_text_channel(nome_canal, category=categoria, overwrites=overwrites)
            mencoes = interaction.user.mention
            if cargo_staff:
                mencoes += f" | {cargo_staff.mention}"

            embed = montar_embed_ticket(tipo, interaction.user)
            await canal.send(content=mencoes, embed=embed, view=TicketOpcoesView())
            await interaction.followup.send(f"O teu ticket foi criado aqui: {canal.mention}", ephemeral=True)

        except discord.Forbidden:
            await interaction.followup.send(
                "⚠️ Não tenho permissões suficientes para criar o canal do ticket. Avisa a equipa.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                "⚠️ Ocorreu um erro ao criar o ticket. Avisa a equipa.", ephemeral=True
            )

class PainelTicketsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PainelTicketsSelect())

class VerifyAuthView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Verificar Conta",
            url=get_discord_auth_url(),
            style=discord.ButtonStyle.link,
            emoji="✅"
        ))

# ================= 🤖 CLASSE DO BOT =================
class MeuBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(PainelTicketsView())
        self.add_view(TicketOpcoesView())
        self.add_view(VerifyAuthView())
        synced = await self.tree.sync()
        print(f"Sincronizados {len(synced)} comandos Slash com sucesso!")

bot = MeuBot()

# ================= ⚡ EVENTOS DO BOT =================
@bot.event
async def on_ready():
    print(f'O bot {bot.user} arrancou e está pronto a usar!')

@bot.event
async def on_member_join(member):
    # O utilizador entra sem cargo para apenas ver o canal de verificação.
    pass

# ================= 💬 COMANDOS SLASH =================
@bot.tree.command(name="setup_tickets", description="Cria o painel de tickets com o menu de seleção")
@app_commands.default_permissions(administrator=True)
async def setup_tickets(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    embed_painel = discord.Embed(
        title="<:Vex:1550695876769489088> Vex Otimização - Ticket's",
        description=(
            "Selecione abaixo a opção que melhor atende à sua necessidade e abra seu "
            "ticket para receber um suporte adequado e personalizado. Assim, poderemos "
            "te ajudar da melhor forma possível."
        ),
        color=discord.Color.dark_theme(),
    )
    ficheiros = []
    if os.path.isfile(BANNER_PATH):
        ficheiro_banner = discord.File(BANNER_PATH, filename="banner.png")
        embed_painel.set_image(url="attachment://banner.png")
        ficheiros.append(ficheiro_banner)

    try:
        if ficheiros:
            await interaction.channel.send(embed=embed_painel, files=ficheiros, view=PainelTicketsView())
        else:
            await interaction.channel.send(embed=embed_painel, view=PainelTicketsView())
            
        await interaction.followup.send("Painel de tickets criado com sucesso!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Ocorreu um erro ao criar o painel: {e}", ephemeral=True)

@bot.tree.command(name="aviso", description="Envia uma mensagem para o canal atual usando o bot")
@app_commands.default_permissions(administrator=True)
async def aviso(interaction: discord.Interaction, mensagem: str):
    await interaction.channel.send(mensagem)
    await interaction.response.send_message("Aviso enviado com sucesso!", ephemeral=True)

@bot.tree.command(name="setup_verify", description="Cria a mensagem de verificação (OAuth2)")
@app_commands.default_permissions(administrator=True)
async def setup_verify(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔒 Verificação de Segurança",
        description=(
            "Para teres acesso aos canais do servidor, clica no botão abaixo para te **verificares**.\n\n"
            "Ao clicares, serás redirecionado para autorizar a aplicação e validar o teu acesso."
        ),
        color=discord.Color.green()
    )
    await interaction.channel.send(embed=embed, view=VerifyAuthView())
    await interaction.response.send_message("Painel de verificação enviado!", ephemeral=True)

@bot.tree.command(name="pull_all", description="Puxa todos os membros autorizados da DB para este servidor")
@app_commands.default_permissions(administrator=True)
async def pull_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    tokens = get_all_tokens()
    if not tokens:
        await interaction.followup.send("Nenhum utilizador registado na base de dados.", ephemeral=True)
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
        
        if status in (201, 200):
            sucessos += 1
        elif status == 204:
            ja_no_servidor += 1
        else:
            falhas += 1
            
        await asyncio.sleep(1) # Prevenir rate limits

    embed = discord.Embed(title="📊 Resultado do Pull de Membros", color=discord.Color.blue())
    embed.add_field(name="Adicionados com Sucesso", value=str(sucessos), inline=True)
    embed.add_field(name="Já no Servidor", value=str(ja_no_servidor), inline=True)
    embed.add_field(name="Falhas / Tokens Expirados", value=str(falhas), inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="pull_user", description="Puxa um utilizador específico pelo seu ID")
@app_commands.default_permissions(administrator=True)
async def pull_user(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer(ephemeral=True)
    
    row = get_user_token(user_id)
    if not row:
        await interaction.followup.send("⚠️ Este utilizador não se encontra na base de dados de autorizados.", ephemeral=True)
        return

    _, access_token, refresh_token, expires_at = row
    
    if time.time() >= expires_at:
        access_token = refresh_access_token(user_id, refresh_token)
        if not access_token:
            await interaction.followup.send("❌ O token do utilizador expirou e não foi possível renová-lo.", ephemeral=True)
            return

    bot_token = os.getenv("DISCORD_TOKEN")
    status = add_user_to_guild(access_token, interaction.guild_id, user_id, bot_token)

    if status in (201, 200):
        await interaction.followup.send(f"✅ Utilizador `{user_id}` adicionado ao servidor com sucesso!", ephemeral=True)
    elif status == 204:
        await interaction.followup.send(f"ℹ️ O utilizador `{user_id}` já se encontra neste servidor.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Falha ao adicionar utilizador (Código API: {status}).", ephemeral=True)

@bot.tree.command(name="auth_stats", description="Mostra o número total de utilizadores autorizados na DB")
@app_commands.default_permissions(administrator=True)
async def auth_stats(interaction: discord.Interaction):
    tokens = get_all_tokens()
    await interaction.response.send_message(
        f"📈 **Estatísticas de Autorização:**\nTotal de membros autorizados na DB: `{len(tokens)}`", 
        ephemeral=True
    )

# ================= 🚀 INICIAR O BOT =================
if __name__ == '__main__':
    keep_alive()
    TOKEN = os.getenv('DISCORD_TOKEN')
    if not TOKEN:
        print("ERRO: A variável de ambiente 'DISCORD_TOKEN' não foi encontrada!")
    else:
        bot.run(TOKEN)
