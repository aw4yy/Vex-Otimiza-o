import os
import asyncio
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
    # O Render usa obrigatoriamente a variável de ambiente PORT (por defeito 10000)
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    t = threading.Thread(target=run_web_server)
    t.daemon = True
    t.start()


# ================= CONFIGURAÇÕES DO BOT =================
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


# ================= 🎫 VIEWS: BOTÕES DENTRO DO TICKET =================


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


# ================= 📋 PAINEL: MENU DE SELEÇÃO =================


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
            print("Erro ao criar ticket: falta permissão (Manage Channels / Manage Roles) para o cargo do bot.")
            await interaction.followup.send(
                "⚠️ Não tenho permissões suficientes para criar o canal do ticket. Avisa a equipa.",
                ephemeral=True,
            )
        except Exception as e:
            print(f"Erro ao criar ticket: {e!r}")
            await interaction.followup.send(
                "⚠️ Ocorreu um erro ao criar o ticket. Avisa a equipa.", ephemeral=True
            )


class PainelTicketsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PainelTicketsSelect())


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
        await self.tree.sync()


bot = MeuBot()


# ================= ⚡ EVENTOS DO BOT =================


@bot.event
async def on_ready():
    print(f'O bot {bot.user} arrancou e está pronto a usar!')


@bot.event
async def on_member_join(member):
    cargo = member.guild.get_role(ID_CARGO_MEMBRO)
    if cargo:
        await member.add_roles(cargo)


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
    else:
        print(f"AVISO: não encontrei o ficheiro do banner em '{BANNER_PATH}'.")

    try:
        if ficheiros:
            await interaction.channel.send(embed=embed_painel, files=ficheiros, view=PainelTicketsView())
        else:
            await interaction.channel.send(embed=embed_painel, view=PainelTicketsView())
            
        await interaction.followup.send("Painel de tickets criado com sucesso!", ephemeral=True)
    except discord.Forbidden:
        print("Erro ao criar painel: falta permissão para enviar mensagens/anexos neste canal.")
        await interaction.followup.send(
            "⚠️ Não tenho permissão para enviar mensagens/anexos neste canal.", ephemeral=True
        )
    except Exception as e:
        print(f"Erro ao criar painel: {e!r}")
        await interaction.followup.send("⚠️ Ocorreu um erro ao criar o painel.", ephemeral=True)


@bot.tree.command(name="aviso", description="Envia uma mensagem para o canal atual usando o bot")
@app_commands.default_permissions(administrator=True)
async def aviso(interaction: discord.Interaction, mensagem: str):
    await interaction.channel.send(mensagem)
    await interaction.response.send_message("Aviso enviado com sucesso!", ephemeral=True)


# ================= 🚀 INICIAR O BOT =================

if __name__ == '__main__':
    keep_alive()
    TOKEN = os.getenv('DISCORD_TOKEN')
    if not TOKEN:
        print("ERRO: A variável de ambiente 'DISCORD_TOKEN' não foi encontrada!")
    else:
        bot.run(TOKEN)
