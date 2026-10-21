"""Пользовательская панель: все возможности для обычных участников — вечные кнопки (работают после перезапуска бота)."""
import discord
import logging
import re
from discord import app_commands
from discord.ext import commands
import media
from cogs.afk import AfkModal
from cogs.fleet import TakeModal
from cogs.contracts import ContractCardView, calculate_contract_amount
from utils import staff_only, staff_or_permission, make, money, COLOR, FAMILY


def num(text):
    """'1 500,5' -> 1500.5 (или None, если не число)."""
    try:
        return float(text.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


class ContractModal(discord.ui.Modal, title="Контракт по номеру"):
    number = discord.ui.TextInput(label="Номер контракта", max_length=9, placeholder="12")

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, i):
        if not self.number.value.strip().isdigit():
            return await i.response.send_message("Введите номер контракта цифрами.", ephemeral=True)
        c = await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=? AND guild_id=?", (int(self.number.value), i.guild_id))
        if not c:
            return await i.response.send_message("Контракт не найден.", ephemeral=True)
        e, files = await self.bot.get_cog("Contracts").info_embed(i.guild_id, c)
        await i.response.send_message(embed=e, ephemeral=True, **media.fk(files))


class DepositModal(discord.ui.Modal, title="Внести в общак"):
    amount = discord.ui.TextInput(label="Сумма", max_length=15, placeholder="50000")
    note = discord.ui.TextInput(label="Комментарий", required=False, max_length=100)

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, i):
        a = num(self.amount.value)
        if a is None or not 1 <= a <= 1e12:
            return await i.response.send_message("Введите сумму числом, например 50000.", ephemeral=True)
        e = await self.bot.get_cog("Family").do_deposit(i.guild, i.user, a, self.note.value)
        await i.response.send_message(embed=e, ephemeral=True)


class VoiceModal(discord.ui.Modal, title="Свой временный войс"):
    name = discord.ui.TextInput(label="Название (необязательно)", required=False, max_length=60)
    limit = discord.ui.TextInput(label="Лимит участников (0 — без лимита)", required=False, default="0", max_length=2)

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, i):
        lim = int(self.limit.value) if self.limit.value.strip().isdigit() else 0
        await i.response.defer(ephemeral=True)
        hub = await self.bot.get_ch(i.guild, "tempvoice_hub")
        ch, err = await self.bot.get_cog("TempVoice").create(
            i.guild, i.user, self.name.value or f"🔊 {i.user.display_name}", lim, hub.category if hub else None)
        await i.followup.send(err or f"✅ {ch.mention} создан. Зайдите в течение **60 сек.**, иначе он удалится.", ephemeral=True)


class TempVoicePanelView(discord.ui.View):
    """Постоянная отдельная панель создания временных голосовых каналов."""
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Создать временный войс", emoji="🎙️", style=discord.ButtonStyle.primary,
                       custom_id="tempvoice:panel:create")
    async def create_voice(self, i: discord.Interaction, button: discord.ui.Button):
        if not i.guild or not isinstance(i.user, discord.Member):
            return await i.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
        await i.response.send_modal(VoiceModal(self.bot))


class CarSelect(discord.ui.View):
    """Выпадающий список машин: взять свободную или сдать свою."""
    def __init__(self, bot, cars, take):
        super().__init__(timeout=120)
        self.bot, self.take = bot, take
        self.select = discord.ui.Select(
            placeholder="Какую машину взять?" if take else "Какую машину сдать?",
            options=[discord.SelectOption(label=c["name"][:100], description=f"Номер {c['plate']}", value=str(c["id"])) for c in cars[:25]])
        self.select.callback = self.pick
        self.add_item(self.select)

    async def pick(self, i):
        cog = self.bot.get_cog("Fleet")
        car = await self.bot.db.fetchone("SELECT * FROM vehicles WHERE id=? AND guild_id=?", (int(self.select.values[0]), i.guild_id))
        if not car:
            return await i.response.edit_message(content="Машина не найдена.", view=None)
        if self.take:   # спрашиваем, на сколько часов; дальше работает TakeModal
            err = cog.take_error(car)
            if err:
                return await i.response.edit_message(content=err, view=None)
            await i.response.send_modal(TakeModal(self.bot, car["id"]))
            return self.stop()
        err = await cog.do_return(i.guild, i.user, car)
        if err:
            return await i.response.edit_message(content=err, view=None)
        await cog.refresh_card(i.guild, car["id"])
        await i.response.edit_message(content=f"✅ **{car['name']}** возвращена.", view=None)
        self.stop()


class StaffView(discord.ui.View):
    """Базовый класс меню руководства: любое нажатие проверяется на права."""
    def __init__(self, bot, timeout=300):
        super().__init__(timeout=timeout)
        self.bot = bot

    async def interaction_check(self, i):
        if await self.bot.has_panel_access(i.user):
            return True
        await i.response.send_message("🔒 Эта административная панель доступна только администраторам и ролям, которым разрешён доступ.", ephemeral=True)
        return False


