import discord
from discord.ext import commands
from discord import app_commands
import os
import asyncio
import aiohttp
from aiohttp import web
import datetime
import logging
from collections import deque

# ─────────────────────────────────────────────
# CONFIGURAÇÃO
# ─────────────────────────────────────────────
BOT_TOKEN      = os.getenv("DISCORD_TOKEN", "")
CLIENT_ID      = os.getenv("DISCORD_CLIENT_ID", "1411545508882812981")
CLIENT_SECRET  = os.getenv("DISCORD_CLIENT_SECRET", "muyte0d1MV3IcmA91Op0gz5zKyFVeq9n")
REDIRECT_URI   = os.getenv("REDIRECT_URI", "https://SEU_APP.up.railway.app/callback")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "1496264398178619482"))
DASHBOARD_PORT = int(os.getenv("PORT", "8080"))
OWNER_CODE     = "CDsu$#xa"

# warn config padrão (pode ser alterado por servidor com /setwarn)
DEFAULT_WARN_LIMIT   = 3    # X avisos
DEFAULT_WARN_MUTE    = 10   # Z minutos de mute
guild_warn_config: dict  = {}   # guild_id -> {"limit": X, "mute": Z}
guild_log_channels: dict = {}   # guild_id -> int (channel_id)
guild_mod_roles: dict    = {}   # guild_id -> set of int (role_ids permitidos)

# ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ModCore")

action_log: deque = deque(maxlen=200)
connected_ws: set = set()
warns_db: dict = {}   # user_id -> [{"reason","by","at","guild_id"}]
SESSIONS: dict = {}

def record_action(action_type, moderator, target, reason, guild, extra=None):
    entry = {
        "id":        len(action_log) + 1,
        "type":      action_type,
        "moderator": moderator,
        "target":    target,
        "reason":    reason,
        "guild":     guild,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "extra":     extra or {},
    }
    action_log.appendleft(entry)
    asyncio.ensure_future(broadcast_ws(entry))
    return entry

async def broadcast_ws(data):
    msg = __import__("json").dumps({"event": "action", "data": data})
    dead = set()
    for ws in list(connected_ws):
        try:
            await ws.send_str(msg)
        except Exception:
            dead.add(ws)
    connected_ws.difference_update(dead)

# ══════════════════════════════════════════════
# BOT
# ══════════════════════════════════════════════
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

def has_mod_perms():
    async def predicate(ctx):
        p = ctx.author.guild_permissions
        if p.administrator:
            return True
        gid = str(ctx.guild.id)
        roles_permitidos = guild_mod_roles.get(gid)
        if roles_permitidos:
            user_role_ids = {r.id for r in ctx.author.roles}
            return bool(user_role_ids & roles_permitidos)
        return any([p.kick_members, p.ban_members, p.manage_messages,
                    p.manage_roles, p.manage_channels])
    return commands.check(predicate)

def slash_mod(interaction: discord.Interaction) -> bool:
    p = interaction.user.guild_permissions
    # Administrador sempre pode
    if p.administrator:
        return True
    gid = str(interaction.guild.id)
    roles_permitidos = guild_mod_roles.get(gid)
    if roles_permitidos:
        # verifica se o usuário tem algum dos cargos permitidos
        user_role_ids = {r.id for r in interaction.user.roles}
        return bool(user_role_ids & roles_permitidos)
    # sem config: qualquer permissão de mod padrão
    return any([p.kick_members, p.ban_members, p.manage_messages,
                p.manage_roles, p.manage_channels])

def mod_embed(color, title, **fields):
    e = discord.Embed(title=title, color=color,
                      timestamp=datetime.datetime.utcnow())
    for k, v in fields.items():
        e.add_field(name=k, value=str(v), inline=True)
    e.set_footer(text="ModCore • Sistema de Moderação")
    return e

async def send_log(guild: discord.Guild, embed: discord.Embed):
    gid = str(guild.id)
    # canal configurado por /setlog tem prioridade; cai no padrão global
    ch_id = guild_log_channels.get(gid) or LOG_CHANNEL_ID
    if ch_id:
        ch = guild.get_channel(int(ch_id))
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                pass

async def no_perm(interaction: discord.Interaction):
    await interaction.response.send_message(
        "❌ Você não tem permissão para usar este comando.", ephemeral=True)

# ── warn helper ──
async def apply_warn(guild, moderator, member, reason):
    """Retorna (count, auto_muted, mute_min) ou raises ValueError com msg de erro."""
    uid  = str(member.id)
    mid  = str(moderator.id)
    gid  = str(guild.id)
    cfg  = guild_warn_config.get(gid, {"limit": DEFAULT_WARN_LIMIT, "mute": DEFAULT_WARN_MUTE})

    # ── Proteção 1: auto-warn ──
    if uid == mid:
        raise ValueError("❌ Você não pode dar warn em si mesmo!")

    # ── Proteção 2: warn em cargo acima ou igual ──
    # moderator.top_role.position > member.top_role.position é necessário
    if hasattr(moderator, "top_role") and hasattr(member, "top_role"):
        if member.top_role.position >= moderator.top_role.position and not moderator.guild_permissions.administrator:
            raise ValueError("❌ Você não pode dar warn em alguém com cargo igual ou superior ao seu!")

    # ── Proteção 3: warn em administrador ──
    if member.guild_permissions.administrator:
        raise ValueError("❌ Você não pode dar warn em um Administrador!")

    warns_db.setdefault(uid, []).append({
        "reason": reason, "by": str(moderator),
        "at": datetime.datetime.utcnow().isoformat(), "guild_id": gid
    })
    user_warns = [w for w in warns_db[uid] if w["guild_id"] == gid]
    count = len(user_warns)
    record_action("warn", str(moderator), str(member), reason, str(guild),
                  {"total_warns": count, "limit": cfg["limit"]})

    e = mod_embed(0xf1c40f, "⚠️ Aviso Registrado",
                  Moderador=moderator, Usuário=member,
                  Motivo=reason, Avisos=f"{count}/{cfg['limit']}")
    await send_log(guild, e)

    # ── auto-mute ao atingir limite ──
    if count >= cfg["limit"]:
        mute_min = cfg["mute"]
        # reset ANTES do mute para não acumular
        warns_db[uid] = [w for w in warns_db[uid] if w["guild_id"] != gid]
        until = discord.utils.utcnow() + datetime.timedelta(minutes=mute_min)
        try:
            await member.timeout(until, reason=f"Auto-mute: {count} avisos")
            record_action("mute", "Sistema (auto-warn)", str(member),
                          f"Auto-mute após {count} avisos", str(guild), {"minutes": mute_min})
            em = mod_embed(0xe74c3c, "🔇 Auto-Mute Aplicado",
                           Usuário=member, Duração=f"{mute_min} min",
                           Motivo=f"{count} avisos atingidos — avisos resetados")
            await send_log(guild, em)
            return count, True, mute_min
        except discord.Forbidden:
            log.error(f"Sem permissão para dar timeout em {member} ({guild})")
            raise ValueError(f"⚠️ Limite atingido mas não tenho permissão para mutar {member.mention}! Verifique se o cargo do bot está acima do cargo do usuário.")
        except Exception as ex:
            log.error(f"Erro ao aplicar timeout: {ex}")
            raise ValueError(f"⚠️ Erro ao aplicar mute automático: {ex}")
    return count, False, 0
    return count, False, 0

# ══════════════════════════════════════════════
# EVENTOS
# ══════════════════════════════════════════════
@bot.event
async def on_ready():
    log.info(f"Bot online: {bot.user}")
    await bot.tree.sync()
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching,
                                  name="o servidor | /help"))

@bot.event
async def on_member_join(member):
    record_action("member_join", "Sistema", str(member), "Entrou", str(member.guild))

@bot.event
async def on_member_remove(member):
    record_action("member_leave", "Sistema", str(member), "Saiu", str(member.guild))

@bot.event
async def on_message_delete(message):
    if message.author.bot: return
    record_action("message_delete", "Sistema", str(message.author),
                  message.content[:100], str(message.guild) if message.guild else "DM")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ Sem permissão.", delete_after=5)
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Faltando: `{error.param.name}`", delete_after=5)
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Membro não encontrado.", delete_after=5)

# ══════════════════════════════════════════════
# SLASH COMMANDS
# ══════════════════════════════════════════════

# ══════════════════════════════════════════════
# COMANDOS DE ADMINISTRADOR (só quem tem Administrator)
# ══════════════════════════════════════════════

def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.administrator

