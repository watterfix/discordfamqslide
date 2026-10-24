import logging
import discord
from discord import app_commands
from discord.ext import commands

REACTION = "👍"

class VZPCreateModal(discord.ui.Modal, title="Создать сообщение ВЗП"):
    message_text = discord.ui.TextInput(
        label="Текст сообщения", style=discord.TextStyle.paragraph,
        placeholder="Напишите объявление для ВЗП…", max_length=1800, required=True
    )
    required_reactions = discord.ui.TextInput(
        label="Сколько реакций нужно для закрытия?", placeholder="Например: 5",
        min_length=1, max_length=3, required=True
    )

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            needed = int(self.required_reactions.value.strip())
        except ValueError:
            return await interaction.response.send_message("Количество реакций должно быть целым числом.", ephemeral=True)
        if not 1 <= needed <= 100:
            return await interaction.response.send_message("Укажи количество от 1 до 100.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        content = f"{self.message_text.value.strip()}\n\nНажмите {REACTION}, если идёте на ВЗП. Нужно участников: **{needed}**."
        try:
            msg = await interaction.channel.send(content, allowed_mentions=discord.AllowedMentions.none())
            await msg.add_reaction(REACTION)
            await self.cog.bot.db.execute(
                "INSERT INTO vzp_posts(guild_id,channel_id,message_id,creator_id,body,required,active,created) VALUES(?,?,?,?,?,?,1,?)",
                (interaction.guild_id, interaction.channel_id, msg.id, interaction.user.id,
                 self.message_text.value.strip(), needed, int(discord.utils.utcnow().timestamp()))
            )
        except Exception:
            logging.exception("Не удалось создать сообщение ВЗП")
            return await interaction.followup.send("Не удалось создать сообщение. Проверь права бота на отправку сообщений и добавление реакций.", ephemeral=True)
        await interaction.followup.send(f"✅ Сообщение ВЗП создано: {msg.jump_url}", ephemeral=True)

class VZPPanelView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Создать ВЗП", style=discord.ButtonStyle.success, emoji="📣", custom_id="vzp:create")
    async def create_vzp(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (self.cog.bot.is_owner_user(interaction.user)
                or interaction.user.guild_permissions.administrator
                or interaction.user.guild_permissions.manage_guild
                or await self.cog.bot.has_role_permission(interaction.user, "vzp_create")):
            pass
        else:
            return await interaction.response.send_message("🔒 Создавать объявления ВЗП могут только владелец бота, администраторы или роль с правом «Создавать объявления ВЗП» в /панель.", ephemeral=True)
        await interaction.response.send_modal(VZPCreateModal(self.cog))

class VZP(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS vzp_posts(
            message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL, body TEXT NOT NULL, required INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS vzp_participants(
            message_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            PRIMARY KEY(message_id,user_id));
        """)
        await self.bot.db.conn.commit()
        self.bot.add_view(VZPPanelView(self))

    @app_commands.command(name="взп_панель", description="Опубликовать панель создания объявлений ВЗП")
    async def vzp_panel(self, interaction: discord.Interaction):
        if not (self.bot.is_owner_user(interaction.user)
                or interaction.user.guild_permissions.administrator
                or interaction.user.guild_permissions.manage_guild
                or await self.bot.has_role_permission(interaction.user, "vzp_create")):
            pass
        else:
            return await interaction.response.send_message("🔒 Создавать панель ВЗП могут только владелец бота, администраторы или роль с правом «Создавать объявления ВЗП» в /панель.", ephemeral=True)
        await interaction.response.send_message(
            "📣 **Панель ВЗП**\nНажми кнопку, чтобы создать сообщение и указать нужное количество реакций.",
            view=VZPPanelView(self), allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) != REACTION:
            return
        row = await self.bot.db.fetchone(
            "SELECT * FROM vzp_posts WHERE message_id=? AND active=1", (payload.message_id,))
        if not row:
            return
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO vzp_participants(message_id,user_id) VALUES(?,?)",
            (payload.message_id, payload.user_id))
        participants = await self.bot.db.fetchall(
            "SELECT user_id FROM vzp_participants WHERE message_id=? ORDER BY rowid", (payload.message_id,))
        if len(participants) < row["required"]:
            return
        # Close atomically before editing so simultaneous reactions cannot finalize twice.
        await self.bot.db.execute("UPDATE vzp_posts SET active=0 WHERE message_id=? AND active=1", (payload.message_id,))
        channel = self.bot.get_channel(row["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(row["channel_id"])
            except discord.HTTPException:
                logging.exception("Не удалось найти канал сообщения ВЗП")
                return
        try:
            message = await channel.fetch_message(payload.message_id)
            mentions = " ".join(f"<@{p['user_id']}>" for p in participants)
            await message.edit(content=f"**На ВЗП идут:** {mentions}", allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            try:
                await message.clear_reactions()
            except discord.Forbidden:
                # If reaction removal is forbidden, the post is still closed in the database
                # and subsequent reactions are ignored.
                pass
        except discord.HTTPException:
            logging.exception("Не удалось закрыть сообщение ВЗП %s", payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) != REACTION:
            return
        await self.bot.db.execute(
            "DELETE FROM vzp_participants WHERE message_id=? AND user_id=? AND EXISTS "
            "(SELECT 1 FROM vzp_posts WHERE message_id=? AND active=1)",
            (payload.message_id, payload.user_id, payload.message_id))

async def setup(bot):
    await bot.add_cog(VZP(bot))