class AfkPullPicker(StaffView):
    def __init__(self, bot, guild, rows):
        super().__init__(bot, 180)
        opts = []
        for r in rows[:25]:
            m = guild.get_member(r["user_id"])
            opts.append(discord.SelectOption(label=(m.display_name if m else f"ID {r['user_id']}")[:100], value=str(r["id"]),
                                             description=("🔕 неактивен (авто-АФК)" if r["auto"] else f"💤 {r['reason']}")[:100]))
        self.select = discord.ui.Select(placeholder="Кого вытащить из АФК?", options=opts)
        self.select.callback = self.pick
        self.add_item(self.select)

    async def pick(self, i):
        row = await self.bot.db.fetchone("SELECT * FROM afk WHERE id=? AND guild_id=? AND status='active'", (int(self.select.values[0]), i.guild_id))
        if not row:
            return await i.response.edit_message(content="Этот АФК уже завершён.", view=None)
        await i.response.defer()
        sent = await self.bot.get_cog("AFK").pull(i.guild, i.user, row)
        await i.edit_original_response(content=f"✅ <@{row['user_id']}> вытащен из АФК." + ("" if sent else "\n⚠️ ЛС закрыты — он не получил уведомление."), view=None)
        self.stop()


class CarTakeAwayPicker(StaffView):
    def __init__(self, bot, guild, cars):
        super().__init__(bot, 180)
        opts = []
        for c in cars[:25]:
            m = guild.get_member(c["holder_id"])
            opts.append(discord.SelectOption(label=c["name"][:100], value=str(c["id"]),
                                             description=f"{c['plate']} · у {m.display_name if m else c['holder_id']}"[:100]))
        self.select = discord.ui.Select(placeholder="Какую машину забрать?", options=opts)
        self.select.callback = self.pick
        self.add_item(self.select)

    async def pick(self, i):
        if not (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "fleet_take_away")):
            return await i.response.send_message("🔒 Принудительно забирать машины могут только руководство и назначенные роли.", ephemeral=True)
        car = await self.bot.db.fetchone("SELECT * FROM vehicles WHERE id=? AND guild_id=?", (int(self.select.values[0]), i.guild_id))
        if not car:
            return await i.response.edit_message(content="Машина не найдена.", view=None)
        await i.response.defer()
        holder = car["holder_id"]
        err, sent = await self.bot.get_cog("Fleet").take_away(i.guild, i.user, car)
        await i.edit_original_response(
            content=err or (f"✅ **{car['name']}** `{car['plate']}` забрана у <@{holder}> и возвращена в автопарк." + ("" if sent else "\n⚠️ ЛС закрыты — он не получил уведомление.")),
            view=None)
        self.stop()




class ContractDetailsModal(discord.ui.Modal, title="Данные контракта"):
    title_text = discord.ui.TextInput(label="Название контракта", max_length=100, placeholder="Например: Доставка груза")
    honorariums = discord.ui.TextInput(label="Гонорары через запятую", required=True, max_length=200, placeholder="10000, 15000 — по порядку участников")

    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard

    async def on_submit(self, i):
        if not self.title_text.value.strip():
            return await i.response.send_message("Название контракта не может быть пустым.", ephemeral=True)
        fees = []
        for part in self.honorariums.value.split(","):
            val = num(part.strip())
            if val is None or val < 0:
                return await i.response.send_message("Гонорары должны быть числами через запятую.", ephemeral=True)
            fees.append(val)
        if len(fees) != len(self.wizard.member_ids):
            return await i.response.send_message(f"Нужно указать ровно {len(self.wizard.member_ids)} гонорар(ов) — по одному на каждого выбранного участника, в том же порядке.", ephemeral=True)
        if sum(fees) <= 0:
            return await i.response.send_message("Сумма персональных гонораров должна быть больше нуля.", ephemeral=True)
        percent = float(await self.wizard.bot.db.get_setting(self.wizard.guild_id, "contract_percent") or 0)
        self.wizard.data = {"title": self.title_text.value.strip(), "fees": fees, "percent": percent}
        await i.response.send_message(embed=self.wizard.preview_embed(), view=ContractWizardConfirmView(self.wizard))


class ContractWizardView(discord.ui.View):
    def __init__(self, bot, guild_id, member_ids, creator_id):
        super().__init__(timeout=900)
        self.bot, self.guild_id = bot, guild_id
        self.member_ids, self.creator_id = member_ids, creator_id
        self.data = None

    def preview_embed(self):
        guild = self.bot.get_guild(self.guild_id)
        names = []
        for uid in self.member_ids:
            member = guild.get_member(uid) if guild else None
            names.append(f"• {member.mention if member else f'<@{uid}>'}")
        data = self.data or {}
        desc = (f"**Название:** {data.get('title','—')}\n"
                f"**Исполнители ({len(self.member_ids)}):**\n" + "\n".join(names))
        if data.get('fees'):
            fees_total = sum(data['fees'])
            desc += "\n\n**Гонорары:**\n" + "\n".join(f"{names[n]} — {money(fee)}" for n, fee in enumerate(data['fees']))
            desc += f"\n**Всего гонораров:** {money(fees_total)}"
            percent = float(data.get("percent", 0))
            amount, family_amount = calculate_contract_amount(fees_total, percent)
            desc += f"\n**Процент семьи:** {percent:g}% от гонораров\n**Доля семьи:** {money(family_amount)}\n**Общая сумма контракта:** {money(amount)}"
        else:
            desc += "\n\nГонорары будут рассчитаны по настройкам сервера."
        return make("📄 Проверьте контракт", desc, COLOR["main"])

    @discord.ui.button(label="Заполнить данные", emoji="📝", style=discord.ButtonStyle.primary)
    async def fill(self, i, button):
        if i.user.id != self.creator_id:
            return await i.response.send_message("Эта форма принадлежит другому пользователю.", ephemeral=True)
        await i.response.send_modal(ContractDetailsModal(self))