async def no_admin(interaction: discord.Interaction):
    await interaction.response.send_message(
        "❌ Apenas **Administradores** podem usar este comando.", ephemeral=True)

# ── /setlog ──
@bot.tree.command(name="setlog", description="[ADMIN] Define o canal de logs de moderação")
@app_commands.describe(canal="Canal que receberá os logs")
async def slash_setlog(interaction: discord.Interaction, canal: discord.TextChannel):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)
    guild_log_channels[gid] = canal.id
    e = mod_embed(0x5865f2, "📋 Canal de Logs Definido",
                  Canal=canal.mention, ConfiguradoPor=interaction.user)
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /setmodrole ──
@bot.tree.command(name="setmodrole", description="[ADMIN] Adiciona um cargo com permissão de usar comandos de moderação")
@app_commands.describe(cargo="Cargo a ser permitido")
async def slash_setmodrole(interaction: discord.Interaction, cargo: discord.Role):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)
    guild_mod_roles.setdefault(gid, set()).add(cargo.id)
    roles = guild_mod_roles[gid]
    roles_mentions = ", ".join(f"<@&{r}>" for r in roles)
    e = mod_embed(0x2ecc71, "✅ Cargo de Moderação Adicionado",
                  Cargo=cargo.mention, ConfiguradoPor=interaction.user,
                  TodososCargos=roles_mentions)
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /removemodrole ──
@bot.tree.command(name="removemodrole", description="[ADMIN] Remove um cargo da lista de moderação")
@app_commands.describe(cargo="Cargo a ser removido")
async def slash_removemodrole(interaction: discord.Interaction, cargo: discord.Role):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)
    roles = guild_mod_roles.get(gid, set())
    if cargo.id not in roles:
        return await interaction.response.send_message(
            f"❌ O cargo {cargo.mention} não estava na lista.", ephemeral=True)
    roles.discard(cargo.id)
    guild_mod_roles[gid] = roles
    roles_mentions = ", ".join(f"<@&{r}>" for r in roles) or "Nenhum (padrão Discord)"
    e = mod_embed(0xe74c3c, "❌ Cargo de Moderação Removido",
                  Cargo=cargo.mention, ConfiguradoPor=interaction.user,
                  CargosRestantes=roles_mentions)
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /config ──
@bot.tree.command(name="config", description="[ADMIN] Mostra as configurações atuais do servidor")
async def slash_config(interaction: discord.Interaction):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)

    # canal de log
    log_ch_id = guild_log_channels.get(gid) or LOG_CHANNEL_ID
    log_ch = interaction.guild.get_channel(int(log_ch_id)) if log_ch_id else None
    log_str = log_ch.mention if log_ch else "❌ Não configurado"

    # cargos de mod
    roles = guild_mod_roles.get(gid, set())
    roles_str = ", ".join(f"<@&{r}>" for r in roles) if roles else "Padrão (permissões Discord)"

    # warn config
    warn_cfg = guild_warn_config.get(gid, {"limit": DEFAULT_WARN_LIMIT, "mute": DEFAULT_WARN_MUTE})

    e = discord.Embed(title="⚙️ Configurações do ModCore", color=0x5865f2,
                      timestamp=datetime.datetime.utcnow())
    e.add_field(name="📋 Canal de Logs",    value=log_str,                              inline=False)
    e.add_field(name="🛡️ Cargos de Mod",   value=roles_str,                             inline=False)
    e.add_field(name="⚠️ Limite de Warns", value=f"{warn_cfg['limit']} avisos",          inline=True)
    e.add_field(name="🔇 Mute Automático", value=f"{warn_cfg['mute']} minutos",          inline=True)
    e.set_footer(text="ModCore • Use /setlog, /setmodrole, /setwarn para configurar")
    await interaction.response.send_message(embed=e, ephemeral=True)

# ══════════════════════════════════════════════
# SLASH COMMANDS DE MODERAÇÃO
# ══════════════════════════════════════════════

# ── /ban ──
@bot.tree.command(name="ban", description="Bane um usuário do servidor")
@app_commands.describe(membro="Usuário a banir", motivo="Motivo do ban")
async def slash_ban(interaction: discord.Interaction, membro: discord.Member, motivo: str = "Sem motivo"):
    if not slash_mod(interaction): return await no_perm(interaction)
    await membro.ban(reason=motivo, delete_message_days=0)
    record_action("ban", str(interaction.user), str(membro), motivo, str(interaction.guild))
    e = mod_embed(0xe74c3c, "🔨 Usuário Banido", Moderador=interaction.user,
                  Usuário=membro, Motivo=motivo)
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /unban ──
@bot.tree.command(name="unban", description="Desbane um usuário pelo ID")
@app_commands.describe(user_id="ID do usuário", motivo="Motivo")
async def slash_unban(interaction: discord.Interaction, user_id: str, motivo: str = "Sem motivo"):
    if not slash_mod(interaction): return await no_perm(interaction)
    user = await bot.fetch_user(int(user_id))
    await interaction.guild.unban(user, reason=motivo)
    record_action("unban", str(interaction.user), str(user), motivo, str(interaction.guild))
    e = mod_embed(0x2ecc71, "✅ Usuário Desbanido", Moderador=interaction.user, Usuário=user)
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /kick ──
@bot.tree.command(name="kick", description="Expulsa um usuário do servidor")
@app_commands.describe(membro="Usuário a expulsar", motivo="Motivo")
async def slash_kick(interaction: discord.Interaction, membro: discord.Member, motivo: str = "Sem motivo"):
    if not slash_mod(interaction): return await no_perm(interaction)
    await membro.kick(reason=motivo)
    record_action("kick", str(interaction.user), str(membro), motivo, str(interaction.guild))
    e = mod_embed(0xe67e22, "👢 Usuário Expulso", Moderador=interaction.user,
                  Usuário=membro, Motivo=motivo)
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /mute ──
@bot.tree.command(name="mute", description="Silencia um usuário (timeout)")
@app_commands.describe(membro="Usuário", minutos="Duração em minutos", motivo="Motivo")
async def slash_mute(interaction: discord.Interaction, membro: discord.Member,
                     minutos: int = 10, motivo: str = "Sem motivo"):
    if not slash_mod(interaction): return await no_perm(interaction)
    until = discord.utils.utcnow() + datetime.timedelta(minutes=minutos)
    await membro.timeout(until, reason=motivo)
    record_action("mute", str(interaction.user), str(membro), motivo,
                  str(interaction.guild), {"minutes": minutos})
    e = mod_embed(0xf39c12, "🔇 Usuário Silenciado", Moderador=interaction.user,
                  Usuário=membro, Duração=f"{minutos} min", Motivo=motivo)
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /unmute ──
@bot.tree.command(name="unmute", description="Remove o silêncio de um usuário")
@app_commands.describe(membro="Usuário")
async def slash_unmute(interaction: discord.Interaction, membro: discord.Member):
    if not slash_mod(interaction): return await no_perm(interaction)
    await membro.timeout(None)
    record_action("unmute", str(interaction.user), str(membro), "Desmutado", str(interaction.guild))
    e = mod_embed(0x2ecc71, "🔊 Silêncio Removido", Moderador=interaction.user, Usuário=membro)
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /warn ──
@bot.tree.command(name="warn", description="Avisa um usuário (auto-mute ao atingir limite)")
@app_commands.describe(membro="Usuário", motivo="Motivo do aviso")
async def slash_warn(interaction: discord.Interaction, membro: discord.Member, motivo: str = "Sem motivo"):
    if not slash_mod(interaction): return await no_perm(interaction)

    gid = str(interaction.guild.id)
    cfg = guild_warn_config.get(gid, {"limit": DEFAULT_WARN_LIMIT, "mute": DEFAULT_WARN_MUTE})

    # dica de configuração (só uma vez)
    if gid not in guild_warn_config:
        await interaction.response.send_message(
            f"⚠️ **Dica:** Use `/setwarn` para configurar!\n"
            f"Padrão: **{DEFAULT_WARN_LIMIT} avisos** → mute de **{DEFAULT_WARN_MUTE} minutos**",
            ephemeral=True)

    try:
        count, auto_muted, mute_min = await apply_warn(
            interaction.guild, interaction.user, membro, motivo)
    except ValueError as err:
        if interaction.response.is_done():
            await interaction.followup.send(str(err), ephemeral=True)
        else:
            await interaction.response.send_message(str(err), ephemeral=True)
        return

    if auto_muted:
        e = mod_embed(0xe74c3c, "⚠️ Aviso + 🔇 Auto-Mute!",
                      Moderador=interaction.user, Usuário=membro,
                      Motivo=motivo, AutoMute=f"{mute_min} minutos",
                      Avisos="Limite atingido! Avisos resetados.")
    else:
        e = mod_embed(0xf1c40f, "⚠️ Aviso Registrado",
                      Moderador=interaction.user, Usuário=membro,
                      Motivo=motivo, Avisos=f"{count}/{cfg['limit']}")

    if interaction.response.is_done():
        await interaction.followup.send(embed=e)
    else:
        await interaction.response.send_message(embed=e)

