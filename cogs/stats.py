import discord
from discord import app_commands
from discord.ext import commands, tasks

KEYS = ["all", "bots", "online", "afk", "contracts", "vehicles", "applications", "payments"]
NAMES = {
    "voice": {
        "all": "👥・Всего участников: {}", "bots": "🤖・Боты: {}", "online": "🟢・Сейчас онлайн: {}",
        "afk": "🌙・В AFK: {}", "contracts": "📄・Активные контракты: {}",
        "vehicles": "🚘・Машины в аренде: {}", "applications": "📨・Заявки на рассмотрении: {}",
        "payments": "⏳・Ожидают проверки оплаты: {}",
    },
    "text": {
        "all": "всего-участников-{}", "bots": "боты-{}", "online": "сейчас-онлайн-{}",
        "afk": "в-afk-{}", "contracts": "активные-контракты-{}",
        "vehicles": "машины-в-аренде-{}", "applications": "заявки-на-рассмотрении-{}",
        "payments": "ожидают-проверки-оплаты-{}",
    },
}
CATEGORY = "📊 Статистика сервера"


class Stats(commands.Cog):
    """Каналы-счётчики сверху списка каналов: участники, онлайн, в голосе, боты."""
    def __init__(self, bot):
        self.bot = bot
        self.refresh.start()

    async def interaction_check(self, i: discord.Interaction):
        if self.bot.is_owner_user(i.user) or i.user.guild_permissions.administrator or i.user.guild_permissions.manage_guild:
            return True
        await i.response.send_message("🔒 Управлять статистикой могут только владелец бота и администраторы сервера.", ephemeral=True)
        return False

    def cog_unload(self):
        self.refresh.cancel()

    async def counts(self, guild):
        bots = sum(1 for m in guild.members if m.bot)
        voice = sum(1 for vc in [*guild.voice_channels, *guild.stage_channels] for m in vc.members if not m.bot)
        try:   # онлайн берём у Discord — так не нужен привилегированный Presence Intent
            online = (await self.bot.fetch_guild(guild.id, with_counts=True)).approximate_presence_count
        except discord.HTTPException:
            online = None
        db = self.bot.db
        async def count(sql, params):
            row = await db.fetchone(sql, params)
            return int(row["c"] or 0) if row else 0
        gid = guild.id
        afk = await count("SELECT COUNT(*) c FROM afk WHERE guild_id=? AND status='active'", (gid,))
        contracts = await count("SELECT COUNT(*) c FROM contracts WHERE guild_id=? AND status='open'", (gid,))
        vehicles = await count("SELECT COUNT(*) c FROM vehicles WHERE guild_id=? AND status='in_use'", (gid,))
        applications = await count("SELECT COUNT(*) c FROM applications WHERE guild_id=? AND status IN ('pending','reviewing','called')", (gid,))
        payments = await count("SELECT COUNT(*) c FROM treasury_submissions WHERE guild_id=? AND status IN ('waiting_screenshot','pending_review')", (gid,))
        payments += await count("SELECT COUNT(*) c FROM contract_submissions WHERE guild_id=? AND status IN ('waiting_screenshot','pending_review')", (gid,))
        return {"all": max(0, (guild.member_count or 0) - bots), "online": online, "bots": bots,
                "afk": afk, "contracts": contracts, "vehicles": vehicles,
                "applications": applications, "payments": payments}

    async def update(self, guild):
        db = self.bot.db
        style = await db.get_setting(guild.id, "stats_style")
        if not style:
            return
        data = await self.counts(guild)
        for k in KEYS:
            cid = await db.get_setting(guild.id, f"stats_{k}")
            ch = guild.get_channel(int(cid)) if cid else None
            if not ch or data[k] is None:
                continue
            name = NAMES[style][k].format(data[k])
            if ch.name != name:   # переименовываем только при изменении (у Discord лимит на смену имён)
                try: await ch.edit(name=name, reason="Статистика сервера")
                except discord.HTTPException: pass

    async def _remove(self, guild):
        db = self.bot.db
        # Remove legacy stats categories too (older versions used a separate Server Stats category).
        # Only categories clearly named as statistics are touched.
        for category in list(guild.categories):
            lowered = category.name.casefold()
            if ("статистик" in lowered or "server stats" in lowered or "server-stat" in lowered):
                try:
                    for child in list(category.channels):
                        await child.delete(reason="Обновление статистики сервера")
                    await category.delete(reason="Обновление статистики сервера")
                except discord.HTTPException:
                    pass
        for k in [*KEYS, "category"]:
            setting_key = "stats_category" if k == "category" else f"stats_{k}"
            cid = await db.get_setting(guild.id, setting_key)
            ch = guild.get_channel(int(cid)) if cid else None
            if ch:
                try: await ch.delete(reason="Статистика сервера")
                except discord.HTTPException: pass
            await db.execute("DELETE FROM settings WHERE guild_id=? AND key=?", (guild.id, setting_key))
        await db.execute("DELETE FROM settings WHERE guild_id=? AND key='stats_style'", (guild.id,))

    @app_commands.command(name="статистика_создать", description="Создать 8 автоматически обновляемых счётчиков сервера")
    @app_commands.rename(style="стиль")
    @app_commands.describe(style="Вид каналов статистики")
    @app_commands.choices(style=[app_commands.Choice(name="Голосовые, с эмодзи (рекомендую)", value="voice"),
                                 app_commands.Choice(name="Текстовые, как на скриншоте", value="text")])
    async def setup_stats(self, i, style: app_commands.Choice[str] = None):
        await i.response.defer(ephemeral=True)
        style, g, db = (style.value if style else "voice"), i.guild, self.bot.db
        await self._remove(g)   # если статистика уже была — пересоздаём
        ow = {g.default_role: discord.PermissionOverwrite(view_channel=True, connect=False, send_messages=False, add_reactions=False)}
        try:
            cat = await g.create_category(CATEGORY, overwrites=ow, position=0, reason="Статистика сервера")
            await db.set_setting(g.id, "stats_category", cat.id)
            for k in KEYS:
                name = NAMES[style][k].format(0)
                make = g.create_voice_channel if style == "voice" else g.create_text_channel
                ch = await make(name, category=cat, overwrites=ow, reason="Статистика сервера")
                await db.set_setting(g.id, f"stats_{k}", ch.id)
        except discord.Forbidden:
            await self._remove(g)
            return await i.followup.send("❌ У бота нет права **«Управление каналами»**. Выдайте его роли бота и повторите.", ephemeral=True)
        await db.set_setting(g.id, "stats_style", style)
        await self.update(g)
        await self.bot.log(g, "server", "Статистика сервера создана", f"Стиль: {style}", i.user, 0x57F287)
        await i.followup.send("✅ Статистика создана в одной категории. Все 8 счётчиков обновляются раз в минуту. Старые категории статистики удалены.", ephemeral=True)

    @app_commands.command(name="статистика_удалить", description="Удалить каналы со статистикой")
    async def remove_stats(self, i):
        await i.response.defer(ephemeral=True)
        await self._remove(i.guild)
        await self.bot.log(i.guild, "server", "Статистика сервера удалена", "", i.user, 0xED4245)
        await i.followup.send("🗑 Каналы статистики удалены.", ephemeral=True)

    @tasks.loop(minutes=1)
    async def refresh(self):
        for g in self.bot.guilds:
            await self.update(g)

    @refresh.before_loop
    async def _w(self): await self.bot.wait_until_ready()

async def setup(bot): await bot.add_cog(Stats(bot))
