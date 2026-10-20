import time, logging, discord
from discord import app_commands
from discord.ext import commands, tasks

TTL = 60   # через сколько секунд пустой временный войс удаляется


class TempVoice(commands.Cog):
    """Временные голосовые каналы: удаляются через минуту, если в них никто не сидит."""
    def __init__(self, bot):
        self.bot = bot
        self.sweep.start()

    def cog_unload(self):
        self.sweep.cancel()

    # ---------- создание ----------
    async def create(self, guild, owner, name, limit=0, category=None):
        """Создаёт временный войс. Возвращает (канал, ошибка)."""
        db = self.bot.db
        old = await db.fetchone("SELECT channel_id FROM temp_voices WHERE guild_id=? AND owner_id=?", (guild.id, owner.id))
        if old and guild.get_channel(old["channel_id"]):
            return guild.get_channel(old["channel_id"]), None   # у человека уже есть свой войс
        kw = dict(name=name[:100], user_limit=limit, category=category, reason=f"Временный войс: {owner}")
        try:
            try:   # владельцу даём управление своим каналом
                ch = await guild.create_voice_channel(
                    overwrites={owner: discord.PermissionOverwrite(manage_channels=True, move_members=True, connect=True)}, **kw)
            except discord.Forbidden:
                ch = await guild.create_voice_channel(**kw)
        except discord.HTTPException as ex:
            logging.warning("tempvoice create failed: %s", ex)
            return None, "❌ Не удалось создать канал: у бота нет права **«Управление каналами»**."
        now = int(time.time())
        await db.execute("INSERT INTO temp_voices(channel_id,guild_id,owner_id,created,empty_since) VALUES(?,?,?,?,?)",
                         (ch.id, guild.id, owner.id, now, now))   # пустой с момента создания — таймер сразу идёт
        await self.bot.log(guild, "tempvoice", "Временный войс создан", f"{ch.mention} · владелец {owner.mention}", owner, 0x57F287)
        return ch, None

    @app_commands.command(name="войс_создать", description="Создать временный голосовой канал (удалится через минуту, если в нём никого нет)")
    @app_commands.rename(name="название", limit="лимит")
    @app_commands.describe(name="Название канала (необязательно)", limit="Лимит участников (0 — без лимита)")
    async def voice_create(self, i, name: str = None, limit: app_commands.Range[int, 0, 99] = 0):
        await i.response.defer(ephemeral=True)
        hub = await self.bot.get_ch(i.guild, "tempvoice_hub")
        ch, err = await self.create(i.guild, i.user, name or f"🔊 {i.user.display_name}", limit, hub.category if hub else None)
        if err: return await i.followup.send(err, ephemeral=True)
        await i.followup.send(f"✅ {ch.mention} создан. Зайдите в течение **{TTL} сек.**, иначе он удалится.", ephemeral=True)

    # ---------- «зайди, чтобы создать» ----------
    @commands.Cog.listener()
    async def on_voice_state_update(self, m, a, b):
        if m.bot or a.channel == b.channel:
            return
        db = self.bot.db
        hub = await db.get_setting(m.guild.id, "tempvoice_hub")
        if b.channel and hub and b.channel.id == int(hub):
            ch, err = await self.create(m.guild, m, f"🔊 {m.display_name}", 0, b.channel.category)
            if ch:
                try: await m.move_to(ch)
                except discord.HTTPException: pass
        # пустой канал — запускаем таймер, зашёл кто-то — сбрасываем
        if a.channel:
            await self._touch(a.channel)
        if b.channel:
            await self._touch(b.channel)

    async def _touch(self, ch):
        row = await self.bot.db.fetchone("SELECT * FROM temp_voices WHERE channel_id=?", (ch.id,))
        if not row: return
        if ch.members:
            if row["empty_since"] is not None:
                await self.bot.db.execute("UPDATE temp_voices SET empty_since=NULL WHERE channel_id=?", (ch.id,))
        elif row["empty_since"] is None:
            await self.bot.db.execute("UPDATE temp_voices SET empty_since=? WHERE channel_id=?", (int(time.time()), ch.id))

    # ---------- уборка ----------
    @tasks.loop(seconds=10)
    async def sweep(self):
        now = int(time.time())
        for row in await self.bot.db.fetchall("SELECT * FROM temp_voices"):
            guild = self.bot.get_guild(row["guild_id"])
            if not guild: continue
            ch = guild.get_channel(row["channel_id"])
            if ch is None:   # канал удалён вручную
                await self.bot.db.execute("DELETE FROM temp_voices WHERE channel_id=?", (row["channel_id"],))
                continue
            if ch.members:
                if row["empty_since"] is not None:
                    await self.bot.db.execute("UPDATE temp_voices SET empty_since=NULL WHERE channel_id=?", (ch.id,))
                continue
            if row["empty_since"] is None:
                await self.bot.db.execute("UPDATE temp_voices SET empty_since=? WHERE channel_id=?", (now, ch.id))
                continue
            if now - row["empty_since"] >= TTL:
                name = ch.name
                try:
                    await ch.delete(reason="Временный войс пуст")
                except discord.HTTPException as ex:
                    logging.warning("tempvoice delete failed: %s", ex)
                    continue
                await self.bot.db.execute("DELETE FROM temp_voices WHERE channel_id=?", (row["channel_id"],))
                await self.bot.log(guild, "tempvoice", "Временный войс удалён", f"**{name}** · пуст {TTL} сек. · владелец <@{row['owner_id']}>", None, 0x99AAB5)

    @sweep.before_loop
    async def _w(self): await self.bot.wait_until_ready()

async def setup(bot): await bot.add_cog(TempVoice(bot))
