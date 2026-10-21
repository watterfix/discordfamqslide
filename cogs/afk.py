import time, discord
from discord import app_commands
from discord.ext import commands, tasks
from utils import make, COLOR, staff_only, staff_or_permission

TAG = "inactive"

def tagged(name):
    """Добавляет inactive в начало ника, не дублируя метку."""
    clean = name.strip()
    if clean.lower().startswith(TAG.lower()):
        return clean[:32]
    prefix = f"{TAG} "
    return prefix + clean[:32 - len(prefix)]


class AfkModal(discord.ui.Modal, title="Встать в АФК"):
    reason = discord.ui.TextInput(label="Причина", max_length=200)
    hours = discord.ui.TextInput(label="На сколько часов (1–720)", default="24", max_length=3)

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, i):
        try:
            h = int(self.hours.value)
            assert 1 <= h <= 720
        except (ValueError, AssertionError):
            return await i.response.send_message("Укажите число часов от 1 до 720.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        err = await self.bot.get_cog("AFK").start(i.guild, i.user, self.reason.value, int(time.time()) + h * 3600)
        await i.followup.send(err or "💤 Вы в АФК. Возвращайтесь!", ephemeral=True)


class AfkPanel(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Встать в АФК", style=discord.ButtonStyle.primary, emoji="💤", custom_id="afk:start")
    async def start(self, i, b):
        await i.response.send_modal(AfkModal(self.bot))

    @discord.ui.button(label="Выйти из АФК", style=discord.ButtonStyle.success, emoji="✅", custom_id="afk:end")
    async def end(self, i, b):
        cog = self.bot.get_cog("AFK")
        await i.response.defer(ephemeral=True)
        row = await cog.active(i.guild_id, i.user.id)
        if not row:
            return await i.followup.send("Вы не в АФК.", ephemeral=True)
        await cog.end(row, i.guild, i.user, "кнопка")
        await i.followup.send("С возвращением! 👋", ephemeral=True)


class AfkCardView(discord.ui.View):
    """Кнопка под сообщением об АФК: руководство может вытащить человека из АФК."""
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Вытащить из АФК", style=discord.ButtonStyle.danger, emoji="🚪", custom_id="afk:pull")
    async def pull_btn(self, i, b):
        if not (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "afk_pull")):
            return await i.response.send_message("🔒 Нет права выводить участников из АФК.", ephemeral=True)
        row = await self.bot.db.fetchone("SELECT * FROM afk WHERE message_id=? AND status='active'", (i.message.id,))
        if not row:
            return await i.response.send_message("Этот АФК уже завершён.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        sent = await self.bot.get_cog("AFK").pull(i.guild, i.user, row)
        await i.followup.send(f"✅ <@{row['user_id']}> вытащен из АФК." + ("" if sent else "\n⚠️ ЛС закрыты — он не получил уведомление."), ephemeral=True)


class AFK(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check.start()
        self.inactive_check.start()
        self.panel_refresh.start()

    async def cog_load(self):
        self.bot.add_view(AfkPanel(self.bot))
        self.bot.add_view(AfkCardView(self.bot))

    def cog_unload(self):
        self.check.cancel()
        self.inactive_check.cancel()
        self.panel_refresh.cancel()

    # ---------- ядро ----------
    async def active(self, gid, uid):
        return await self.bot.db.fetchone("SELECT * FROM afk WHERE guild_id=? AND user_id=? AND status='active'", (gid, uid))

    async def _role(self, guild):
        rid = await self.bot.db.get_setting(guild.id, "afk_role")
        return guild.get_role(int(rid)) if rid else None

    async def start(self, guild, member, reason, end_ts, auto=False):
        """Ставит в АФК. Возвращает текст ошибки или None."""
        if await self.active(guild.id, member.id):
            return "Вы уже в АФК."
        now = int(time.time())
        aid = await self.bot.db.execute(
            "INSERT INTO afk(guild_id,user_id,reason,start_ts,end_ts,status,auto,old_nick) VALUES(?,?,?,?,?,'active',?,?)",
            (guild.id, member.id, reason, now, end_ts, 1 if auto else 0, member.nick))
        role = await self._role(guild)
        if role:
            try: await member.add_roles(role)
            except discord.HTTPException: pass
        # При любом входе в АФК ставим inactive перед ником.
        nick_warning = None
        me = guild.me
        if me is None or not me.guild_permissions.manage_nicknames:
            nick_warning = "⚠️ Вы в АФК, но ник не изменён: у бота нет права **Управлять никами (Manage Nicknames)**."
        elif member.id == guild.owner_id:
            nick_warning = "⚠️ Вы в АФК, но ник владельца сервера бот изменить не может."
        elif member.top_role >= me.top_role and member.id != me.id:
            nick_warning = "⚠️ Вы в АФК, но ник не изменён: поднимите роль бота выше вашей роли в настройках ролей сервера."
        else:
            try:
                await member.edit(nick=tagged(member.display_name), reason="Участник ушёл в АФК")
            except discord.Forbidden:
                nick_warning = "⚠️ Вы в АФК, но Discord запретил менять ник. Проверьте право **Управлять никами** и иерархию ролей бота."
            except discord.HTTPException as exc:
                nick_warning = f"⚠️ Вы в АФК, но ник не удалось изменить (ошибка Discord: {exc.status})."
        if nick_warning:
            await self.bot.log(guild, "afk", "Не удалось изменить ник при входе в АФК", nick_warning, member, 0xED4245)
        ch = await self.bot.get_ch(guild, "afk_channel")
        if ch:
            if auto:
                e = make("🔕 Неактивен", f"### {member.display_name}", COLOR["gray"], footer="Автоматический АФК")
                e.add_field(name="📋 Причина", value=reason, inline=False)
                e.add_field(name="🕐 С", value=f"<t:{now}:R>")
            else:
                e = make("💤 Участник ушёл в АФК", f"### {member.display_name}", COLOR["gray"], footer="АФК")
                e.add_field(name="📋 Причина", value=reason, inline=False)
                e.add_field(name="🕐 Ушёл", value=f"<t:{now}:R>")
                e.add_field(name="⏰ Вернётся", value=f"<t:{end_ts}:f>\n<t:{end_ts}:R>")
            e.set_thumbnail(url=member.display_avatar.url)
            try:
                msg = await ch.send(embed=e, view=AfkCardView(self.bot))
                await self.bot.db.execute("UPDATE afk SET message_id=? WHERE id=?", (msg.id, aid))
            except discord.HTTPException:
                pass
        await self.bot.log(guild, "afk", "Авто-АФК (неактивность)" if auto else "АФК начат",
                           reason if auto else f"{reason} · до <t:{end_ts}:f>", member, 0x99AAB5)
        # Обновляем панель сразу, не ожидая фонового таймера.
        await self.refresh_panel(guild)
        return nick_warning

    async def end(self, row, guild, member, why, by=None):
        await self.bot.db.execute("UPDATE afk SET status='ended' WHERE id=?", (row["id"],))
        if row["message_id"]:   # убираем сообщение об АФК
            ch = await self.bot.get_ch(guild, "afk_channel")
            if ch:
                try: await ch.get_partial_message(row["message_id"]).delete()
                except discord.HTTPException: pass
        role = await self._role(guild)
        if member and role:
            try: await member.remove_roles(role)
            except discord.HTTPException: pass
        if member and member.nick and member.nick.lower().startswith(TAG.lower()):   # возвращаем исходный ник
            try:
                await member.edit(nick=row["old_nick"], reason="Вернулся из АФК")
            except discord.Forbidden:
                await self.bot.log(guild, "afk", "Не удалось восстановить ник", "Проверьте право **Управлять никами** и иерархию ролей бота.", member, 0xED4245)
            except discord.HTTPException as exc:
                await self.bot.log(guild, "afk", "Не удалось восстановить ник", f"Ошибка Discord: {exc.status}.", member, 0xED4245)
        await self.bot.log(guild, "afk", f"АФК завершён ({why})", f"<@{row['user_id']}> · с <t:{row['start_ts']}:R>" + (f"\nВытащил: {by.mention}" if by else ""), member, 0x57F287)
        # Убираем участника из списка сразу после выхода из АФК.
        await self.refresh_panel(guild)

    async def pull(self, guild, staff, row):
        """Принудительно возвращает человека из АФК и оповещает его. True — если ЛС дошло."""
        member = guild.get_member(row["user_id"])
        await self.end(row, guild, member, "принудительно", by=staff)
        if not member: return False
        try:
            await member.send(f"🚪 Вас вернули из АФК на **{guild.name}**: {staff.display_name}.")
            return True
        except discord.HTTPException:
            return False

    # ---------- команды ----------
    @app_commands.command(name="афк_панель", description="Отправить панель с кнопками АФК")
    @app_commands.rename(channel="канал")
    @app_commands.describe(channel="Канал для панели АФК")
    @staff_only()
    async def panel(self, i, channel: discord.TextChannel):
        e = make("💤 АФК", "Нужно отойти? Нажмите **«Встать в АФК»**, укажите причину и срок.\n"
                           "Когда вернётесь — **«Выйти из АФК»**, сообщение об АФК исчезнет.\n\n"
                           "*Кто не пишет и не заходит в голос несколько дней, получает АФК автоматически.*", COLOR["gray"])
        # Панель содержит актуальный список и обновляется автоматически раз в минуту.
        e.description = (e.description or "") + "\n\n" + "**Сейчас в АФК**\n" + (await self.list_embed(i.guild_id)).description
        msg = await channel.send(embed=e, view=AfkPanel(self.bot))
        await self.bot.db.set_setting(i.guild_id, "afk_panel_channel_id", channel.id)
        await self.bot.db.set_setting(i.guild_id, "afk_panel_message_id", msg.id)
        await self.bot.log(i.guild, "afk", "Панель АФК отправлена", channel.mention, i.user)
        await i.response.send_message(f"Панель отправлена в {channel.mention}. Список АФК обновляется автоматически раз в минуту.", ephemeral=True)

    @app_commands.command(name="афк", description="Встать в АФК")
    @app_commands.rename(reason="причина", hours="часы")
    @app_commands.describe(reason="Причина АФК", hours="На сколько часов (1–720)")
    async def afk(self, i, reason: str, hours: app_commands.Range[int, 1, 720]):
        await i.response.defer(ephemeral=True)
        err = await self.start(i.guild, i.user, reason, int(time.time()) + hours * 3600)
        await i.followup.send(err or "💤 Вы в АФК. Возвращайтесь!", ephemeral=True)

    @app_commands.command(name="афк_вытащить", description="Вытащить участника из АФК (руководство)")
    @app_commands.rename(member="участник")
    @app_commands.describe(member="Кого вытащить из АФК")
    @staff_or_permission("afk_pull")
    async def pull_cmd(self, i, member: discord.Member):
        row = await self.active(i.guild_id, member.id)
        if not row: return await i.response.send_message("Он не в АФК.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        sent = await self.pull(i.guild, i.user, row)
        await i.followup.send(f"✅ {member.mention} вытащен из АФК." + ("" if sent else "\n⚠️ ЛС закрыты — он не получил уведомление."), ephemeral=True)

    @app_commands.command(name="афк_вернуться", description="Вернуться из АФК")
    async def back(self, i):
        row = await self.active(i.guild_id, i.user.id)
        if not row: return await i.response.send_message("Вы не в АФК.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        await self.end(row, i.guild, i.user, "вручную")
        await i.followup.send("С возвращением! 👋", ephemeral=True)

    @app_commands.command(name="афк_список", description="Кто сейчас в АФК")
    async def lst(self, i):
        await i.response.send_message(embed=await self.list_embed(i.guild_id))

    async def list_embed(self, gid):
        rows = await self.bot.db.fetchall("SELECT * FROM afk WHERE guild_id=? AND status='active' ORDER BY auto, end_ts", (gid,))
        text = "\n".join((f"🔕 <@{r['user_id']}> — неактивен <t:{r['start_ts']}:R>" if r["auto"]
                          else f"💤 <@{r['user_id']}> — {r['reason']}\n┕ вернётся <t:{r['end_ts']}:R>") for r in rows) or "*Все на месте* ✅"
        return make(f"💤 Сейчас в АФК · {len(rows)}", text[:4000], COLOR["gray"])

    # ---------- события ----------
    @commands.Cog.listener()
    async def on_message(self, msg):
        if msg.author.bot or not msg.guild:
            return
        mine = await self.active(msg.guild.id, msg.author.id)
        if mine and mine["auto"]:   # написал — значит вернулся
            await self.end(mine, msg.guild, msg.author, "появился в чате")
        for m in msg.mentions[:3]:
            r = await self.active(msg.guild.id, m.id)
            if r:
                text = f"🔕 {m.display_name} неактивен." if r["auto"] else f"💤 {m.display_name} в АФК до <t:{r['end_ts']}:R>: {r['reason']}"
                await msg.reply(text, mention_author=False, delete_after=15)

    @commands.Cog.listener()
    async def on_voice_state_update(self, m, a, b):
        if m.bot or a.channel or not b.channel:
            return
        mine = await self.active(m.guild.id, m.id)
        if mine and mine["auto"]:
            await self.end(mine, m.guild, m, "зашёл в голос")

    # ---------- фоновые задачи ----------
    async def refresh_panel(self, guild):
        """Обновляет сохранённую панель АФК сразу после изменения статуса."""
        channel_id = await self.bot.db.get_setting(guild.id, "afk_panel_channel_id")
        message_id = await self.bot.db.get_setting(guild.id, "afk_panel_message_id")
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(int(message_id))
            if not msg.embeds:
                return
            e = msg.embeds[0].copy()
            description = e.description or ""
            marker = "\n\n**Сейчас в АФК**\n"
            base = description.split(marker, 1)[0]
            current = (await self.list_embed(guild.id)).description or "*Все на месте* ✅"
            e.description = (base + marker + current)[:4096]
            await msg.edit(embed=e, view=AfkPanel(self.bot))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    @tasks.loop(minutes=1)
    async def panel_refresh(self):
        """Резервная синхронизация панели раз в минуту."""
        for guild in self.bot.guilds:
            await self.refresh_panel(guild)

    @panel_refresh.before_loop
    async def _panel_refresh_ready(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def check(self):
        for r in await self.bot.db.fetchall("SELECT * FROM afk WHERE status='active' AND auto=0 AND end_ts<=?", (int(time.time()),)):
            g = self.bot.get_guild(r["guild_id"])
            if g: await self.end(r, g, g.get_member(r["user_id"]), "время вышло")

    @tasks.loop(minutes=30)
    async def inactive_check(self):
        db, now = self.bot.db, int(time.time())
        for g in self.bot.guilds:
            days = int(await db.get_setting(g.id, "inactive_days") or 4)
            hb = await db.get_setting(g.id, "heartbeat")
            await db.set_setting(g.id, "heartbeat", now)
            if hb and now - int(hb) > 5400:   # бот был выключен — это время не считаем неактивностью
                await db.execute("UPDATE last_seen SET ts=ts+? WHERE guild_id=?", (now - int(hb), g.id))
            if days <= 0:
                continue
            rid = await db.get_setting(g.id, "accepted_role")
            role = g.get_role(int(rid)) if rid else None
            roster = {r["user_id"] for r in await db.fetchall("SELECT user_id FROM members WHERE guild_id=?", (g.id,))}
            seen = {r["user_id"]: r["ts"] for r in await db.fetchall("SELECT user_id, ts FROM last_seen WHERE guild_id=?", (g.id,))}
            for m in g.members:
                if m.bot or not (m.id in roster or (role and role in m.roles)):
                    continue
                ts = seen.get(m.id)
                if ts is None or (m.voice and m.voice.channel):
                    await db.execute("INSERT INTO last_seen VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET ts=excluded.ts", (g.id, m.id, now))
                elif now - ts >= days * 86400:
                    await self.start(g, m, f"Нет сообщений и голоса {days} дн.", 0, auto=True)

    @check.before_loop
    @inactive_check.before_loop
    async def _w(self): await self.bot.wait_until_ready()

async def setup(bot): await bot.add_cog(AFK(bot))
