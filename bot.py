import os
import asyncio
import threading
import sqlite3
import requests
import time
import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask, request, redirect

# ================= 🗄️ BASE DE DADOS (SQLITE) =================
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
REDIRECT_URI = os.getenv("REDIRECT_URI") # Ex: https://teu-dominio.up.railway.app/callback

app = Flask('')

@app.route('/')
def home():
    return "O bot e o sistema de Verificação (Auth) estão online e operacionais 24/7!"

@app.route('/auth')
def auth():
    if not CLIENT_ID or not REDIRECT_URI:
        return "Erro interno: CLIENT_ID ou REDIRECT_URI não estão configurados no Railway.", 500
    
    discord_auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify%20guilds.join"
    )
    return redirect(discord_auth_url)

@app.route('/callback')
def callback():
    # Se o utilizador clicar em "Cancelar" no Discord
    error = request.args.get('error')
    if error:
        return f"Autorização cancelada ou falhou. Motivo: {error}", 400

    code = request.args.get('code')
    if not code:
        return "Erro: Código de autorização não fornecido.", 400

    # 1. Trocar o código pelo token
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
        return f"Erro ao obter token do Discord. Verifica as credenciais no Railway.", 400

    token_data = response.json()
    access_token = token_data['access_token']
    refresh_token = token_data['refresh_token']
    expires_in = token_data['expires_in']

    # 2. Obter o ID do utilizador usando o access_token
    user_info_url = "https://discord.com/api/users/@me"
    user_headers = {'Authorization': f"Bearer {access_token}"}
    user_response = requests.get(user_info_url, headers=user_headers)
    
    if user_response.status_code != 200:
        return "Erro ao ler o ID da tua conta do Discord.", 400

    user_data = user_response.json()
    user_id = user_data['id']

    # 3. Guardar na base de dados
    save_user_tokens(user_id, access_token, refresh_token, expires_in)

    return """
    <div style="text-align: center; font-family: Arial, sans-serif; margin-top: 100px;">
        <h1 style="color: #43b581;">✅ Verificação Concluída!</h1>
        <p style="font-size: 18px; color: #fff; background-color: #36393f; padding: 20px; border-radius: 8px; display: inline-block;">
            A tua conta foi verificada com sucesso. Já podes fechar esta página e voltar ao Discord.
        </p>
    </div>
    <style>body { background-color: #2f3136; color: white; }</style>
    """

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_web_server)
    t.daemon = True
    t.start()

# ================= 🔄 AUXILIAR DE REFRESH & PULL =================
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
            "🧑‍💼 **Bem-vindo ao Suporte de Aquisição!**\n\nEstamos aqui para otimizar a sua experiência. Descreve o que pretendes adquirir.",
        ),
        "duvidas": (
            "<:duvida:1551427863587069972> Dúvidas - Suporte Geral",
            discord.Color.blurple(),
            "🧑‍💼 **Bem-vindo ao Suporte de Dúvidas!**\n\nSe tiver alguma dúvida ou precisar de assistência, fique à vontade para perguntar.",
        ),
        "reotimizar": (
            "<:eng:1551427790534877325> Reotimizar",
            discord.Color.green(),
            "🧑‍💼 **Bem-vindo ao Suporte de Reotimização!**\n\nPediste para refazer a tua Otimização Exclusiva. Descreve o teu pedido.",
        ),
    }
    titulo, cor, descricao = textos.get(tipo, (f"{emoji} Ticket", discord.Color.dark_theme(), ""))
    embed = discord.Embed(title=titulo, description=descricao, color=cor)
    embed.add_field(name="🧑‍💼 Horário de Atendimento", value=HORARIO_ATENDIMENTO, inline=False)
    embed.set_author(name=f"Ticket de {autor.name}", icon_url=autor.display_avatar.url)
    embed.timestamp = discord.utils.utcnow()
    return embed

# ================= 🎫 VIEWS =================
class TicketOpcoesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Finalizar Ticket", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="ticket_finalizar_btn")
    async def finalizar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Ticket será encerrado em 3 segundos...")
        await asyncio.sleep(3)
        await interaction.channel.delete()

class PainelTicketsSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Adquirir Otimização", value="adquirir", emoji="<:adquirir:1551427917144260609>"),
            discord.SelectOption(label="Dúvidas", value="duvidas", emoji="<:duvida:1551427863587069972>"),
            discord.SelectOption(label="Reotimizar", value="reotimizar", emoji="<:eng:1551427790534877325>"),
        ]
        super().__init__(placeholder="Selecione o atendimento...", min_values=1, max_values=1, options=options, custom_id="painel_tickets_select")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        tipo = self.values[0]
        guild = interaction.guild
        categoria_id = CATEGORIAS_TICKET.get(tipo)
        categoria = discord.utils.get(guild.categories, id=categoria_id) if categoria_id else None
        nome_canal = f"{interaction.user.name}-{tipo}".lower()
        existente = discord.utils.get(guild.text_channels, name=nome_canal)

        if existente:
            return await interaction.followup.send(f"Já tens um ticket aberto: {existente.mention}", ephemeral=True)

        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            }
            cargo_staff = guild.get_role(ID_CARGO_STAFF)
            if cargo_staff:
                overwrites[cargo_staff] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

            canal = await guild.create_text_channel(nome_canal, category=categoria, overwrites=overwrites)
            mencoes = interaction.user.mention + (f" | {cargo_staff.mention}" if cargo_staff else "")
            
            await canal.send(content=mencoes, embed=montar_embed_ticket(tipo, interaction.user), view=TicketOpcoesView())
            await interaction.followup.send(f"O teu ticket foi criado aqui: {canal.mention}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send("⚠️ Erro ao criar ticket. Verifica as permissões.", ephemeral=True)

class PainelTicketsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PainelTicketsSelect())