class ContractWizardConfirmView(discord.ui.View):
    def __init__(self, wizard):
        super().__init__(timeout=900)
        self.wizard = wizard

    @discord.ui.button(label="Подтвердить и создать", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, i, button):
        w = self.wizard
        if i.user.id != w.creator_id:
            return await i.response.send_message("Эта форма принадлежит другому пользователю.", ephemeral=True)
        if not w.data:
            return await i.response.send_message("Сначала заполните данные кнопкой «Заполнить данные».", ephemeral=True)
        guild = w.bot.get_guild(w.guild_id)
        if not guild:
            return await i.response.send_message("Сервер больше недоступен боту. Начните создание заново.", ephemeral=True)
        members = []
        for uid in w.member_ids:
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return await i.response.send_message("Один из выбранных участников не найден на сервере. Проверьте intents участников и попробуйте создать контракт заново.", ephemeral=True)
            members.append(member)
        ch = await w.bot.get_ch(guild, "contracts_channel")
        if not ch:
            return await i.response.send_message("Сначала задайте канал контрактов: `/настроить_канал` → Контракты.", ephemeral=True)
        await i.response.defer()
        import time
        from cogs.contracts import pack, pings
        data = w.data
        cid = None
        try:
            percent = float(await w.bot.db.get_setting(guild.id, "contract_percent") or 0)
            fees_total = sum(data["fees"])
            amount, family_amount = calculate_contract_amount(fees_total, percent)
            fees_raw = "|".join(f"{uid}:{fee:.2f}" for uid, fee in zip(w.member_ids, data["fees"])) if data["fees"] else ""
            now = int(time.time())
            cid = await w.bot.db.execute(
                "INSERT INTO contracts(guild_id,title,client,executor_id,executors,creator_id,amount,percent,deadline,created,fees,funded) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
                (guild.id, data["title"], "", w.member_ids[0], pack(w.member_ids), w.creator_id, amount, percent,
                 None, now, fees_raw))
            c = await w.bot.db.fetchone("SELECT * FROM contracts WHERE id=?", (cid,))
            cog = w.bot.get_cog("Contracts")
            if not c or not cog:
                raise RuntimeError("Контракт сохранён, но модуль Contracts не найден или запись не прочиталась")
            e = cog.embed(c)
            files = await media.attach(w.bot.db, guild.id, "contract", cid, e)
            msg = await ch.send(content=pings(c), embed=e, view=ContractCardView(w.bot), **media.fk(files))
            await w.bot.db.execute("UPDATE contracts SET message_id=? WHERE id=?", (msg.id, cid))
            mentions = " ".join(f"<@{uid}>" for uid in w.member_ids)
            try:
                await w.bot.log(guild, "contract", f"Контракт #{cid} создан", f"{data['title']} · сумма контракта {money(amount)} · гонорары {money(fees_total)} · доля семьи {money(family_amount)} · исполнители {mentions}", i.user, 0x57F287)
            except Exception:
                logging.exception("Контракт #%s создан, но лог не записан", cid)
            await i.edit_original_response(content=f"✅ Контракт №{cid} создан: {msg.jump_url}\nСумма контракта: {money(amount)}\nГонорары исполнителей: {money(fees_total)}\nДоля семьи ({percent:g}% от гонораров): {money(family_amount)}\nДолг исполнителей активируется после подтверждения оплаты скриншотом.", embed=None, view=None)
            self.stop()
        except Exception as ex:
            logging.exception("Ошибка создания контракта через панель (guild=%s, creator=%s, contract_id=%s)", guild.id, w.creator_id, cid)
            if cid is None:
                message = "❌ Не удалось сохранить контракт. Ошибка записана в консоль бота; проверьте, что база данных обновлена и бот имеет доступ к ней."
            else:
                message = f"⚠️ Контракт №{cid} уже записан в базу, но публикация карточки не завершилась. Не нажимайте подтверждение повторно; проверьте канал контрактов и консоль бота."
            try:
                await i.edit_original_response(content=message, embed=None, view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Отменить", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, i, button):
        if i.user.id != self.wizard.creator_id:
            return await i.response.send_message("Эта форма принадлежит другому пользователю.", ephemeral=True)
        await i.response.edit_message(content="Создание контракта отменено.", embed=None, view=None)
        self.stop()


class ContractMemberPicker(discord.ui.View):
    def __init__(self, bot, creator_id):
        super().__init__(timeout=180)
        self.bot, self.creator_id = bot, creator_id
        self.select = discord.ui.UserSelect(placeholder="Выберите исполнителей (до 10)", min_values=1, max_values=10)
        self.select.callback = self.picked
        self.add_item(self.select)

    async def picked(self, i):
        if i.user.id != self.creator_id:
            return await i.response.send_message("Эта форма принадлежит другому пользователю.", ephemeral=True)
        if not i.guild:
            return await i.response.send_message("Выбор участников нужно начать на сервере.", ephemeral=True)
        member_ids = []
        for user in self.select.values:
            member = user if isinstance(user, discord.Member) else i.guild.get_member(user.id)
            if member is None:
                try:
                    member = await i.guild.fetch_member(user.id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
            if member.id not in member_ids:
                member_ids.append(member.id)
        if not member_ids:
            return await i.response.send_message("Не удалось определить участников сервера. Попробуйте ещё раз.", ephemeral=True)
        try:
            dm = await i.user.create_dm()
            wizard = ContractWizardView(self.bot, i.guild_id, member_ids, i.user.id)
            await dm.send("📝 **Создание контракта**\nУчастники выбраны. Нажмите кнопку ниже, чтобы заполнить название и персональные гонорары. После заполнения проверьте карточку и подтвердите создание.", view=wizard)
        except discord.Forbidden:
            return await i.response.send_message("Не могу написать вам в ЛС. Разрешите личные сообщения от участников сервера и нажмите кнопку создания ещё раз.", ephemeral=True)
        await i.response.edit_message(content="✅ Участники выбраны. Я отправил форму создания контракта вам в личные сообщения.", view=None)
        self.stop()


class ContractClosePicker(StaffView):
    """Выбор открытого контракта для закрытия из панели. Доступ проверяется по contracts_manage."""
    def __init__(self, bot, guild, rows):
        super().__init__(bot, 180)
        opts = []
        for c in rows[:25]:
            title = (c["title"] or f"Контракт №{c['id']}")[:90]
            debt = max(0, round(float(c["amount"] or 0) + float(c["extra"] or 0) - float(c["paid"] or 0), 2))
            opts.append(discord.SelectOption(label=f"№{c['id']} · {title}"[:100], value=str(c["id"]), description=f"Остаток: {debt:,.2f}"[:100]))
        self.select = discord.ui.Select(placeholder="Выберите контракт для закрытия", min_values=1, max_values=1, options=opts)
        self.select.callback = self.pick
        self.add_item(self.select)

    async def interaction_check(self, i):
        if await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "contracts_manage"):
            return True
        await i.response.send_message("🔒 Закрывать контракты могут только руководство и роли с правом «Редактировать контракты». ", ephemeral=True)
        return False

    async def pick(self, i):
        cid = int(self.select.values[0])
        c = await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=? AND guild_id=? AND status='open'", (cid, i.guild_id))
        if not c:
            return await i.response.edit_message(content="Этот контракт уже закрыт или не найден.", view=None)
        from cogs.contracts import left
        debt = left(c)
        if debt > 0:
            return await i.response.edit_message(content=f"⚠️ Контракт №{cid} нельзя закрыть: остаток долга **{money(debt)}**. Сначала погасите долг. Если требуется принудительное закрытие, используйте `/контракт_закрыть` с параметром `принудительно: Да`.", view=None)
        await self.bot.db.execute("UPDATE contracts SET status='closed' WHERE id=? AND guild_id=?", (cid, i.guild_id))
        cog = self.bot.get_cog("Contracts")
        if cog:
            await cog.drop_card(i.guild, c)
        await self.bot.log(i.guild, "contract", f"Контракт №{cid} закрыт через панель", f"Закрыл: {i.user.mention}", i.user, 0x99AAB5)
        await i.response.edit_message(content=f"🔒 Контракт №{cid} — **{c['title']}** закрыт.", view=None)


class ContractAdminView(discord.ui.View):
    def __init__(self, bot, timeout=300):
        super().__init__(timeout=timeout)
        self.bot = bot

    @discord.ui.button(label="Создать контракт", emoji="📝", style=discord.ButtonStyle.success, custom_id="contracts_panel:create", row=0)
    async def create_contract(self, i, b):
        if not (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "contracts_create")):
            return await i.response.send_message("🔒 Создавать контракты могут только администраторы, руководство или участники с правом «Создавать контракты».", ephemeral=True)
        await i.response.send_message("**Шаг 1/2 — выберите исполнителей.** После выбора я отправлю форму создания контракта вам в личные сообщения, где вы укажете персональные гонорары и срок в часах.", view=ContractMemberPicker(self.bot, i.user.id), ephemeral=True)

    @discord.ui.button(label="Закрыть контракт", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="contracts_panel:close", row=0)
    async def close_contract(self, i, b):
        if not (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "contracts_manage")):
            return await i.response.send_message("🔒 Эта кнопка доступна только руководству и ролям, которым выдано право «Редактировать контракты» в `/панель` → «Права ролей».", ephemeral=True)
        rows = await self.bot.db.fetchall("SELECT id, title, amount, extra, paid FROM contracts WHERE guild_id=? AND status='open' ORDER BY id DESC", (i.guild_id,))
        if not rows:
            return await i.response.send_message("Открытых контрактов для закрытия нет.", ephemeral=True)
        view = ContractClosePicker(self.bot, i.guild, rows)
        extra = f"\nПоказаны первые 25 из {len(rows)} открытых контрактов." if len(rows) > 25 else ""
        await i.response.send_message("🔒 Выберите открытый контракт для закрытия. Контракты с непогашенным долгом через эту кнопку закрыть нельзя." + extra, view=view, ephemeral=True)