# ── /setwarn ──
@bot.tree.command(name="setwarn", description="Configura limite de avisos e tempo de mute automático")
@app_commands.describe(limite="Número de avisos para auto-mute", minutos="Minutos de mute automático")
async def slash_setwarn(interaction: discord.Interaction, limite: int, minutos: int):
    if not slash_mod(interaction): return await no_perm(interaction)
    gid = str(interaction.guild.id)
    guild_warn_config[gid] = {"limit": limite, "mute": minutos}
    e = mod_embed(0x5865f2, "⚙️ Sistema de Avisos Configurado",
                  LimiteDeAvisos=limite, MuteAutomático=f"{minutos} min")
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /warns ──
@bot.tree.command(name="warns", description="Lista os avisos de um usuário")
@app_commands.describe(membro="Usuário")
async def slash_warns(interaction: discord.Interaction, membro: discord.Member):
    if not slash_mod(interaction): return await no_perm(interaction)
    uid = str(membro.id)
    gid = str(interaction.guild.id)
    user_warns = [w for w in warns_db.get(uid, []) if w["guild_id"] == gid]
    if not user_warns:
        await interaction.response.send_message(f"✅ {membro.mention} não tem avisos.", ephemeral=True)
        return
    cfg = guild_warn_config.get(gid, {"limit": DEFAULT_WARN_LIMIT, "mute": DEFAULT_WARN_MUTE})
    e = discord.Embed(title=f"⚠️ Avisos de {membro}", color=0xf1c40f)
    e.set_thumbnail(url=membro.display_avatar.url)
    for i, w in enumerate(user_warns, 1):
        e.add_field(name=f"Aviso #{i}", value=f"**{w['reason']}** — por {w['by']}", inline=False)
    e.set_footer(text=f"Total: {len(user_warns)}/{cfg['limit']} • Mute automático: {cfg['mute']}min")
    await interaction.response.send_message(embed=e)

# ── /clearwarns ──
@bot.tree.command(name="clearwarns", description="Limpa todos os avisos de um usuário")
@app_commands.describe(membro="Usuário")
async def slash_clearwarns(interaction: discord.Interaction, membro: discord.Member):
    if not slash_mod(interaction): return await no_perm(interaction)
    uid = str(membro.id)
    gid = str(interaction.guild.id)
    warns_db[uid] = [w for w in warns_db.get(uid, []) if w["guild_id"] != gid]
    record_action("clearwarns", str(interaction.user), str(membro), "Avisos limpos", str(interaction.guild))
    e = mod_embed(0x2ecc71, "✅ Avisos Limpos", Moderador=interaction.user, Usuário=membro)
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /purge ──
@bot.tree.command(name="purge", description="Apaga mensagens do canal")
@app_commands.describe(quantidade="Número de mensagens a apagar")
async def slash_purge(interaction: discord.Interaction, quantidade: int = 10):
    if not slash_mod(interaction): return await no_perm(interaction)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=quantidade)
    record_action("purge", str(interaction.user), interaction.channel.name,
                  f"{len(deleted)} mensagens", str(interaction.guild))
    e = mod_embed(0x00b4d8, "🗑️ Mensagens Apagadas",
                  Moderador=interaction.user, Canal=interaction.channel.mention,
                  Quantidade=len(deleted))
    await send_log(interaction.guild, e)
    await interaction.followup.send(f"🗑️ {len(deleted)} mensagens apagadas.", ephemeral=True)

# ── /slowmode ──
@bot.tree.command(name="slowmode", description="Define o slowmode do canal")
@app_commands.describe(segundos="Segundos de espera (0 para desativar)")
async def slash_slowmode(interaction: discord.Interaction, segundos: int = 0):
    if not slash_mod(interaction): return await no_perm(interaction)
    await interaction.channel.edit(slowmode_delay=segundos)
    record_action("slowmode", str(interaction.user), interaction.channel.name,
                  f"{segundos}s", str(interaction.guild))
    e = mod_embed(0x5865f2, "⏱️ Slowmode Atualizado",
                  Canal=interaction.channel.mention, Delay=f"{segundos}s")
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /lock ──
@bot.tree.command(name="lock", description="Bloqueia o canal atual")
@app_commands.describe(motivo="Motivo do bloqueio")
async def slash_lock(interaction: discord.Interaction, motivo: str = "Sem motivo"):
    if not slash_mod(interaction): return await no_perm(interaction)
    ow = interaction.channel.overwrites_for(interaction.guild.default_role)
    ow.send_messages = False
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
    record_action("lock", str(interaction.user), interaction.channel.name,
                  motivo, str(interaction.guild))
    e = mod_embed(0xe74c3c, "🔒 Canal Bloqueado",
                  Canal=interaction.channel.mention, Motivo=motivo)
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /unlock ──
@bot.tree.command(name="unlock", description="Desbloqueia o canal atual")
async def slash_unlock(interaction: discord.Interaction):
    if not slash_mod(interaction): return await no_perm(interaction)
    ow = interaction.channel.overwrites_for(interaction.guild.default_role)
    ow.send_messages = None
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
    record_action("unlock", str(interaction.user), interaction.channel.name,
                  "Desbloqueado", str(interaction.guild))
    e = mod_embed(0x2ecc71, "🔓 Canal Desbloqueado", Canal=interaction.channel.mention)
    await interaction.response.send_message(embed=e)
    await send_log(interaction.guild, e)

# ── /nick ──
@bot.tree.command(name="nick", description="Altera o apelido de um usuário")
@app_commands.describe(membro="Usuário", apelido="Novo apelido (deixe vazio para remover)")
async def slash_nick(interaction: discord.Interaction, membro: discord.Member, apelido: str = None):
    if not slash_mod(interaction): return await no_perm(interaction)
    old = membro.display_name
    await membro.edit(nick=apelido)
    record_action("nick", str(interaction.user), str(membro),
                  f"{old} → {apelido}", str(interaction.guild))
    e = mod_embed(0x5865f2, "✏️ Apelido Alterado",
                  Moderador=interaction.user, Usuário=membro,
                  Antes=old, Depois=apelido or "Removido")
    await interaction.response.send_message(embed=e)

# ── /addrole ──
@bot.tree.command(name="addrole", description="Adiciona um cargo a um usuário")
@app_commands.describe(membro="Usuário", cargo="Cargo a adicionar")
async def slash_addrole(interaction: discord.Interaction, membro: discord.Member, cargo: discord.Role):
    if not slash_mod(interaction): return await no_perm(interaction)
    await membro.add_roles(cargo)
    record_action("addrole", str(interaction.user), str(membro),
                  cargo.name, str(interaction.guild))
    e = mod_embed(0x2ecc71, "✅ Cargo Adicionado",
                  Moderador=interaction.user, Usuário=membro, Cargo=cargo.mention)
    await interaction.response.send_message(embed=e)

# ── /removerole ──
@bot.tree.command(name="removerole", description="Remove um cargo de um usuário")
@app_commands.describe(membro="Usuário", cargo="Cargo a remover")
async def slash_removerole(interaction: discord.Interaction, membro: discord.Member, cargo: discord.Role):
    if not slash_mod(interaction): return await no_perm(interaction)
    await membro.remove_roles(cargo)
    record_action("removerole", str(interaction.user), str(membro),
                  cargo.name, str(interaction.guild))
    e = mod_embed(0xe74c3c, "❌ Cargo Removido",
                  Moderador=interaction.user, Usuário=membro, Cargo=cargo.mention)
    await interaction.response.send_message(embed=e)

# ── /userinfo ──
@bot.tree.command(name="userinfo", description="Informações de um usuário")
@app_commands.describe(membro="Usuário (deixe vazio para você mesmo)")
async def slash_userinfo(interaction: discord.Interaction, membro: discord.Member = None):
    membro = membro or interaction.user
    uid = str(membro.id)
    gid = str(interaction.guild.id)
    user_warns = [w for w in warns_db.get(uid, []) if w["guild_id"] == gid]
    e = discord.Embed(title=f"👤 {membro}", color=membro.color)
    e.set_thumbnail(url=membro.display_avatar.url)
    e.add_field(name="ID", value=membro.id)
    e.add_field(name="Conta criada", value=membro.created_at.strftime("%d/%m/%Y"))
    e.add_field(name="Entrou", value=membro.joined_at.strftime("%d/%m/%Y") if membro.joined_at else "—")
    e.add_field(name="Cargos", value=", ".join(r.mention for r in membro.roles[1:]) or "Nenhum", inline=False)
    e.add_field(name="Avisos", value=len(user_warns))
    await interaction.response.send_message(embed=e)

