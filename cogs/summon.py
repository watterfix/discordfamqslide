import asyncio, discord
from discord import app_commands
from discord.ext import commands
from utils import staff_only, make, COLOR


class SummonPicker(discord.ui.View):
    """Меню выбора войса для созыва (видит только нажавший админ)."""
    def __init__(self, cog):
        super().__init__(timeout=180)
        self.cog, self.dm = cog, True
        self.select = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.voice, discord.ChannelType.stage_voice],
                                               placeholder="В какой войс созвать всех?", min_values=1, max_values=1)
        self.select.callback = self.picked
        self.add_item(self.select)

    @discord.ui.button(label="Звать в ЛС тех, кто не в войсе: ВКЛ", style=discord.ButtonStyle.secondary, emoji="📩", row=1)
    async def toggle(self, i, b):
        self.dm = not self.dm
        b.label = f"Звать в ЛС тех, кто не в войсе: {'ВКЛ' if self.dm else 'ВЫКЛ'}"
        await i.response.edit_message(view=self)

    async def picked(self, i):
        if not (await self.cog.bot.is_staff(i.user) or await self.cog.bot.has_role_permission(i.user, "summon_manage")):
            return await i.response.send_message("🔒 Созыв доступен только руководству или роли с правом «Проводить обзвон/созыв».", ephemeral=True)
        await i.response.edit_message(content="📣 Созываю…", view=None)
        vc = i.guild.get_channel(self.select.values[0].id) or self.select.values[0]
        text = await self.cog.summon(i.guild, i.user, vc, self.dm)
        await i.edit_original_response(content=text)
        self.stop()


class SummonPanel(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Созвать всех в войс", style=discord.ButtonStyle.danger, emoji="📣", custom_id="summon:open")
    async def open(self, i, b):
        if not (await self.cog.bot.is_staff(i.user) or await self.cog.bot.has_role_permission(i.user, "summon_manage")):
            return await i.response.send_message("🔒 Созыв доступен только руководству или роли с правом «Проводить обзвон/созыв».", ephemeral=True)
        await i.response.send_message("Выберите войс — все участники семьи, сидящие в голосовых, будут перемещены туда:",
                                      view=SummonPicker(self.cog), ephemeral=True)


class Summon(commands.Cog):
    def __init__(self, bot): self.bot = bot

    async def cog_load(self):
        self.bot.add_view(SummonPanel(self))

    async def roster_ids(self, guild):
        """Состав семьи: роль принятых + таблица members. None — если ни то ни другое не настроено (тогда зовём всех)."""
        ids = {r["user_id"] for r in await self.bot.db.fetchall("SELECT user_id FROM members WHERE guild_id=?", (guild.id,))}
        rid = await self.bot.db.get_setting(guild.id, "accepted_role")
        role = guild.get_role(int(rid)) if rid else None
        if role: ids |= {m.id for m in role.members}
        return ids or None

    async def summon(self, guild, staff, vc, dm=True):
        db = self.bot.db
        roster = await self.roster_ids(guild)
        afk = {r["user_id"] for r in await db.fetchall("SELECT user_id FROM afk WHERE guild_id=? AND status='active' AND auto=0", (guild.id,))}
        moved = already = failed = dmed = nodm = 0
        outside = []
        for m in guild.members:
            if m.bot or m.id in afk or (roster is not None and m.id not in roster):
                continue
            if m.voice and m.voice.channel:
                if m.voice.channel.id == vc.id:
                    already += 1; continue
                try:
                    await m.move_to(vc, reason=f"Созыв: {staff}")
                    moved += 1
                except discord.HTTPException:
                    failed += 1
            elif dm and roster is not None:
                outside.append(m)
        for m in outside:   # ЛС тем, кто не в голосе (с паузой, чтобы не упереться в лимиты)
            try:
                await m.send(f"📣 **{guild.name}**: {staff.display_name} созывает всех в {vc.mention}.\nЗаходите: {vc.jump_url}")
                dmed += 1
            except discord.HTTPException:
                nodm += 1
            await asyncio.sleep(0.6)
        await self.bot.log(guild, "summon", "Созыв в войс",
                           f"{vc.mention}\nПеремещено: {moved} · уже там: {already} · не удалось: {failed}\nЛС: отправлено {dmed}, закрыты {nodm}",
                           staff, 0xE67E22)
        out = f"✅ Созыв в {vc.mention}: перемещено **{moved}**, уже там **{already}**"
        if failed: out += f", не удалось **{failed}** (проверьте право бота «Перемещать участников»)"
        if dm and roster is not None: out += f"\n📩 ЛС: отправлено **{dmed}**, закрыты у **{nodm}**"
        return out

    @app_commands.command(name="созыв_панель", description="Отправить в админ-чат кнопку «Созвать всех в войс»")
    @app_commands.rename(channel="канал")
    @app_commands.describe(channel="Админ-чат, куда отправить кнопку")
    @staff_only()
    async def panel(self, i, channel: discord.TextChannel):
        e = make("📣 Созыв", "Нажмите кнопку, выберите голосовой канал — все участники, сидящие в войсах, "
                             "переместятся туда, остальным придёт приглашение в ЛС.", COLOR["orange"])
        await channel.send(embed=e, view=SummonPanel(self))
        await self.bot.log(i.guild, "summon", "Панель созыва отправлена", channel.mention, i.user)
        await i.response.send_message(f"Панель отправлена в {channel.mention}", ephemeral=True)

    @app_commands.command(name="созыв", description="Созвать всех в указанный войс")
    @app_commands.rename(channel="канал", dm="в_лс")
    @app_commands.describe(channel="Войс, куда созвать всех", dm="Звать в ЛС тех, кто не в войсе")
    @staff_only()
    async def summon_cmd(self, i, channel: discord.VoiceChannel, dm: bool = True):
        await i.response.defer(ephemeral=True)
        await i.followup.send(await self.summon(i.guild, i.user, channel, dm), ephemeral=True)

async def setup(bot): await bot.add_cog(Summon(bot))