ROLE_PERMISSION_CHOICES = {
    "fine_issue": "⚖️ Выдавать штрафы",
    "contracts_create": "📝 ОТДЕЛЬНО: Создавать контракты",
    "contracts_manage": "📄 Закрывать/редактировать контракты",
    "contracts_review": "🧾 Проверять оплату контракта (скриншот)",
    "treasury_contract_review": "💳 Одобрять/отклонять оплату контрактов",
    "treasury_fine_review": "⚖️ Одобрять/отклонять оплату штрафов",
    "applications_review": "📨 Рассматривать/принимать/отклонять заявки",
    "summon_manage": "📣 Проводить обзвон/созыв в войс",
    "vzp_create": "📢 ОТДЕЛЬНО: Создавать объявления ВЗП",
    "afk_pull": "💤 Принудительно возвращать из АФК",
    "fleet_take_away": "🚗 Принудительно забирать выданный автопарк",
    "fleet_return_any": "🚘 Возвращать чужие машины",
    "fleet_manage": "🛠 Управлять автопарком (добавить/списать/статус)",
    "treasury_deposit": "🏦 Вносить деньги в общак",
    "treasury_withdraw": "🏦 Снимать деньги из общака",
    "family_manage": "👥 Управлять составом семьи и рангами",
    "photos_manage": "📸 Управлять фотографиями",
}