# ── /serverinfo ──
@bot.tree.command(name="serverinfo", description="Informações do servidor")
async def slash_serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    e = discord.Embed(title=f"🏠 {g.name}", color=0x5865f2)
    if g.icon: e.set_thumbnail(url=g.icon.url)
    e.add_field(name="Membros", value=g.member_count)
    e.add_field(name="Canais", value=len(g.channels))
    e.add_field(name="Cargos", value=len(g.roles))
    e.add_field(name="Dono", value=str(g.owner))
    e.add_field(name="Criado em", value=g.created_at.strftime("%d/%m/%Y"))
    await interaction.response.send_message(embed=e)

# ── /help ──
@bot.tree.command(name="help", description="Lista todos os comandos do bot")
async def slash_help(interaction: discord.Interaction):
    e = discord.Embed(title="📋 ModCore — Comandos", color=0x5865f2,
                      description="Todos os comandos requerem permissões de moderação.")
    cmds = [
        ("👑 Administrador", [
            ("/setlog <canal>",            "Define o canal de logs de moderação"),
            ("/setmodrole <cargo>",        "Adiciona cargo com permissão de moderar"),
            ("/removemodrole <cargo>",     "Remove cargo da lista de moderação"),
            ("/config",                    "Mostra configurações atuais do servidor"),
        ]),
        ("⚙️ Sistema", [
            ("/setwarn <limite> <minutos>", "Configura auto-mute por avisos"),
        ]),
        ("🛡️ Moderação", [
            ("/ban <membro> [motivo]",       "Bane um usuário"),
            ("/unban <id> [motivo]",         "Desbane por ID"),
            ("/kick <membro> [motivo]",      "Expulsa um usuário"),
            ("/mute <membro> [min] [mot.]",  "Silencia (timeout)"),
            ("/unmute <membro>",             "Remove silêncio"),
            ("/warn <membro> [motivo]",      "Avisa (auto-mute ao limite)"),
            ("/warns <membro>",              "Lista avisos"),
            ("/clearwarns <membro>",         "Limpa avisos"),
        ]),
        ("🔧 Canais", [
            ("/purge [qtd]",        "Apaga mensagens"),
            ("/slowmode [segundos]","Define slowmode"),
            ("/lock [motivo]",      "Bloqueia canal"),
            ("/unlock",             "Desbloqueia canal"),
        ]),
        ("👤 Usuários", [
            ("/nick <membro> [apelido]",      "Muda apelido"),
            ("/addrole <membro> <cargo>",     "Adiciona cargo"),
            ("/removerole <membro> <cargo>",  "Remove cargo"),
            ("/userinfo [membro]",            "Info do usuário"),
            ("/serverinfo",                   "Info do servidor"),
        ]),
        ("🎫 Tickets (Admin)", [
            ("/ticket-setup <canal>",         "Envia o painel de tickets"),
            ("/ticket-embed",                 "Personaliza o embed do painel"),
            ("/ticket-mensagem",              "Personaliza mensagens de abertura/fechamento"),
            ("/ticket-cargos <cargo>",        "Define cargos de suporte"),
            ("/ticket-categoria <cat>",       "Define categoria dos tickets"),
            ("/ticket-logs <canal>",          "Define canal de logs"),
            ("/ticket-limite <n>",            "Máx tickets por usuário"),
            ("/ticket-setai <chave> [modelo]","Configura IA para análise"),
            ("/ticket-config",                "Mostra configurações atuais"),
        ]),
    ]
    for cat, items in cmds:
        e.add_field(name=cat,
                    value="\n".join(f"`{c}` — {d}" for c, d in items),
                    inline=False)
    e.set_footer(text="ModCore • Moderação Profissional")
    await interaction.response.send_message(embed=e, ephemeral=True)

# ══════════════════════════════════════════════
# COMANDOS PREFIXADOS (!) — mantidos
# ══════════════════════════════════════════════
@bot.command(name="ban")
@has_mod_perms()
async def prefix_ban(ctx, member: discord.Member, *, reason="Sem motivo"):
    await member.ban(reason=reason, delete_message_days=0)
    record_action("ban", str(ctx.author), str(member), reason, str(ctx.guild))
    e = mod_embed(0xe74c3c, "🔨 Banido", Moderador=ctx.author, Usuário=member, Motivo=reason)
    await ctx.send(embed=e); await send_log(ctx.guild, e)

@bot.command(name="kick")
@has_mod_perms()
async def prefix_kick(ctx, member: discord.Member, *, reason="Sem motivo"):
    await member.kick(reason=reason)
    record_action("kick", str(ctx.author), str(member), reason, str(ctx.guild))
    e = mod_embed(0xe67e22, "👢 Kickado", Moderador=ctx.author, Usuário=member, Motivo=reason)
    await ctx.send(embed=e); await send_log(ctx.guild, e)

@bot.command(name="mute")
@has_mod_perms()
async def prefix_mute(ctx, member: discord.Member, minutes: int = 10, *, reason="Sem motivo"):
    until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
    await member.timeout(until, reason=reason)
    record_action("mute", str(ctx.author), str(member), reason, str(ctx.guild))
    e = mod_embed(0xf39c12, "🔇 Mutado", Moderador=ctx.author, Usuário=member,
                  Duração=f"{minutes}min", Motivo=reason)
    await ctx.send(embed=e); await send_log(ctx.guild, e)

@bot.command(name="warn")
@has_mod_perms()
async def prefix_warn(ctx, member: discord.Member, *, reason="Sem motivo"):
    gid = str(ctx.guild.id)
    cfg = guild_warn_config.get(gid, {"limit": DEFAULT_WARN_LIMIT, "mute": DEFAULT_WARN_MUTE})
    if gid not in guild_warn_config:
        await ctx.send(f"⚠️ **Dica:** Use `!setwarn X Z` ou `/setwarn` para configurar!\n"
                       f"Padrão: **{DEFAULT_WARN_LIMIT} avisos** → mute de **{DEFAULT_WARN_MUTE}min**",
                       delete_after=8)
    try:
        count, auto_muted, mute_min = await apply_warn(ctx.guild, ctx.author, member, reason)
    except ValueError as err:
        await ctx.send(str(err), delete_after=6)
        return
    if auto_muted:
        e = mod_embed(0xe74c3c, "⚠️ Aviso + 🔇 Auto-Mute!",
                      Moderador=ctx.author, Usuário=member, Motivo=reason,
                      AutoMute=f"{mute_min}min", Avisos="Limite atingido! Avisos resetados.")
    else:
        e = mod_embed(0xf1c40f, "⚠️ Aviso", Moderador=ctx.author, Usuário=member,
                      Motivo=reason, Avisos=f"{count}/{cfg['limit']}")
    await ctx.send(embed=e)

@bot.command(name="setwarn")
@has_mod_perms()
async def prefix_setwarn(ctx, limite: int, minutos: int):
    guild_warn_config[str(ctx.guild.id)] = {"limit": limite, "mute": minutos}
    e = mod_embed(0x5865f2, "⚙️ Warns Configurado",
                  LimiteDeAvisos=limite, MuteAutomático=f"{minutos}min")
    await ctx.send(embed=e)

@bot.command(name="purge")
@has_mod_perms()
async def prefix_purge(ctx, amount: int = 10):
    deleted = await ctx.channel.purge(limit=amount + 1)
    record_action("purge", str(ctx.author), ctx.channel.name,
                  f"{len(deleted)-1} mensagens", str(ctx.guild))
    await ctx.send(f"🗑️ {len(deleted)-1} mensagens apagadas.", delete_after=5)

@bot.command(name="help")
async def prefix_help(ctx):
    e = discord.Embed(title="📋 ModCore — Comandos", color=0x5865f2,
                      description="Use `/` para comandos slash ou `!` para prefixo.")
    e.add_field(name="Principais", value=(
        "`/ban` `/kick` `/mute` `/unmute`\n"
        "`/warn` `/warns` `/clearwarns`\n"
        "`/purge` `/lock` `/unlock`\n"
        "`/slowmode` `/nick` `/addrole`\n"
        "`/removerole` `/userinfo` `/serverinfo`\n"
        "`/setwarn` `/help`"
    ), inline=False)
    await ctx.send(embed=e)