class VerifyAuthView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        # Tenta pegar o link direto para /auth
        auth_url = REDIRECT_URI.replace("/callback", "/auth") if REDIRECT_URI else "https://discord.com"
        self.add_item(discord.ui.Button(label="Verificar Conta", url=auth_url, style=discord.ButtonStyle.link, emoji="✅"))

# ================= 🤖 INÍCIO BOT =================
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
        await self.tree.sync()

bot = MeuBot()

@bot.event
async def on_ready():
    print(f'O bot {bot.user} arrancou e está pronto!')

@bot.event
async def on_member_join(member):
    cargo = member.guild.get_role(ID_CARGO_MEMBRO)
    if cargo:
        try: await member.add_roles(cargo)
        except: pass

# ================= 💬 COMANDOS SLASH =================
@bot.tree.command(name="setup_tickets", description="Cria o painel de tickets")
@app_commands.default_permissions(administrator=True)
async def setup_tickets(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    embed_painel = discord.Embed(
        title="<:Vex:1550695876769489088> Vex Otimização - Ticket's",
        description="Selecione abaixo a opção que melhor atende à sua necessidade.",
        color=discord.Color.dark_theme(),
    )
    ficheiros = []
    if os.path.isfile(BANNER_PATH):
        ficheiros.append(discord.File(BANNER_PATH, filename="banner.png"))
        embed_painel.set_image(url="attachment://banner.png")
    
    await interaction.channel.send(embed=embed_painel, files=ficheiros, view=PainelTicketsView())
    await interaction.followup.send("Painel de tickets criado!", ephemeral=True)

@bot.tree.command(name="setup_verify", description="Cria a mensagem de verificação (OAuth2)")
@app_commands.default_permissions(administrator=True)
async def setup_verify(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔒 Verificação de Segurança",
        description="Clica no botão abaixo para te **verificares** e teres acesso ao servidor.\n\nSerás redirecionado para autorizar a aplicação (isto permite-nos restaurar o teu acesso futuramente).",
        color=discord.Color.green()
    )
    await interaction.channel.send(embed=embed, view=VerifyAuthView())
    await interaction.response.send_message("Painel de verificação enviado!", ephemeral=True)

@bot.tree.command(name="pull_all", description="Puxa todos os membros autorizados (DB) para este servidor")
@app_commands.default_permissions(administrator=True)
async def pull_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    tokens = get_all_tokens()
    
    if not tokens:
        return await interaction.followup.send("Ninguém na base de dados.", ephemeral=True)

    sucessos, ja_estava, falhas = 0, 0, 0
    bot_token = os.getenv("DISCORD_TOKEN")
    
    for user_id, access_token, refresh_token, expires_at in tokens:
        if time.time() >= expires_at:
            access_token = refresh_access_token(user_id, refresh_token)
            if not access_token:
                falhas += 1
                continue

        status = add_user_to_guild(access_token, interaction.guild_id, user_id, bot_token)
        
        if status in (201, 200): sucessos += 1
        elif status == 204: ja_estava += 1
        else: falhas += 1
        await asyncio.sleep(1) # Previne block da API

    embed = discord.Embed(title="📊 Resultado do Pull", color=discord.Color.blue())
    embed.add_field(name="Adicionados", value=str(sucessos))
    embed.add_field(name="Já no Servidor", value=str(ja_estava))
    embed.add_field(name="Falharam", value=str(falhas))
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="pull_user", description="Puxa um membro específico pelo ID")
@app_commands.default_permissions(administrator=True)
async def pull_user(interaction: discord.Interaction, id_utilizador: str):
    await interaction.response.defer(ephemeral=True)
    row = get_user_token(id_utilizador)
    
    if not row:
        return await interaction.followup.send("⚠️ Este utilizador não autorizou o bot.", ephemeral=True)

    _, access_token, refresh_token, expires_at = row
    if time.time() >= expires_at:
        access_token = refresh_access_token(id_utilizador, refresh_token)
        if not access_token:
            return await interaction.followup.send("❌ Token expirou e não pôde ser renovado.", ephemeral=True)

    status = add_user_to_guild(access_token, interaction.guild_id, id_utilizador, os.getenv("DISCORD_TOKEN"))
    if status in (201, 200): await interaction.followup.send(f"✅ Utilizador `{id_utilizador}` adicionado!", ephemeral=True)
    elif status == 204: await interaction.followup.send(f"ℹ️ O utilizador já está no servidor.", ephemeral=True)
    else: await interaction.followup.send(f"❌ Falha ao adicionar (Erro API: {status}).", ephemeral=True)

@bot.tree.command(name="auth_stats", description="Mostra quantos utilizadores verificados tens na DB")
@app_commands.default_permissions(administrator=True)
async def auth_stats(interaction: discord.Interaction):
    await interaction.response.send_message(f"📈 **Membros na Base de Dados:** `{len(get_all_tokens())}`", ephemeral=True)

# ================= 🚀 START =================
if __name__ == '__main__':
    keep_alive()
    if not os.getenv('DISCORD_TOKEN'):
        print("ERRO: Falta a variável 'DISCORD_TOKEN'")
    else:
        bot.run(os.getenv('DISCORD_TOKEN'))
