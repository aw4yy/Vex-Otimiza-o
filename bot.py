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

    # Obter ID e dados do utilizador
    user_info_url = "https://discord.com/api/users/@me"
    user_headers = {'Authorization': f"Bearer {access_token}"}
    user_response = requests.get(user_info_url, headers=user_headers)
    
    if user_response.status_code != 200:
        return "Erro ao obter informações do perfil de utilizador.", 400

    user_data = user_response.json()
    user_id = user_data['id']
    avatar_hash = user_data.get('avatar')
    
    # URL do Avatar do Utilizador
    if avatar_hash:
        user_avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=128"
    else:
        user_avatar_url = "https://cdn.discordapp.com/embed/avatars/0.png"

    # 1. Guardar tokens na DB
    save_user_tokens(user_id, access_token, refresh_token, expires_in)

    # 2. Obter Informações do Servidor (Ícone e Banner) e Atribuir Cargo
    guild_icon_url = ""
    guild_banner_url = ""
    guild_name = "Vex Otimização"

    try:
        bot_token = os.getenv("DISCORD_TOKEN")
        guild_id = os.getenv("GUILD_ID")
        
        if not guild_id and bot.guilds:
            guild_id = str(bot.guilds[0].id)
            
        if guild_id and bot_token:
            # Obter dados do Servidor via API do Discord
            guild_req = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}", headers={"Authorization": f"Bot {bot_token}"})
            if guild_req.status_code == 200:
                g_data = guild_req.json()
                guild_name = g_data.get('name', guild_name)
                icon_hash = g_data.get('icon')
                banner_hash = g_data.get('banner')

                if icon_hash:
                    guild_icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.png?size=128"
                if banner_hash:
                    guild_banner_url = f"https://cdn.discordapp.com/banners/{guild_id}/{banner_hash}.png?size=600"

            # Atribuir Cargo
            role_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles/{ID_CARGO_MEMBRO}"
            role_headers = {"Authorization": f"Bot {bot_token}"}
            res = requests.put(role_url, headers=role_headers)
            print(f"Atribuição do cargo {ID_CARGO_MEMBRO} ao utilizador {user_id}: Status {res.status_code}")
    except Exception as e:
        print(f"Erro no callback (GuildInfo/Cargo): {e}")

    data_hora_atual = time.strftime("%d/%m/%Y, %H:%M")

    # CSS Dinâmico para o Banner
    banner_style = f"background-image: url('{guild_banner_url}');" if guild_banner_url else "background: linear-gradient(135deg, #1f2235 0%, #0d0e14 100%);"
    
    # HTML do Ícone do Servidor
    server_icon_html = f"<img src='{guild_icon_url}' class='server-icon-img' alt='Logo'>" if guild_icon_url else "<div class='server-icon'>VEX</div>"

    # 🎨 HTML DA PÁGINA WEB COM AVATAR DO UTILIZADOR E DADOS DO SERVIDOR
    return f"""
    <!DOCTYPE html>
    <html lang="pt">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Verificação Concluída - {guild_name}</title>
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
                {banner_style}
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
                object-fit: cover;
                box-shadow: 0 4px 15px rgba(0,0,0,0.5);
                background-color: #14151a;
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

            .server-icon-img {{
                width: 36px;
                height: 36px;
                border-radius: 10px;
                object-fit: cover;
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
                <img src="{user_avatar_url}" class="avatar" alt="Avatar">
                <div class="badge">✓</div>
            </div>
            <div class="content">
                <h1 class="title">Verificação concluída</h1>
                <div class="timestamp">{data_hora_atual}</div>
                <p class="description">
                    Sua identidade foi confirmada com segurança e vinculada ao servidor <strong>{guild_name}</strong>. O cargo de acesso já foi processado.
                </p>
                <div class="server-box">
                    <div class="server-info">
                        {server_icon_html}
                        <div class="server-name">{guild_name}</div>
                    </div>
                    <div class="verified-tag">✓ Verified</div>
                </div>
                <a href="discord://" class="btn">Acessar servidor</a>
            </div>
        </div>
    </body>
    </html>
    """