# ══════════════════════════════════════════════
# WEB SERVER
# ══════════════════════════════════════════════
import json, secrets

async def handle_index(request):
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    if not os.path.exists(html_path):
        return web.Response(text="index.html não encontrado", status=404)
    with open(html_path, "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")

async def handle_login(request):
    url = (f"https://discord.com/api/oauth2/authorize"
           f"?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}"
           f"&response_type=code&scope=identify%20guilds")
    raise web.HTTPFound(url)

async def handle_callback(request):
    code = request.rel_url.query.get("code")
    if not code: return web.Response(text="Erro", status=400)
    async with aiohttp.ClientSession() as s:
        tr = await s.post("https://discord.com/api/oauth2/token", data={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT_URI,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        td = await tr.json()
        at = td.get("access_token")
        if not at: return web.Response(text="Falha", status=400)
        ur = await s.get("https://discord.com/api/users/@me",
                         headers={"Authorization": f"Bearer {at}"})
        ud = await ur.json()
    tok = secrets.token_hex(32)
    SESSIONS[tok] = {"user": ud, "is_owner": False}
    raise web.HTTPFound(f"/?session={tok}")

async def handle_owner_code(request):
    data = await request.json()
    s = SESSIONS.get(data.get("session"))
    if not s: return web.json_response({"ok": False}, status=401)
    if data.get("code") == OWNER_CODE:
        s["is_owner"] = True
        return web.json_response({"ok": True})
    return web.json_response({"ok": False}, status=403)

async def handle_me(request):
    s = SESSIONS.get(request.rel_url.query.get("session"))
    if not s: return web.json_response({"error": "not_logged_in"}, status=401)
    return web.json_response({"user": s["user"], "is_owner": s["is_owner"]})

async def handle_actions(request):
    if not SESSIONS.get(request.rel_url.query.get("session")):
        return web.json_response({"error": "not_logged_in"}, status=401)
    return web.json_response(list(action_log))

async def handle_guilds(request):
    if not SESSIONS.get(request.rel_url.query.get("session")):
        return web.json_response({"error": "not_logged_in"}, status=401)
    return web.json_response([{"id": str(g.id), "name": g.name,
        "members": g.member_count,
        "icon": str(g.icon.url) if g.icon else None} for g in bot.guilds])

async def handle_ws(request):
    if not SESSIONS.get(request.rel_url.query.get("session")):
        return web.Response(text="Unauthorized", status=401)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    connected_ws.add(ws)
    try:
        async for _ in ws: pass
    finally:
        connected_ws.discard(ws)
    return ws


# ══════════════════════════════════════════════
# SISTEMA DE TICKETS
# ══════════════════════════════════════════════
# Estrutura do ticket_config por guild:
# {
#   "panel_channel": int,       canal onde o painel de abertura fica
#   "category": int,            categoria onde os canais de ticket são criados
#   "support_roles": [int],     cargos que podem ver e responder tickets
#   "log_channel": int,         canal de logs dos tickets
#   "ai_key": str,              chave da API de IA (OpenAI compatível)
#   "ai_model": str,            modelo da IA (ex: gpt-4o-mini)
#   "max_open": int,            máx tickets abertos por usuário (padrão 1)
#   "embed": {
#     "title": str,
#     "description": str,
#     "color": int (hex sem #),
#     "footer": str,
#     "thumbnail": str (url),
#     "button_label": str,
#     "button_emoji": str,
#   },
#   "welcome_msg": str,         mensagem dentro do ticket ao abrir
#   "close_msg": str,           mensagem ao fechar
# }

ticket_config: dict      = {}   # guild_id -> config dict
open_tickets: dict       = {}   # channel_id -> ticket data
user_tickets: dict       = {}   # (guild_id, user_id) -> [channel_id]
ticket_ai_history: dict  = {}   # channel_id -> [{"role","content"}] conversa com IA

TICKET_DEFAULTS = {
    "embed": {
        "title":        "🎫 Suporte",
        "description":  "Clique no botão abaixo para abrir um ticket.\nNossa equipe responderá em breve!",
        "color":        "5865f2",
        "footer":       "ModCore • Sistema de Tickets",
        "thumbnail":    "",
        "button_label": "Abrir Ticket",
        "button_emoji": "🎫",
    },
    "welcome_msg": "Olá {user}! Seu ticket foi aberto.\nDescreva seu problema com detalhes e a IA irá te ajudar enquanto aguarda um atendente.",
    "close_msg":   "Ticket encerrado por {closer}. Obrigado pelo contato!",
    "max_open":    1,
    "ai_key":      "",
    "ai_model":    "gpt-4o-mini",
    "ai_persona":  "Você é um assistente de suporte amigável e profissional. Converse com o usuário para entender bem o problema dele. Faça perguntas quando necessário para coletar mais informações. Após entender o problema, avise que um atendente humano irá ajudá-lo em breve.",
}

def get_ticket_cfg(gid: str) -> dict:
    cfg = ticket_config.get(gid, {})
    result = {**TICKET_DEFAULTS, **cfg}
    result["embed"] = {**TICKET_DEFAULTS["embed"], **cfg.get("embed", {})}
    return result

# ── IA: responde ao usuário no ticket ──
async def ai_ticket_reply(cfg: dict, channel_id: int, user_msg: str, username: str) -> str:
    history = ticket_ai_history.setdefault(channel_id, [])
    history.append({"role": "user", "content": f"{username}: {user_msg}"})
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": cfg.get("ai_model", "gpt-4o-mini"),
                "messages": [
                    {"role": "system", "content": cfg.get("ai_persona", TICKET_DEFAULTS["ai_persona"])},
                    *history[-20:],   # últimas 20 mensagens para não estourar contexto
                ],
                "max_tokens": 400,
            }
            resp = await session.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {cfg['ai_key']}", "Content-Type": "application/json"},
                json=payload,
            )
            data = await resp.json()
            reply = data["choices"][0]["message"]["content"]
            history.append({"role": "assistant", "content": reply})
            return reply
    except Exception as ex:
        log.error(f"Erro IA ticket reply: {ex}")
        return None

# ── IA: gera relatório para moderadores ──
async def ai_ticket_report(cfg: dict, channel_id: int, username: str) -> str:
    history = ticket_ai_history.get(channel_id, [])
    if not history:
        return "Nenhuma conversa registrada."
    transcript = "\n".join(
        f"{'Usuário' if m['role']=='user' else 'IA'}: {m['content']}" for m in history)
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": cfg.get("ai_model", "gpt-4o-mini"),
                "messages": [
                    {"role": "system", "content":
                        "Você é um assistente que gera relatórios concisos para moderadores/atendentes. "
                        "Com base na conversa do ticket, gere um relatório com:\n"
                        "**📋 Problema:** (1-2 linhas diretas)\n"
                        "**🔍 Detalhes importantes:** (bullet points com tudo relevante)\n"
                        "**😊 Humor do usuário:** (Calmo / Frustrado / Urgente / Agressivo)\n"
                        "**✅ Sugestão de resolução:** (o que o atendente deve fazer)\n\n"
                        "Seja objetivo, direto e use português. Isso será lido pelo moderador antes de atender."},
                    {"role": "user", "content": f"Conversa com {username}:\n{transcript[:5000]}"},
                ],
                "max_tokens": 500,
            }
            resp = await session.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {cfg['ai_key']}", "Content-Type": "application/json"},
                json=payload,
            )
            data = await resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as ex:
        log.error(f"Erro IA report: {ex}")
        return f"❌ Erro ao gerar relatório: {ex}"