class RolePermissionView(StaffView):
    def __init__(self, bot, guild):
        super().__init__(bot, 300)
        self.guild_id = guild.id
        self.add_item(RolePermissionSelect(bot, guild))

class RolePermissionSelect(discord.ui.RoleSelect):
    def __init__(self, bot, guild):
        super().__init__(placeholder="Выберите роль, которой выдаются права", min_values=1, max_values=1)
        self.bot, self.guild = bot, guild

    async def callback(self, i):
        role = self.values[0]
        granted = set()
        for key in ROLE_PERMISSION_CHOICES:
            raw = await self.bot.db.get_setting(i.guild_id, f"role_permission:{key}")
            if raw and str(role.id) in raw.split(","):
                granted.add(key)
        view = PermissionGrantView(self.bot, role, granted)
        await i.response.send_message(f"Настройка прав для роли {role.mention}. Отметьте разрешённые действия и нажмите «Сохранить».", view=view, ephemeral=True)

class PermissionGrantView(StaffView):
    def __init__(self, bot, role, granted=None):
        super().__init__(bot, 300)
        self.role = role
        self.granted = set(granted or [])
        self.select = discord.ui.Select(
            placeholder="Выберите права (можно несколько)", min_values=0,
            max_values=len(ROLE_PERMISSION_CHOICES),
            options=[discord.SelectOption(label=label, value=key, default=key in self.granted) for key, label in ROLE_PERMISSION_CHOICES.items()])
        self.select.callback = self.change
        self.add_item(self.select)

    async def change(self, i):
        self.granted = set(self.select.values)
        await i.response.defer(ephemeral=True)

    @discord.ui.button(label="Сохранить права", emoji="💾", style=discord.ButtonStyle.success)
    async def save(self, i, b):
        for key in ROLE_PERMISSION_CHOICES:
            raw = await self.bot.db.get_setting(i.guild_id, f"role_permission:{key}")
            ids = {int(x) for x in raw.split(",") if x.strip()} if raw else set()
            if key in self.granted: ids.add(self.role.id)
            else: ids.discard(self.role.id)
            await self.bot.db.set_setting(i.guild_id, f"role_permission:{key}", ",".join(map(str, sorted(ids))))
        await self.bot.log(i.guild, "server", "Права роли обновлены", f"Роль {self.role.mention}: " + (", ".join(ROLE_PERMISSION_CHOICES[k] for k in self.granted) if self.granted else "специальные права сняты"), i.user)
        await i.response.send_message(f"✅ Права роли {self.role.mention} сохранены. Для снятия всех специальных прав оставьте список пустым и сохраните.", ephemeral=True)


class FinancialResetSelect(discord.ui.Select):
    def __init__(self, bot):
        self.bot = bot
        options = [
            discord.SelectOption(label="Сбросить статистику общака", value="fund", description="Баланс, всего внесено/снято и последние операции станут нулевыми"),
            discord.SelectOption(label="Сбросить зачисления через панель", value="credited", description="Обнулить счётчик старых зачислений в панели казны"),
            discord.SelectOption(label="Снять все активные штрафы", value="fines", description="Все активные штрафы сервера будут отменены"),
            discord.SelectOption(label="Закрыть все открытые контракты", value="contracts", description="Открытые долги по контрактам перестанут учитываться"),
            discord.SelectOption(label="Полный сброс финансов", value="all", description="Общак, зачисления, активные штрафы и открытые контракты"),
        ]
        super().__init__(placeholder="Что нужно очистить?", min_values=1, max_values=1, options=options)

    async def callback(self, i: discord.Interaction):
        self.view.choice = self.values[0]
        labels = {"fund":"статистику общака и историю операций", "credited":"счётчик зачислений через панель", "fines":"все активные штрафы", "contracts":"все открытые контракты и их долги", "all":"ВСЕ финансовые показатели, активные штрафы и открытые контракты"}
        await i.response.edit_message(content=(f"⚠️ **Подтвердите действие: {labels[self.view.choice]}**.\n"
            "Это действие нельзя автоматически отменить. История платежей/заявок для аудита не удаляется; активные штрафы отменяются, контракты закрываются."), view=self.view)


