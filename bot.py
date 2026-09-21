import os
import threading
import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask

# ================= CONFIGURAÇÕES DO SERVIDOR WEB (RENDER) =================
app = Flask('')

@app.route('/')
def home():
    return "O bot está online e a funcionar 24/7!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_web_server)
    t.start()

# ================= CONFIGURAÇÕES DO BOT =================
ID_CARGO_MEMBRO = 1550663372272566382
ID_CATEGORIA_TICKETS = 1550661876528971826
# ========================================================

class TicketButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Abrir Ticket", style=discord.ButtonStyle.primary, custom_id="abrir_ticket_btn")
    async def ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        categoria = discord.utils.get(interaction.guild.categories, id=ID_CATEGORIA_TICKETS)
        canal = await interaction.guild.create_text_channel(f"ticket-{interaction.user.name}", category=categoria)
        
        await canal.set_permissions(interaction.guild.default_role, read_messages=False)
        await canal.set_permissions(interaction.user, read_messages=True, send_messages=True)
        
        await interaction.response.send_message(f"O teu ticket foi criado aqui: {canal.mention}", ephemeral=True)

class MeuBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(TicketButton())
        await self.tree.sync()

bot = MeuBot()

@bot.event
async def on_ready():
    print(f'O bot {bot.user} arrancou e está pronto a usar!')

@bot.event
async def on_member_join(member):
    cargo = member.guild.get_role(ID_CARGO_MEMBRO)
    if cargo:
        await member.add_roles(cargo)

@bot.tree.command(name="setup_tickets", description="Cria a mensagem com o botão de tickets")
@app_commands.default_permissions(administrator=True)
async def setup_tickets(interaction: discord.Interaction):
    await interaction.channel.send("Tens alguma dúvida ou precisas de ajuda? Clica no botão abaixo para abrir um ticket.", view=TicketButton())
    await interaction.response.send_message("Painel de tickets criado!", ephemeral=True)

@bot.tree.command(name="aviso", description="Envia uma mensagem para o canal atual usando o bot")
@app_commands.default_permissions(administrator=True)
async def aviso(interaction: discord.Interaction, mensagem: str):
    await interaction.channel.send(mensagem)
    await interaction.response.send_message("Aviso enviado com sucesso!", ephemeral=True)

if __name__ == '__main__':
    keep_alive()
    TOKEN = os.getenv('DISCORD_TOKEN')
    
    if not TOKEN:
        print("ERRO: A variável de ambiente 'DISCORD_TOKEN' não foi encontrada!")
    else:
        bot.run(TOKEN)