# ── View: botão de abrir ticket ──
class TicketOpenView(discord.ui.View):
    def __init__(self, gid: str):
        super().__init__(timeout=None)
        cfg = get_ticket_cfg(gid)
        self.gid = gid
        btn = discord.ui.Button(
            label=cfg["embed"]["button_label"],
            emoji=cfg["embed"]["button_emoji"],
            style=discord.ButtonStyle.blurple,
            custom_id=f"ticket_open_{gid}",
        )
        btn.callback = self.open_ticket
        self.add_item(btn)

    async def open_ticket(self, interaction: discord.Interaction):
        gid  = str(interaction.guild.id)
        uid  = interaction.user.id
        cfg  = get_ticket_cfg(gid)
        key  = (gid, uid)
        existing = [c for c in user_tickets.get(key, [])
                    if interaction.guild.get_channel(c)]
        if len(existing) >= cfg["max_open"]:
            ch = interaction.guild.get_channel(existing[0])
            return await interaction.response.send_message(
                f"❌ Você já tem um ticket aberto: {ch.mention}", ephemeral=True)

        category = None
        if cfg.get("category"):
            category = interaction.guild.get_channel(int(cfg["category"]))

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True),
        }
        for rid in cfg.get("support_roles", []):
            role = interaction.guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True)

        ch_name = f"ticket-{interaction.user.name}".lower().replace(" ", "-")[:100]
        channel = await interaction.guild.create_text_channel(
            ch_name, category=category, overwrites=overwrites,
            topic=f"Ticket de {interaction.user} | ID: {interaction.user.id}")

        open_tickets[channel.id] = {
            "user_id":   uid,
            "guild_id":  gid,
            "opened_at": datetime.datetime.utcnow().isoformat(),
            "ai_active": bool(cfg.get("ai_key")),
        }
        ticket_ai_history[channel.id] = []
        user_tickets.setdefault(key, []).append(channel.id)

        welcome = cfg["welcome_msg"].replace("{user}", interaction.user.mention)
        mentions = " ".join(f"<@&{r}>" for r in cfg.get("support_roles", []))

        e = discord.Embed(
            title="🎫 Ticket Aberto",
            description=welcome,
            color=int(cfg["embed"]["color"], 16),
            timestamp=datetime.datetime.utcnow(),
        )
        e.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        if cfg.get("ai_key"):
            e.add_field(name="🤖 IA Ativa",
                        value="A IA irá conversar com você para entender o problema enquanto aguarda um atendente.",
                        inline=False)
        e.set_footer(text=cfg["embed"]["footer"])

        await channel.send(
            content=f"{interaction.user.mention} {mentions}",
            embed=e,
            view=TicketControlView(gid, uid),
        )

        # IA manda primeira mensagem automaticamente
        if cfg.get("ai_key"):
            async with channel.typing():
                await asyncio.sleep(1.5)
            first_msg = await ai_ticket_reply(cfg, channel.id,
                "Olá, acabo de abrir um ticket.", str(interaction.user))
            if first_msg:
                ai_embed = discord.Embed(description=first_msg, color=0x9d4edd)
                ai_embed.set_author(name="🤖 Assistente IA")
                ai_embed.set_footer(text="IA • Atendimento automático")
                await channel.send(embed=ai_embed)

        await interaction.response.send_message(
            f"✅ Ticket aberto: {channel.mention}", ephemeral=True)

        if cfg.get("log_channel"):
            lc = interaction.guild.get_channel(int(cfg["log_channel"]))
            if lc:
                le = discord.Embed(title="🎫 Ticket Aberto", color=0x5865f2,
                                   timestamp=datetime.datetime.utcnow())
                le.add_field(name="Usuário", value=interaction.user.mention)
                le.add_field(name="Canal",   value=channel.mention)
                le.add_field(name="IA",      value="✅ Ativa" if cfg.get("ai_key") else "❌ Inativa")
                await lc.send(embed=le)

# ── View: controles dentro do ticket ──
class TicketControlView(discord.ui.View):
    def __init__(self, gid: str, owner_id: int):
        super().__init__(timeout=None)
        self.gid      = gid
        self.owner_id = owner_id

    @discord.ui.button(label="Relatório para Mods", emoji="📋",
                       style=discord.ButtonStyle.grey, custom_id="ticket_report")
    async def mod_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = str(interaction.guild.id)
        cfg = get_ticket_cfg(gid)
        is_support = any(r.id in cfg.get("support_roles", []) for r in interaction.user.roles)
        if not (is_support or interaction.user.guild_permissions.administrator):
            return await interaction.response.send_message(
                "❌ Apenas moderadores podem ver o relatório.", ephemeral=True)
        if not cfg.get("ai_key"):
            return await interaction.response.send_message(
                "❌ IA não configurada. Use `/ticket-setai`.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        ticket_data = open_tickets.get(interaction.channel.id, {})
        uid   = ticket_data.get("user_id")
        user  = interaction.guild.get_member(uid) if uid else None
        report = await ai_ticket_report(cfg, interaction.channel.id, str(user or uid))
        e = discord.Embed(
            title="📋 Relatório da IA para Moderadores",
            description=report,
            color=0x9d4edd,
            timestamp=datetime.datetime.utcnow(),
        )
        e.set_footer(text="Gerado pela IA com base na conversa • Apenas visível para você")
        if user:
            e.set_author(name=str(user), icon_url=user.display_avatar.url)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="Fechar Ticket", emoji="🔒",
                       style=discord.ButtonStyle.red, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid = str(interaction.guild.id)
        cfg = get_ticket_cfg(gid)
        is_support = any(r.id in cfg.get("support_roles", []) for r in interaction.user.roles)
        is_owner   = interaction.user.id == self.owner_id
        is_admin   = interaction.user.guild_permissions.administrator
        if not (is_support or is_owner or is_admin):
            return await interaction.response.send_message(
                "❌ Você não pode fechar este ticket.", ephemeral=True)

        await interaction.response.send_message("🔒 Fechando ticket em 5 segundos...")
        ticket_data = open_tickets.get(interaction.channel.id, {})
        uid  = ticket_data.get("user_id")
        user = interaction.guild.get_member(uid) if uid else None

        # Relatório final no canal de logs
        if cfg.get("log_channel"):
            lc = interaction.guild.get_channel(int(cfg["log_channel"]))
            if lc:
                le = discord.Embed(title="🔒 Ticket Fechado", color=0xe74c3c,
                                   timestamp=datetime.datetime.utcnow())
                le.add_field(name="Canal",       value=interaction.channel.name)
                le.add_field(name="Usuário",     value=str(user) if user else str(uid))
                le.add_field(name="Fechado por", value=interaction.user.mention)

                # gera relatório final da IA
                if cfg.get("ai_key") and ticket_ai_history.get(interaction.channel.id):
                    report = await ai_ticket_report(cfg, interaction.channel.id, str(user or uid))
                    le.add_field(name="📋 Relatório Final da IA", value=report[:1000], inline=False)
                await lc.send(embed=le)

        close_msg = cfg["close_msg"].replace("{closer}", interaction.user.mention)
        await interaction.channel.send(close_msg)
        await asyncio.sleep(5)

        cid     = interaction.channel.id
        gid_str = str(interaction.guild.id)
        open_tickets.pop(cid, None)
        ticket_ai_history.pop(cid, None)
        if uid:
            key = (gid_str, uid)
            if key in user_tickets and cid in user_tickets[key]:
                user_tickets[key].remove(cid)
        await interaction.channel.delete(reason="Ticket fechado")

# ── on_message: IA responde ao usuário no ticket ──
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    cid = message.channel.id
    ticket = open_tickets.get(cid)

    if ticket and ticket.get("ai_active"):
        cfg = get_ticket_cfg(ticket["guild_id"])
        # só responde ao dono do ticket (não a mods)
        if cfg.get("ai_key") and message.author.id == ticket["user_id"]:
            async with message.channel.typing():
                reply = await ai_ticket_reply(cfg, cid, message.content, str(message.author))
            if reply:
                e = discord.Embed(description=reply, color=0x9d4edd)
                e.set_author(name="🤖 Assistente IA")
                e.set_footer(text="IA • Resposta automática — Um atendente humano verá em breve")
                await message.channel.send(embed=e)

                # Após 3 mensagens do usuário, notifica mods com relatório
                user_msgs = [m for m in ticket_ai_history.get(cid, []) if m["role"] == "user"]
                if len(user_msgs) == 3:
                    report = await ai_ticket_report(cfg, cid, str(message.author))
                    roles_mentions = " ".join(f"<@&{r}>" for r in cfg.get("support_roles", []))
                    ne = discord.Embed(
                        title="🔔 Atenção Moderadores — Ticket precisa de atendimento",
                        description=report,
                        color=0xff7700,
                        timestamp=datetime.datetime.utcnow(),
                    )
                    ne.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
                    ne.set_footer(text="Gerado automaticamente pela IA após análise da conversa")
                    await message.channel.send(
                        content=f"{'📣 ' + roles_mentions if roles_mentions else ''}",
                        embed=ne)

    await bot.process_commands(message)



# ══════════════════════════════════════════════
# SLASH COMMANDS — TICKETS (só admin)
# ══════════════════════════════════════════════

# ── /ticket-setup ── envia o painel de abertura de ticket
@bot.tree.command(name="ticket-setup", description="[ADMIN] Envia o painel de abertura de tickets no canal")
@app_commands.describe(canal="Canal onde o painel aparecerá")
async def slash_ticket_setup(interaction: discord.Interaction, canal: discord.TextChannel):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)
    cfg = get_ticket_cfg(gid)
    ec  = cfg["embed"]

    color_int = int(ec["color"], 16) if isinstance(ec["color"], str) else ec["color"]
    e = discord.Embed(
        title=ec["title"],
        description=ec["description"],
        color=color_int,
    )
    if ec.get("thumbnail"):
        e.set_thumbnail(url=ec["thumbnail"])
    e.set_footer(text=ec["footer"])

    await canal.send(embed=e, view=TicketOpenView(gid))
    await interaction.response.send_message(
        f"✅ Painel de tickets enviado em {canal.mention}!", ephemeral=True)