class FinancialResetView(StaffView):
    def __init__(self, bot):
        super().__init__(bot, 120)
        self.choice = None
        self.add_item(FinancialResetSelect(bot))

    @discord.ui.button(label="Подтвердить очистку", emoji="🧹", style=discord.ButtonStyle.danger)
    async def confirm_reset(self, i: discord.Interaction, button: discord.ui.Button):
        if not self.choice:
            return await i.response.send_message("Сначала выберите, что очистить.", ephemeral=True)
        g = i.guild_id
        choice = self.choice
        db = self.bot.db
        # Acknowledge the component interaction before database updates and panel refreshes;
        # Discord expires unacknowledged interactions after a few seconds.
        await i.response.defer()
        try:
            if choice in ("fund", "all"):
                await db.execute("DELETE FROM common_fund_transactions WHERE guild_id=?", (g,))
            if choice in ("credited", "all"):
                await db.execute("DELETE FROM treasury WHERE guild_id=?", (g,))
            if choice in ("fines", "all"):
                await db.execute("UPDATE fines SET cancelled=1 WHERE guild_id=? AND COALESCE(cancelled,0)=0", (g,))
            if choice in ("contracts", "all"):
                await db.execute("UPDATE contracts SET status='closed' WHERE guild_id=? AND status!='closed'", (g,))
            treasury_cog = self.bot.get_cog("Treasury")
            if treasury_cog:
                await treasury_cog.refresh_panel(i.guild)
                await treasury_cog.refresh_common_fund_panel(i.guild)
            fines_cog = self.bot.get_cog("Fines")
            if fines_cog and choice in ("fines", "all"):
                try: await fines_cog.refresh_panel(i.guild)
                except Exception: logging.exception("Не удалось обновить панель штрафов после очистки")
            contracts_cog = self.bot.get_cog("Contracts")
            if contracts_cog and choice in ("contracts", "all"):
                rows = await db.fetchall("SELECT id FROM contracts WHERE guild_id=?", (g,))
                for row in rows:
                    try: await contracts_cog.refresh(i.guild, row["id"])
                    except Exception: pass
            labels = {"fund":"статистика общака сброшена", "credited":"счётчик зачислений через панель сброшен", "fines":"активные штрафы отменены", "contracts":"открытые контракты закрыты", "all":"финансовые показатели сброшены, активные штрафы отменены, открытые контракты закрыты"}
            await self.bot.log(i.guild, "treasury", "Очистка финансовых данных", labels[choice], i.user, COLOR["bad"])
            await i.edit_original_response(content=f"✅ Готово: {labels[choice]}. История заявок и платежей сохранена для аудита.", view=None)
        except Exception:
            logging.exception("Ошибка очистки финансовых данных guild=%s choice=%s", g, choice)
            try:
                await i.edit_original_response(content="⚠️ Очистка была запущена, но при обновлении данных/панели возникла ошибка. Проверьте консоль бота и состояние финансовых данных.", view=None)
            except discord.HTTPException:
                pass


