import discord
from discord import app_commands
from discord.ext import commands
import media
from utils import staff_only, staff_or_permission, make, COLOR

KINDS = [app_commands.Choice(name="Машина", value="car"), app_commands.Choice(name="Контракт", value="contract"),
         app_commands.Choice(name="Заявка", value="application"), app_commands.Choice(name="Участник", value="member")]

class Photos(commands.Cog):
    def __init__(self, bot): self.bot = bot

    async def _store(self, i, kind, ref, photo):
        try:
            await media.save(self.bot.db, i.guild_id, kind, ref, photo)
            return True
        except ValueError as ex:
            await i.followup.send(f"❌ {ex}", ephemeral=True)
            return False

    async def _done(self, i, kind, ref, title, text):
        e = make(title, text, COLOR["ok"])
        files = await media.attach(self.bot.db, i.guild_id, kind, ref, e)
        await i.followup.send(embed=e, ephemeral=True, **media.fk(files))

    async def _app_message(self, guild, app):
        ch = await self.bot.get_ch(guild, "applications_channel")
        try:
            return await ch.fetch_message(app["message_id"])
        except (AttributeError, discord.HTTPException):
            return None

    @app_commands.command(name="фото_машины", description="Добавить или заменить фото машины")
    @app_commands.rename(car_id="машина", photo="фото")
    @app_commands.describe(car_id="Номер машины", photo="Новое фото машины")
    @staff_or_permission("photos_manage")
    async def car_photo(self, i, car_id: int, photo: discord.Attachment):
        await i.response.defer(ephemeral=True)
        c = await self.bot.db.fetchone("SELECT * FROM vehicles WHERE id=? AND guild_id=?", (car_id, i.guild_id))
        if not c: return await i.followup.send("Машина не найдена.", ephemeral=True)
        if await self._store(i, "car", car_id, photo):
            await self.bot.log(i.guild, "fleet", "Фото машины обновлено", f"#{car_id} {c['name']}", i.user)
            await self.bot.get_cog("Fleet").refresh_card(i.guild, car_id, upload=True)
            await self._done(i, "car", car_id, "📸 Фото машины обновлено", f"**{c['name']}** `{c['plate']}`")

    @app_commands.command(name="фото_контракта", description="Добавить фото/скрин к контракту")
    @app_commands.rename(contract_id="контракт", photo="фото")
    @app_commands.describe(contract_id="Номер контракта", photo="Фото/скрин контракта")
    @staff_or_permission("photos_manage")
    async def contract_photo(self, i, contract_id: int, photo: discord.Attachment):
        await i.response.defer(ephemeral=True)
        c = await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=? AND guild_id=?", (contract_id, i.guild_id))
        if not c: return await i.followup.send("Контракт не найден.", ephemeral=True)
        if await self._store(i, "contract", contract_id, photo):
            await self.bot.get_cog("Contracts").refresh(i.guild, contract_id)
            await self.bot.log(i.guild, "contract", f"Фото к контракту #{contract_id}", c["title"], i.user)
            await self._done(i, "contract", contract_id, "📸 Фото добавлено к контракту", f"№{contract_id} · **{c['title']}**")

    @app_commands.command(name="фото_заявки", description="Добавить фото/скрин к заявке")
    @app_commands.rename(app_id="заявка", photo="фото")
    @app_commands.describe(app_id="Номер заявки", photo="Фото/скрин заявки")
    @staff_or_permission("photos_manage")
    async def application_photo(self, i, app_id: int, photo: discord.Attachment):
        await i.response.defer(ephemeral=True)
        a = await self.bot.db.fetchone("SELECT * FROM applications WHERE id=? AND guild_id=?", (app_id, i.guild_id))
        if not a: return await i.followup.send("Заявка не найдена.", ephemeral=True)
        if await self._store(i, "application", app_id, photo):
            msg = await self._app_message(i.guild, a)
            if msg and msg.embeds:
                e = msg.embeds[0]
                files = await media.attach(self.bot.db, i.guild_id, "application", app_id, e)
                await msg.edit(embed=e, attachments=files)
            await self.bot.log(i.guild, "application", f"Фото к заявке #{app_id}", f"<@{a['user_id']}>", i.user)
            await self._done(i, "application", app_id, "📸 Фото добавлено к заявке", f"Заявка №{app_id} · <@{a['user_id']}>")

    @app_commands.command(name="фото_участника", description="Добавить фото в карточку участника")
    @app_commands.rename(member="участник", photo="фото")
    @app_commands.describe(member="Чьё фото", photo="Новое фото участника")
    @staff_or_permission("photos_manage")
    async def member_photo(self, i, member: discord.Member, photo: discord.Attachment):
        await i.response.defer(ephemeral=True)
        if await self._store(i, "member", member.id, photo):
            await self.bot.log(i.guild, "family", "Фото участника обновлено", member.mention, i.user)
            await self._done(i, "member", member.id, "📸 Фото участника обновлено", member.mention)

    @app_commands.command(name="фото_удалить", description="Удалить фото")
    @app_commands.rename(kind="тип", number="номер", member="участник")
    @app_commands.describe(kind="Что за фото", number="Номер машины / контракта / заявки", member="Для типа «Участник»")
    @app_commands.choices(kind=KINDS)
    @staff_or_permission("photos_manage")
    async def photo_remove(self, i, kind: app_commands.Choice[str], number: int = None, member: discord.Member = None):
        ref = member.id if kind.value == "member" else number
        if ref is None:
            return await i.response.send_message("Укажите номер (или участника для типа «Участник»).", ephemeral=True)
        await i.response.defer(ephemeral=True)
        if not await media.remove(self.bot.db, i.guild_id, kind.value, ref):
            return await i.followup.send("Фото не найдено.", ephemeral=True)
        if kind.value == "contract":
            await self.bot.get_cog("Contracts").refresh(i.guild, ref)
        elif kind.value == "car":
            await self.bot.get_cog("Fleet").refresh_card(i.guild, ref, upload=True)
        elif kind.value == "application":
            a = await self.bot.db.fetchone("SELECT * FROM applications WHERE id=? AND guild_id=?", (ref, i.guild_id))
            msg = await self._app_message(i.guild, a) if a else None
            if msg and msg.embeds:
                e = msg.embeds[0]; e.set_image(url=None)
                await msg.edit(embed=e, attachments=[])
        cat = {"car": "fleet", "contract": "contract", "application": "application", "member": "family"}[kind.value]
        await self.bot.log(i.guild, cat, "Фото удалено", f"{kind.name} · {member.mention if member else '№' + str(ref)}", i.user, 0xED4245)
        await i.followup.send("🗑 Фото удалено.", ephemeral=True)

async def setup(bot): await bot.add_cog(Photos(bot))