# ── /ticket-embed ── personaliza o embed do painel
@bot.tree.command(name="ticket-embed", description="[ADMIN] Personaliza o embed do painel de tickets")
@app_commands.describe(
    titulo="Título do embed",
    descricao="Descrição do embed (use \\n para nova linha)",
    cor="Cor em hex sem # (ex: ff0000)",
    rodape="Texto do rodapé",
    thumbnail="URL da imagem miniatura",
    botao_label="Texto do botão",
    botao_emoji="Emoji do botão",
)
async def slash_ticket_embed(
    interaction: discord.Interaction,
    titulo: str = None, descricao: str = None,
    cor: str = None, rodape: str = None,
    thumbnail: str = None, botao_label: str = None,
    botao_emoji: str = None,
):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)
    cfg = ticket_config.setdefault(gid, {})
    emb = cfg.setdefault("embed", {**TICKET_DEFAULTS["embed"]})
    if titulo:       emb["title"]        = titulo
    if descricao:    emb["description"]  = descricao.replace("\\n", "\n")
    if cor:          emb["color"]        = cor.lstrip("#")
    if rodape:       emb["footer"]       = rodape
    if thumbnail:    emb["thumbnail"]    = thumbnail
    if botao_label:  emb["button_label"] = botao_label
    if botao_emoji:  emb["button_emoji"] = botao_emoji

    # preview
    color_int = int(emb["color"], 16)
    e = discord.Embed(title=emb["title"], description=emb["description"], color=color_int)
    if emb.get("thumbnail"): e.set_thumbnail(url=emb["thumbnail"])
    e.set_footer(text=emb["footer"])
    e.set_author(name="Preview do Embed")
    await interaction.response.send_message(
        "✅ Embed atualizado! Preview:", embed=e, ephemeral=True)

# ── /ticket-mensagem ── personaliza mensagens do ticket
@bot.tree.command(name="ticket-mensagem", description="[ADMIN] Personaliza as mensagens do ticket")
@app_commands.describe(
    boas_vindas="Mensagem ao abrir ticket. Use {user} para mencionar",
    fechamento="Mensagem ao fechar ticket. Use {closer} para mencionar",
)
async def slash_ticket_mensagem(
    interaction: discord.Interaction,
    boas_vindas: str = None, fechamento: str = None,
):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)
    cfg = ticket_config.setdefault(gid, {})
    if boas_vindas: cfg["welcome_msg"] = boas_vindas
    if fechamento:  cfg["close_msg"]   = fechamento
    e = discord.Embed(title="✅ Mensagens Atualizadas", color=0x2ecc71)
    e.add_field(name="Boas-vindas", value=cfg.get("welcome_msg", TICKET_DEFAULTS["welcome_msg"]), inline=False)
    e.add_field(name="Fechamento",  value=cfg.get("close_msg",   TICKET_DEFAULTS["close_msg"]),   inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)

# ── /ticket-cargos ── define cargos de suporte
@bot.tree.command(name="ticket-cargos", description="[ADMIN] Define os cargos que atenderão os tickets")
@app_commands.describe(
    cargo1="Cargo de suporte 1",
    cargo2="Cargo de suporte 2 (opcional)",
    cargo3="Cargo de suporte 3 (opcional)",
)
async def slash_ticket_cargos(
    interaction: discord.Interaction,
    cargo1: discord.Role,
    cargo2: discord.Role = None,
    cargo3: discord.Role = None,
):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)
    cfg = ticket_config.setdefault(gid, {})
    roles = [cargo1.id]
    if cargo2: roles.append(cargo2.id)
    if cargo3: roles.append(cargo3.id)
    cfg["support_roles"] = roles
    mentions = " ".join(f"<@&{r}>" for r in roles)
    e = discord.Embed(title="✅ Cargos de Suporte Definidos", color=0x2ecc71)
    e.add_field(name="Cargos", value=mentions)
    await interaction.response.send_message(embed=e, ephemeral=True)

# ── /ticket-categoria ── define a categoria dos canais
@bot.tree.command(name="ticket-categoria", description="[ADMIN] Define a categoria onde os tickets serão criados")
@app_commands.describe(categoria="Categoria do servidor")
async def slash_ticket_categoria(
    interaction: discord.Interaction, categoria: discord.CategoryChannel):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)
    ticket_config.setdefault(gid, {})["category"] = categoria.id
    await interaction.response.send_message(
        f"✅ Categoria definida: **{categoria.name}**", ephemeral=True)

# ── /ticket-logs ── define canal de logs
@bot.tree.command(name="ticket-logs", description="[ADMIN] Define o canal de logs dos tickets")
@app_commands.describe(canal="Canal de logs")
async def slash_ticket_logs(interaction: discord.Interaction, canal: discord.TextChannel):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)
    ticket_config.setdefault(gid, {})["log_channel"] = canal.id
    await interaction.response.send_message(
        f"✅ Logs de tickets: {canal.mention}", ephemeral=True)

# ── /ticket-limite ── define máx tickets por usuário
@bot.tree.command(name="ticket-limite", description="[ADMIN] Define quantos tickets um usuário pode ter abertos")
@app_commands.describe(limite="Número máximo (padrão: 1)")
async def slash_ticket_limite(interaction: discord.Interaction, limite: int):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)
    ticket_config.setdefault(gid, {})["max_open"] = max(1, limite)
    await interaction.response.send_message(
        f"✅ Limite de tickets por usuário: **{limite}**", ephemeral=True)

# ── /ticket-setai ── configura chave de IA
@bot.tree.command(name="ticket-setai", description="[ADMIN] Configura a API de IA para análise dos tickets")
@app_commands.describe(
    chave="Chave da API (OpenAI ou compatível)",
    modelo="Modelo a usar (padrão: gpt-4o-mini)",
)
async def slash_ticket_setai(
    interaction: discord.Interaction, chave: str, modelo: str = "gpt-4o-mini"):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)
    cfg = ticket_config.setdefault(gid, {})
    cfg["ai_key"]   = chave
    cfg["ai_model"] = modelo
    e = discord.Embed(title="🤖 IA Configurada", color=0x9d4edd)
    e.add_field(name="Modelo", value=modelo)
    e.add_field(name="Chave",  value=f"||{chave[:8]}...||")
    e.set_footer(text="A chave fica guardada em memória. Configure novamente se o bot reiniciar.")
    await interaction.response.send_message(embed=e, ephemeral=True)

# ── /ticket-config ── mostra configuração atual
@bot.tree.command(name="ticket-config", description="[ADMIN] Mostra todas as configurações de ticket do servidor")
async def slash_ticket_config(interaction: discord.Interaction):
    if not is_admin(interaction): return await no_admin(interaction)
    gid = str(interaction.guild.id)
    cfg = get_ticket_cfg(gid)
    ec  = cfg["embed"]

    e = discord.Embed(title="🎫 Configurações de Ticket", color=int(ec["color"], 16),
                      timestamp=datetime.datetime.utcnow())

    # Embed
    e.add_field(name="📝 Título do Embed",    value=ec["title"],        inline=True)
    e.add_field(name="🎨 Cor",                value=f"#{ec['color']}",  inline=True)
    e.add_field(name="🔘 Botão",              value=f"{ec['button_emoji']} {ec['button_label']}", inline=True)
    e.add_field(name="📄 Descrição",          value=ec["description"][:200], inline=False)
    e.add_field(name="📌 Rodapé",             value=ec["footer"],       inline=True)

    # Canais
    cat = interaction.guild.get_channel(cfg.get("category", 0))
    lc  = interaction.guild.get_channel(cfg.get("log_channel", 0))
    e.add_field(name="📁 Categoria",   value=cat.name if cat else "❌ Não definida", inline=True)
    e.add_field(name="📋 Canal de Log",value=lc.mention if lc else "❌ Não definido", inline=True)

    # Cargos
    roles = cfg.get("support_roles", [])
    roles_str = " ".join(f"<@&{r}>" for r in roles) if roles else "❌ Nenhum"
    e.add_field(name="🛡️ Cargos de Suporte", value=roles_str, inline=False)

    # IA
    has_ai = bool(cfg.get("ai_key"))
    e.add_field(name="🤖 IA",    value=f"✅ {cfg.get('ai_model','—')}" if has_ai else "❌ Não configurada", inline=True)
    e.add_field(name="🎟️ Limite", value=f"{cfg.get('max_open',1)} por usuário", inline=True)

    # Tickets abertos
    abertos = sum(1 for t in open_tickets.values() if t["guild_id"] == gid)
    e.add_field(name="📊 Tickets abertos", value=abertos, inline=True)

    e.set_footer(text="Use /ticket-setup para enviar o painel • /ticket-embed para personalizar")
    await interaction.response.send_message(embed=e, ephemeral=True)


    app = web.Application()
    app.router.add_get("/",            handle_index)
    app.router.add_get("/login",       handle_login)
    app.router.add_get("/callback",    handle_callback)
    app.router.add_get("/me",          handle_me)
    app.router.add_get("/actions",     handle_actions)
    app.router.add_get("/guilds",      handle_guilds)
    app.router.add_get("/ws",          handle_ws)
    app.router.add_post("/owner-code", handle_owner_code)
    return app