class StaffMenu(StaffView):
    """Меню руководства (видно только нажавшему): набор, АФК, автопарк."""
    @discord.ui.button(label="Очистить финансовые данные", emoji="🧹", style=discord.ButtonStyle.danger, row=3)
    async def reset_financial_data(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_message(
            "🧹 **Очистка финансовых показателей**\nВыберите, что нужно сбросить. Действие требует отдельного подтверждения; история заявок и платежей сохраняется для аудита.",
            view=FinancialResetView(self.bot), ephemeral=True)

    @discord.ui.button(label="Права ролей", emoji="🔑", style=discord.ButtonStyle.primary, row=2)
    async def role_permissions(self, i, b):
        await i.response.send_message("🔑 **Точечные права для ролей**\nВыберите роль и отметьте только нужные действия. «Создавать контракты» и «Создавать объявления ВЗП» — независимые права: назначайте их разным ролям при необходимости.", view=RolePermissionView(self.bot, i.guild), ephemeral=True)

    @discord.ui.button(label="Контракты: создание и оплата", emoji="📄", style=discord.ButtonStyle.success, row=2)
    async def contract_admin(self, i, b):
        await i.response.send_message("📄 **Управление контрактами**\nСоздание контракта и внесение оплаты доступны только руководству.", view=ContractAdminView(self.bot), ephemeral=True)

    async def _toggle(self, i, is_open):
        await i.response.defer(ephemeral=True, thinking=True)
        note = await self.bot.get_cog("Applications").set_open(i.guild, i.user, is_open)
        await i.followup.send(("🟢 Набор открыт." if is_open else "🔒 Набор закрыт.") + note, ephemeral=True)

    @discord.ui.button(label="Открыть набор", emoji="🟢", style=discord.ButtonStyle.success, row=0)
    async def open_set(self, i, b): await self._toggle(i, True)

    @discord.ui.button(label="Закрыть набор", emoji="🔒", style=discord.ButtonStyle.danger, row=0)
    async def close_set(self, i, b): await self._toggle(i, False)

    @discord.ui.button(label="Вытащить из АФК", emoji="🚪", style=discord.ButtonStyle.primary, row=1)
    async def afk_pull(self, i, b):
        rows = await self.bot.db.fetchall("SELECT * FROM afk WHERE guild_id=? AND status='active' ORDER BY auto, end_ts", (i.guild_id,))
        if not rows:
            return await i.response.send_message("Сейчас никто не в АФК ✅", ephemeral=True)
        extra = f"\n*Показаны первые 25 из {len(rows)}.*" if len(rows) > 25 else ""
        await i.response.send_message(f"🚪 Выберите, кого вытащить из АФК:{extra}", view=AfkPullPicker(self.bot, i.guild, rows), ephemeral=True)

    @discord.ui.button(label="Забрать машину", emoji="🚗", style=discord.ButtonStyle.primary, row=1)
    async def car_away(self, i, b):
        cars = await self.bot.db.fetchall("SELECT * FROM vehicles WHERE guild_id=? AND status='in_use' ORDER BY until_ts IS NULL, until_ts", (i.guild_id,))
        if not cars:
            return await i.response.send_message("Сейчас ни одна машина не занята ✅", ephemeral=True)
        extra = f"\n*Показаны первые 25 из {len(cars)}.*" if len(cars) > 25 else ""
        await i.response.send_message(f"🚗 Выберите машину, которую нужно забрать (владелец получит уведомление):{extra}",
                                      view=CarTakeAwayPicker(self.bot, i.guild, cars), ephemeral=True)


class UserPanel(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    def cog(self, name): return self.bot.get_cog(name)

    async def reply(self, i, e, files=()):
        await i.response.send_message(embed=e, ephemeral=True, **media.fk(list(files)))

    # ---------- АФК ----------
    @discord.ui.button(label="Встать в АФК", emoji="💤", style=discord.ButtonStyle.primary, custom_id="up:afk_start", row=0)
    async def afk_start(self, i, b): await i.response.send_modal(AfkModal(self.bot))

    @discord.ui.button(label="Выйти из АФК", emoji="✅", style=discord.ButtonStyle.success, custom_id="up:afk_end", row=0)
    async def afk_end(self, i, b):
        cog = self.cog("AFK")
        row = await cog.active(i.guild_id, i.user.id)
        if not row:
            return await i.response.send_message("Вы не в АФК.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        await cog.end(row, i.guild, i.user, "кнопка")
        await i.followup.send("С возвращением! 👋", ephemeral=True)

    @discord.ui.button(label="Кто в АФК", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="up:afk_list", row=0)
    async def afk_list(self, i, b): await self.reply(i, await self.cog("AFK").list_embed(i.guild_id))

    @discord.ui.button(label="Администрация", emoji="🛡", style=discord.ButtonStyle.secondary, custom_id="up:staff", row=0)
    async def staff(self, i, b):
        if not await self.bot.has_panel_access(i.user):
            return await i.response.send_message("🔒 Эта панель доступна только администраторам и разрешённым ролям.", ephemeral=True)
        db, g = self.bot.db, i.guild_id
        opened = await db.get_setting(g, "applications_open") != "0"
        afk = (await db.fetchone("SELECT COUNT(*) c FROM afk WHERE guild_id=? AND status='active'", (g,)))["c"]
        cars = (await db.fetchone("SELECT COUNT(*) c FROM vehicles WHERE guild_id=? AND status='in_use'", (g,)))["c"]
        e = make("🛡 Панель руководства", f"📝 Набор: **{'🟢 открыт' if opened else '🔒 закрыт'}**\n💤 В АФК: **{afk}**\n🚗 Машин на руках: **{cars}**", COLOR["orange"])
        await i.response.send_message(embed=e, view=StaffMenu(self.bot), ephemeral=True)

    # ---------- Контракты ----------
    @discord.ui.button(label="Контракты", emoji="📄", style=discord.ButtonStyle.secondary, custom_id="up:contracts", row=1)
    async def contracts(self, i, b): await self.reply(i, await self.cog("Contracts").list_embed(i.guild_id))

    @discord.ui.button(label="Долги", emoji="💸", style=discord.ButtonStyle.secondary, custom_id="up:debts", row=1)
    async def debts(self, i, b): await self.reply(i, await self.cog("Contracts").debts_embed(i.guild_id))

    @discord.ui.button(label="Контракт №…", emoji="🔎", style=discord.ButtonStyle.secondary, custom_id="up:contract_no", row=1)
    async def contract_no(self, i, b): await i.response.send_modal(ContractModal(self.bot))

    @discord.ui.button(label="Моя предоплата", emoji="💳", style=discord.ButtonStyle.secondary, custom_id="up:prepay", row=1)
    async def prepay(self, i, b):
        bal = await self.cog("Contracts").prepaid(i.guild_id, i.user.id)
        await self.reply(i, make("💳 Предоплата", f"{i.user.mention}: **{money(bal)}**\n*Списывается в долг по вашим новым контрактам.*", COLOR["main"]))

    # ---------- Автопарк ----------
    @discord.ui.button(label="Автопарк", emoji="🚗", style=discord.ButtonStyle.secondary, custom_id="up:fleet", row=2)
    async def fleet(self, i, b): await self.reply(i, await self.cog("Fleet").list_embed(i.guild_id))

    @discord.ui.button(label="Взять машину", emoji="🔑", style=discord.ButtonStyle.success, custom_id="up:car_take", row=2)
    async def car_take(self, i, b):
        cars = await self.bot.db.fetchall("SELECT * FROM vehicles WHERE guild_id=? AND status='available' ORDER BY id", (i.guild_id,))
        if not cars:
            return await i.response.send_message("Свободных машин сейчас нет.", ephemeral=True)
        await i.response.send_message(view=CarSelect(self.bot, cars, True), ephemeral=True)

    @discord.ui.button(label="Сдать машину", emoji="🅿️", style=discord.ButtonStyle.danger, custom_id="up:car_return", row=2)
    async def car_return(self, i, b):
        cars = await self.bot.db.fetchall("SELECT * FROM vehicles WHERE guild_id=? AND holder_id=? AND status='in_use'", (i.guild_id, i.user.id))
        if not cars:
            return await i.response.send_message("У вас нет машин на руках.", ephemeral=True)
        await i.response.send_message(view=CarSelect(self.bot, cars, False), ephemeral=True)

    # ---------- Семья, активность ----------
    @discord.ui.button(label="Моя карточка", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="up:me", row=3)
    async def me(self, i, b):
        res = await self.cog("Family").member_embed(i.guild, i.user)
        if not res:
            return await i.response.send_message("Вас ещё нет в составе семьи.", ephemeral=True)
        await self.reply(i, *res)

    @discord.ui.button(label="Состав семьи", emoji="👥", style=discord.ButtonStyle.secondary, custom_id="up:family", row=3)
    async def family(self, i, b): await self.reply(i, await self.cog("Family").family_embed(i.guild))

    @discord.ui.button(label="Моя активность", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="up:activity", row=3)
    async def activity(self, i, b): await self.reply(i, await self.cog("Activity").activity_embed(i.guild_id, i.user, 7))

    @discord.ui.button(label="Топ недели", emoji="🏆", style=discord.ButtonStyle.secondary, custom_id="up:top", row=3)
    async def top(self, i, b): await self.reply(i, await self.cog("Activity").top_embed(i.guild_id, 7))

    # ---------- Войс и помощь ----------
    # Старые кнопки «Общак» и «Внести в общак» убраны из панели участника.
    # Для казны используется отдельная панель /казна_панель с актуальным балансом.
    @discord.ui.button(label="Свой войс", emoji="🎙", style=discord.ButtonStyle.primary, custom_id="up:voice", row=4)
    async def voice(self, i, b): await i.response.send_modal(VoiceModal(self.bot))

    @discord.ui.button(label="Помощь", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="up:help", row=4)
    async def help(self, i, b): await self.reply(i, self.cog("Help").embed(i.guild))


class Panel(commands.Cog):
    def __init__(self, bot): self.bot = bot

    async def cog_load(self):
        self.bot.add_view(UserPanel(self.bot))
        self.bot.add_view(ContractAdminView(self.bot, timeout=None))
        self.bot.add_view(TempVoicePanelView(self.bot))

    @app_commands.command(name="контракты_панель", description="Отправить панель создания контрактов")
    @app_commands.rename(channel="канал")
    @app_commands.describe(channel="Канал для панели контрактов")
    @staff_only()
    async def contracts_panel(self, i, channel: discord.TextChannel):
        e = make("📄 Управление контрактами",
                 "Создание новых контрактов.\n\n"
                 "📝 **Создать контракт** — название, исполнители, сумма, персональные гонорары и срок.\n"
                 "🔒 **Закрыть контракт** — доступно только руководству и ролям с правом управления контрактами.\n\n"
                 "📝 Создавать контракты могут все участники. Все платежи, штрафы и предоплаты отправляются через отдельную панель казны со скриншотом и проверкой.", COLOR["main"])
        msg = await channel.send(embed=e, view=ContractAdminView(self.bot, timeout=None))
        await self.bot.log(i.guild, "contract", "Панель контрактов отправлена", channel.mention, i.user)
        await i.response.send_message(f"✅ Панель создания контрактов отправлена в {channel.mention}: {msg.jump_url}", ephemeral=True)

    @app_commands.command(name="временные_войсы_панель", description="Отправить отдельную панель создания временных войсов")
    @app_commands.rename(channel="канал")
    @app_commands.describe(channel="Канал, в который отправить панель")
    @staff_only()
    async def tempvoice_panel(self, i: discord.Interaction, channel: discord.TextChannel):
        e = make("🎙️ Создание временных войсов",
                 "Нажмите кнопку ниже, чтобы создать собственный временный голосовой канал.\n\n"
                 "• Можно задать название и лимит участников.\n"
                 "• Канал создаётся отдельно для вас; бот выдаёт владельцу права управления.\n"
                 "• Если канал пустой **60 секунд**, он автоматически удаляется.\n"
                 "• Если у вас уже есть временный войс, новый канал не создаётся.", COLOR["main"])
        if i.guild and i.guild.icon:
            e.set_thumbnail(url=i.guild.icon.url)
        msg = await channel.send(embed=e, view=TempVoicePanelView(self.bot))
        await self.bot.log(i.guild, "tempvoice", "Панель временных войсов отправлена", channel.mention, i.user)
        await i.response.send_message(f"✅ Панель временных войсов отправлена в {channel.mention}: {msg.jump_url}", ephemeral=True)

    @app_commands.command(name="панель", description="Отправить панель с кнопками для всех участников")
    @app_commands.rename(channel="канал")
    @app_commands.describe(channel="Канал для панели участников")
    async def panel(self, i, channel: discord.TextChannel):
        if not await self.bot.has_panel_access(i.user):
            return await i.response.send_message("🔒 Команда `/панель` доступна только администраторам, руководству и ролям, которым выдали доступ через `/настроить_доступ_панели`.", ephemeral=True)
        e = make(f"🧭 Панель {FAMILY}", "**Выберите нужное действие — нажимайте кнопки ниже.**\n\n"
                 "**💤 АФК:** встать, выйти или посмотреть список.\n"
                 "**📄 Контракты:** посмотреть открытые контракты, личные долги и предоплату.\n"
                 "**🚗 Автопарк:** взять свободную машину или сдать свою.\n"
                 "**👥 Семья:** открыть личную карточку, состав и активность.\n"
                 "**🎙 Свой войс:** создать личный временный голосовой канал.\n"
                 "**🛡 Администрация:** настройки набора, управление АФК/машинами, права ролей и финансовая очистка — только для разрешённых ролей и администраторов.\n\n"
                 "Ответы на нажатия видите только вы. Оплаты штрафов/контрактов и предоплата находятся в отдельной панели казны.", COLOR["main"])
        if i.guild.icon: e.set_thumbnail(url=i.guild.icon.url)
        await channel.send(embed=e, view=UserPanel(self.bot))
        await self.bot.log(i.guild, "server", "Панель участников отправлена", channel.mention, i.user)
        await i.response.send_message(f"Панель отправлена в {channel.mention}", ephemeral=True)

async def setup(bot): await bot.add_cog(Panel(bot))