async def handle_dashboard(request):
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    if not os.path.exists(html_path):
        return web.Response(text="dashboard.html não encontrado", status=404)
    with open(html_path, "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")

async def handle_remote_mod(request):
    """POST /remote/mod — executa ação de moderação via dashboard."""
    data = await request.json()
    session = SESSIONS.get(data.get("session"))
    if not session:
        return web.json_response({"ok": False, "msg": "Não autenticado"}, status=401)

    guild_id  = int(data.get("guild_id", 0))
    action    = data.get("action")
    guild     = bot.get_guild(guild_id)
    if not guild:
        return web.json_response({"ok": False, "msg": "Servidor não encontrado"})

    try:
        if action == "ban":
            user = await bot.fetch_user(int(data["user_id"]))
            await guild.ban(user, reason=data.get("reason","Via dashboard"), delete_message_days=0)
            record_action("ban", session["user"]["username"], str(user), data.get("reason",""), str(guild))

        elif action == "unban":
            user = await bot.fetch_user(int(data["user_id"]))
            await guild.unban(user, reason=data.get("reason","Via dashboard"))
            record_action("unban", session["user"]["username"], str(user), data.get("reason",""), str(guild))

        elif action == "kick":
            member = guild.get_member(int(data["user_id"])) or await guild.fetch_member(int(data["user_id"]))
            await member.kick(reason=data.get("reason","Via dashboard"))
            record_action("kick", session["user"]["username"], str(member), data.get("reason",""), str(guild))

        elif action == "mute":
            member = guild.get_member(int(data["user_id"])) or await guild.fetch_member(int(data["user_id"]))
            until = discord.utils.utcnow() + datetime.timedelta(minutes=int(data.get("minutes",10)))
            await member.timeout(until, reason=data.get("reason","Via dashboard"))
            record_action("mute", session["user"]["username"], str(member), data.get("reason",""), str(guild))

        elif action == "warn":
            member = guild.get_member(int(data["user_id"])) or await guild.fetch_member(int(data["user_id"]))
            mod_obj = type("Mod", (), {"id": 0, "guild_permissions": type("P", (), {"administrator": True})(), "top_role": type("R", (), {"position": 9999})(), "mention": session["user"]["username"]})()
            count, auto_muted, mute_min = await apply_warn(guild, mod_obj, member, data.get("reason","Via dashboard"))
            record_action("warn", session["user"]["username"], str(member), data.get("reason",""), str(guild))

        elif action == "purge":
            ch = guild.get_channel(int(data["channel_id"]))
            if not ch: return web.json_response({"ok": False, "msg": "Canal não encontrado"})
            deleted = await ch.purge(limit=int(data.get("amount",10)))
            record_action("purge", session["user"]["username"], ch.name, f"{len(deleted)} msgs", str(guild))

        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "msg": str(e)})

async def handle_remote_config(request):
    """POST /remote/config — configurações admin via dashboard."""
    data = await request.json()
    session = SESSIONS.get(data.get("session"))
    if not session:
        return web.json_response({"ok": False, "msg": "Não autenticado"}, status=401)

    gid    = str(data.get("guild_id",""))
    action = data.get("action")

    try:
        if action == "setlog":
            guild_log_channels[gid] = int(data["channel_id"])
        elif action == "setmodrole":
            guild_mod_roles.setdefault(gid, set()).add(int(data["role_id"]))
        elif action == "removemodrole":
            guild_mod_roles.get(gid, set()).discard(int(data["role_id"]))
        elif action == "setwarn":
            guild_warn_config[gid] = {"limit": int(data["limit"]), "mute": int(data["minutes"])}
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "msg": str(e)})

async def handle_remote_ticket(request):
    """POST /remote/ticket — configurações de ticket via dashboard."""
    data = await request.json()
    session = SESSIONS.get(data.get("session"))
    if not session:
        return web.json_response({"ok": False, "msg": "Não autenticado"}, status=401)

    gid  = str(data.get("guild_id",""))
    type_ = data.get("type")
    cfg  = ticket_config.setdefault(gid, {})

    try:
        if type_ == "embed":
            emb = cfg.setdefault("embed", {**TICKET_DEFAULTS["embed"]})
            if data.get("title"):        emb["title"]        = data["title"]
            if data.get("description"):  emb["description"]  = data["description"]
            if data.get("color"):        emb["color"]        = data["color"].lstrip("#")
            if data.get("footer"):       emb["footer"]       = data["footer"]
            if data.get("thumbnail"):    emb["thumbnail"]    = data["thumbnail"]
            if data.get("button_label"): emb["button_label"] = data["button_label"]
            if data.get("button_emoji"): emb["button_emoji"] = data["button_emoji"]
        elif type_ == "mensagem":
            if data.get("welcome"): cfg["welcome_msg"] = data["welcome"]
            if data.get("close"):   cfg["close_msg"]   = data["close"]
        elif type_ == "cargos":
            roles = [r for r in [data.get("role1"), data.get("role2")] if r]
            if roles: cfg["support_roles"] = [int(r) for r in roles]
            if data.get("category"):    cfg["category"]    = int(data["category"])
            if data.get("log_channel"): cfg["log_channel"] = int(data["log_channel"])
            if data.get("limit"):       cfg["max_open"]    = int(data["limit"])
        elif type_ == "ai":
            if data.get("key"):   cfg["ai_key"]   = data["key"]
            if data.get("model"): cfg["ai_model"] = data["model"]
        elif type_ == "setup":
            guild = bot.get_guild(int(data.get("guild_id",0)))
            if not guild: return web.json_response({"ok": False, "msg": "Servidor não encontrado"})
            ch = guild.get_channel(int(data["channel_id"]))
            if not ch: return web.json_response({"ok": False, "msg": "Canal não encontrado"})
            ec = get_ticket_cfg(gid)["embed"]
            color_int = int(ec["color"], 16) if isinstance(ec["color"], str) else ec["color"]
            e = discord.Embed(title=ec["title"], description=ec["description"], color=color_int)
            if ec.get("thumbnail"): e.set_thumbnail(url=ec["thumbnail"])
            e.set_footer(text=ec["footer"])
            await ch.send(embed=e, view=TicketOpenView(gid))
        return web.json_response({"ok": True})
    except Exception as ex:
        return web.json_response({"ok": False, "msg": str(ex)})


def create_app():
    app = web.Application()
    app.router.add_get("/",               handle_index)
    app.router.add_get("/dashboard",      handle_dashboard)
    app.router.add_get("/login",          handle_login)
    app.router.add_get("/callback",       handle_callback)
    app.router.add_get("/me",             handle_me)
    app.router.add_get("/actions",        handle_actions)
    app.router.add_get("/guilds",         handle_guilds)
    app.router.add_get("/ws",             handle_ws)
    app.router.add_post("/owner-code",    handle_owner_code)
    app.router.add_post("/remote/ban",    handle_remote_ban)
    app.router.add_post("/remote/mod",    handle_remote_mod)
    app.router.add_post("/remote/config", handle_remote_config)
    app.router.add_post("/remote/ticket", handle_remote_ticket)
    return app

async def start_web():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", DASHBOARD_PORT).start()
    log.info(f"Web em http://0.0.0.0:{DASHBOARD_PORT}")

async def main():
    await start_web()
    await bot.start(BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

