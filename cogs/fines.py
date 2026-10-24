import time
import logging
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from utils import make, money, COLOR, staff_only



class FineRemovalSelect(discord.ui.Select):
    def __init__(self, bot, target_member, rows):
        self.bot, self.target_member = bot, target_member
        options = []
        for row in rows[:24]:
            amount = max(0.0, float(row["amount"] or 0) - float(row["paid"] or 0))
            options.append(discord.SelectOption(
                label=f"{money(amount)} · {str(row['reason'])[:70]}"[:100],
                value=str(row["id"]), description=f"Штраф #{row['id']} · остаток {money(amount)}"[:100]))
        super().__init__(placeholder="Выберите конкретный штраф для снятия…", min_values=1, max_values=1, options=options)

    async def callback(self, i: discord.Interaction):
        if not (await self.bot.is_staff(i.user) or i.user.guild_permissions.manage_guild):
            return await i.response.send_message("🔒 Снимать штрафы могут только руководство и администраторы.", ephemeral=True)
        fine_id = int(self.values[0])
        row = await self.bot.db.fetchone("SELECT * FROM fines WHERE id=? AND guild_id=? AND user_id=? AND COALESCE(cancelled,0)=0", (fine_id, i.guild_id, self.target_member.id))
        if not row:
            return await i.response.send_message("Этот штраф уже снят или не найден.", ephemeral=True)
        await self.bot.db.execute("UPDATE fines SET cancelled=1 WHERE id=? AND guild_id=?", (fine_id, i.guild_id))
        try:
            await self.bot.log(i.guild, "fines", "Штраф снят администратором", f"Участник: {self.target_member.mention}\nСумма остатка: {money(max(0, float(row['amount'])-float(row['paid'] or 0)))}\nПричина: {row['reason']}\nШтраф ID: {fine_id}", i.user, COLOR["ok"])
        except Exception:
            logging.exception("Не удалось записать лог снятия штрафа #%s", fine_id)
        cog = self.bot.get_cog("Fines")
        if cog:
            await cog.refresh_panel(i.guild)
        await i.response.send_message(f"✅ Штраф снят у {self.target_member.mention}. Запись сохранена в истории, долг больше не учитывается.", ephemeral=True)


class FineRemovalView(discord.ui.View):
    def __init__(self, bot, target_member, rows):
        super().__init__(timeout=180)
        self.bot, self.target_member = bot, target_member
        self.add_item(FineRemovalSelect(bot, target_member, rows))

    async def interaction_check(self, i: discord.Interaction):
        if not (await self.bot.is_staff(i.user) or i.user.guild_permissions.manage_guild):
            await i.response.send_message("🔒 Снимать штрафы могут только руководство и администраторы.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Снять все штрафы участника", emoji="🧹", style=discord.ButtonStyle.danger)
    async def remove_all(self, i: discord.Interaction, button: discord.ui.Button):
        rows = await self.bot.db.fetchall("SELECT id, amount, paid FROM fines WHERE guild_id=? AND user_id=? AND COALESCE(cancelled,0)=0", (i.guild_id, self.target_member.id))
        if not rows:
            return await i.response.send_message("У участника нет активных штрафов.", ephemeral=True)
        count = len(rows)
        total = sum(max(0.0, float(r["amount"] or 0)-float(r["paid"] or 0)) for r in rows)
        await self.bot.db.execute("UPDATE fines SET cancelled=1 WHERE guild_id=? AND user_id=? AND COALESCE(cancelled,0)=0", (i.guild_id, self.target_member.id))
        try:
            await self.bot.log(i.guild, "fines", "Все штрафы сняты администратором", f"Участник: {self.target_member.mention}\nКоличество: {count}\nСнятый остаток: {money(total)}", i.user, COLOR["ok"])
        except Exception:
            logging.exception("Не удалось записать лог снятия всех штрафов")
        cog = self.bot.get_cog("Fines")
        if cog:
            await cog.refresh_panel(i.guild)
        await i.response.send_message(f"✅ Снято штрафов: **{count}**, отменённый остаток: **{money(total)}**. История сохранена.", ephemeral=True)


class FineDetailsModal(discord.ui.Modal, title="Выдать штраф"):
    amount_input = discord.ui.TextInput(label="Сумма штрафа", placeholder="Например: 5000", max_length=20)
    reason_input = discord.ui.TextInput(label="Причина штрафа", style=discord.TextStyle.paragraph, max_length=800)

    def __init__(self, bot, member):
        super().__init__()
        self.bot, self.member = bot, member

    async def on_submit(self, i: discord.Interaction):
        if not (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "fine_issue")):
            return await i.response.send_message("🔒 У вашей роли нет права выдавать штрафы.", ephemeral=True)
        try:
            amount = float(self.amount_input.value.strip().replace(" ", "").replace(",", "."))
            if not (0 < amount <= 1e12):
                raise ValueError
        except ValueError:
            return await i.response.send_message("Сумма должна быть числом больше нуля.", ephemeral=True)
        reason = self.reason_input.value.strip()
        if not reason:
            return await i.response.send_message("Укажите причину штрафа.", ephemeral=True)
        # БД + обновление панели + журнал могут занять больше 3 секунд.
        await i.response.defer(ephemeral=True, thinking=True)
        try:
            fine_id = await self.bot.db.execute(
                "INSERT INTO fines(guild_id, user_id, amount, reason, issuer_id, ts) VALUES(?,?,?,?,?,?)",
                (i.guild_id, self.member.id, amount, reason, i.user.id, int(time.time())))
        except Exception:
            logging.exception("Не удалось записать штраф в БД (guild=%s, user=%s)", i.guild_id, self.member.id)
            return await i.followup.send("❌ Не удалось сохранить штраф в базе данных. Ошибка записана в консоль бота.", ephemeral=True)

        # Автоматически списываем доступную предоплату штрафов с нового штрафа.
        prepaid_row = await self.bot.db.fetchone("SELECT balance FROM prepaid_fines WHERE guild_id=? AND user_id=?", (i.guild_id, self.member.id))
        prepaid_balance = max(0.0, float(prepaid_row["balance"] or 0)) if prepaid_row else 0.0
        prepaid_used = round(min(amount, prepaid_balance), 2)
        prepaid_left = round(max(0.0, prepaid_balance - prepaid_used), 2)
        if prepaid_used > 0:
            await self.bot.db.execute("UPDATE prepaid_fines SET balance=? WHERE guild_id=? AND user_id=?", (prepaid_left, i.guild_id, self.member.id))
            await self.bot.db.execute("UPDATE fines SET paid=COALESCE(paid,0)+? WHERE id=?", (prepaid_used, fine_id))
            await self.bot.db.execute("INSERT INTO fine_payments(fine_id,user_id,amount,note,ts) VALUES(?,?,?,?,?)", (fine_id, self.member.id, prepaid_used, "Автоматическое списание предоплаты штрафов", int(time.time())))
            try:
                issuer = i.guild.get_member(i.user.id)
                if issuer:
                    await issuer.send(f"✅ Штраф №{fine_id}, выданный {self.member.mention}, автоматически погашен предоплатой на **{money(prepaid_used)}**. Остаток предоплаты штрафов у участника: **{money(prepaid_left)}**. " + (f"Остаток самого штрафа: **{money(amount-prepaid_used)}**." if prepaid_used < amount else "Штраф полностью оплачен."))
            except (discord.Forbidden, discord.HTTPException):
                pass

        # Сохранение штрафа — основная операция. Ошибка обновления панели/логов
        # не должна создавать впечатление, что штраф не выдан.
        try:
            cog = self.bot.get_cog("Fines")
            if cog:
                await cog.refresh_panel(i.guild)
        except Exception:
            logging.exception("Штраф #%s сохранён, но панель не обновилась", fine_id)
        try:
            await self.bot.log(i.guild, "fines", "Выдан штраф", f"Штраф №{fine_id}\nКому: {self.member.mention}\nСумма: {money(amount)}\nПричина: {reason}\nВыдал: {i.user.mention}", i.user, COLOR["bad"])
        except Exception:
            logging.exception("Штраф #%s сохранён, но лог не записан", fine_id)
        try:
            paid_now = prepaid_used
            msg = f"⚖️ Вам выписан штраф на сервере **{i.guild.name}**.\n**Сумма:** {money(amount)}\n**Причина:** {reason}\n**Кто выписал:** {i.user.mention}\n"
            if paid_now > 0:
                msg += f"Автоматически погашено из предоплаты: **{money(paid_now)}**.\nОстаток предоплаты штрафов: **{money(prepaid_left)}**.\n"
            msg += (f"Осталось оплатить штраф: **{money(amount-paid_now)}**.\n" if paid_now < amount else "Штраф полностью погашен предоплатой.\n") + "Оплатить остаток можно через панель казны на сервере."
            await self.member.send(msg)
        except (discord.Forbidden, discord.HTTPException):
            logging.info("Не удалось отправить ЛС о штрафе участнику %s", self.member.id)
        await i.followup.send(f"✅ Штраф на **{money(amount)}** выдан участнику {self.member.mention}.", ephemeral=True)


class FineMemberPicker(discord.ui.View):
    def __init__(self, bot, issuer_id):
        super().__init__(timeout=120)
        self.bot, self.issuer_id = bot, issuer_id
        self.add_item(FineMemberSelect(bot, issuer_id))


class FineMemberSelect(discord.ui.UserSelect):
    def __init__(self, bot, issuer_id):
        super().__init__(placeholder="Выберите участника для штрафа…", min_values=1, max_values=1)
        self.bot, self.issuer_id = bot, issuer_id

    async def callback(self, i: discord.Interaction):
        if i.user.id != self.issuer_id:
            return await i.response.send_message("Эта форма принадлежит другому пользователю.", ephemeral=True)
        if not (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "fine_issue")):
            return await i.response.send_message("🔒 У вашей роли нет права выдавать штрафы.", ephemeral=True)
        selected = self.values[0]
        if not i.guild:
            return await i.response.send_message("Выбор участника доступен только на сервере.", ephemeral=True)
        member = selected if isinstance(selected, discord.Member) else i.guild.get_member(selected.id)
        if member is None:
            try:
                member = await i.guild.fetch_member(selected.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return await i.response.send_message("Не удалось найти участника на сервере. Проверьте, что у бота включён intent участников и право просмотра участников.", ephemeral=True)
        await i.response.send_modal(FineDetailsModal(self.bot, member))


class FinePanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Выдать штраф", emoji="⚖️", style=discord.ButtonStyle.danger, custom_id="fines:issue")
    async def issue(self, i: discord.Interaction, button: discord.ui.Button):
        if not (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "fine_issue")):
            return await i.response.send_message("🔒 У вашей роли нет права выдавать штрафы.", ephemeral=True)
        await i.response.send_message("**Шаг 1/2 — выберите участника** из списка ниже. ID и тег вводить не нужно.", view=FineMemberPicker(self.bot, i.user.id), ephemeral=True)

    @discord.ui.button(label="Уведомить должников", emoji="🔔", style=discord.ButtonStyle.secondary, custom_id="fines:notify_debtors")
    async def notify_debtors(self, i: discord.Interaction, button: discord.ui.Button):
        if not (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "fine_issue")):
            return await i.response.send_message("🔒 Только сотрудники с правом выдавать штрафы могут рассылать уведомления.", ephemeral=True)
        await i.response.defer(ephemeral=True, thinking=True)
        rows = await self.bot.db.fetchall("SELECT user_id, COUNT(*) AS n, SUM(amount-COALESCE(paid,0)) AS debt FROM fines WHERE guild_id=? AND COALESCE(cancelled,0)=0 AND amount>COALESCE(paid,0) GROUP BY user_id", (i.guild_id,))
        sent = failed = 0
        for row in rows:
            member = i.guild.get_member(int(row["user_id"]))
            if member is None:
                try: member = await i.guild.fetch_member(int(row["user_id"]))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    failed += 1; continue
            try:
                await member.send(f"🔔 Напоминание о неоплаченных штрафах на сервере **{i.guild.name}**.\n**Количество штрафов:** {row['n']}\n**Общий остаток:** {money(float(row['debt'] or 0))}\nДля оплаты откройте панель казны на сервере.")
                sent += 1
            except (discord.Forbidden, discord.HTTPException): failed += 1
        await i.followup.send(f"Рассылка завершена. Уведомлено: **{sent}**; не удалось доставить: **{failed}**; должников найдено: **{len(rows)}**.", ephemeral=True)


class Fines(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(FinePanelView(self.bot))
        self._panel_refresh_task = asyncio.create_task(self._refresh_panels_loop())

    def cog_unload(self):
        task = getattr(self, "_panel_refresh_task", None)
        if task and not task.done():
            task.cancel()

    async def _refresh_panels_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            for guild in list(self.bot.guilds):
                try:
                    await self.refresh_panel(guild)
                except Exception:
                    logging.exception("Не удалось автоматически обновить панель штрафов на сервере %s", guild.id)
            await asyncio.sleep(60)

    async def refresh_panel(self, guild):
        channel_id = await self.bot.db.get_setting(guild.id, "fines_panel_channel_id")
        message_id = await self.bot.db.get_setting(guild.id, "fines_panel_message_id")
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            message = await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        rows = await self.bot.db.fetchall(
            "SELECT * FROM fines WHERE guild_id=? ORDER BY id DESC LIMIT 10", (guild.id,))
        lines = []
        for row in rows:
            ts = int(row["ts"])
            amount = max(0.0, float(row["amount"] or 0))
            paid = max(0.0, float(row["paid"] or 0))
            cancelled = bool(int(row["cancelled"] or 0))
            if cancelled:
                status = "🚫 **СНЯТ**"
            elif paid + 0.005 >= amount:
                status = "✅ **ПОГАШЕН**"
            elif paid > 0.005:
                status = f"🟠 **ЧАСТИЧНО ПОГАШЕН** · осталось {money(amount - paid)}"
            else:
                status = f"🔴 **АКТИВЕН** · долг {money(amount - paid)}"
            lines.append(
                f"### ⚖️ Штраф №{row['id']} · {money(amount)} · {status}\n"
                f"**Кому:** <@{row['user_id']}>\n"
                f"**Причина:** {row['reason']}\n"
                f"**Выдал:** <@{row['issuer_id']}> · <t:{ts}:f>"
            )
        description = "Нажмите **«Выдать штраф»**, выберите участника из списка и заполните сумму с причиной. Выдавать штрафы могут руководство и роли с разрешением `Выдавать штрафы`.\n\n"
        description += "\n\n━━━━━━━━━━━━━━━━━━\n\n".join(lines) if lines else "Пока штрафов нет."
        embed = make("⚖️ Реестр штрафов", description[:4000], COLOR["bad"], footer="Последние 10 штрафов · автообновление раз в 60 секунд")
        try:
            await message.edit(content=None, embed=embed, view=FinePanelView(self.bot), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    @app_commands.command(name="штрафы_панель", description="Создать отдельную панель реестра штрафов")
    @app_commands.rename(channel="канал")
    @app_commands.describe(channel="Канал, в котором будет панель штрафов")
    @staff_only()
    async def fines_panel(self, i: discord.Interaction, channel: discord.TextChannel):
        embed = make("⚖️ Реестр штрафов", "Нажмите **«Выдать штраф»**, выберите участника из списка, затем укажите сумму и причину.\n\nПанель показывает последние 10 штрафов, включая упоминание оштрафованного и того, кто выдал штраф.", COLOR["bad"])
        msg = await channel.send(embed=embed, view=FinePanelView(self.bot))
        await self.bot.db.set_setting(i.guild_id, "fines_panel_channel_id", channel.id)
        await self.bot.db.set_setting(i.guild_id, "fines_panel_message_id", msg.id)
        await i.response.send_message(f"✅ Панель штрафов создана в {channel.mention}: {msg.jump_url}", ephemeral=True)

    @app_commands.command(name="штрафы_снять", description="Снять один или все штрафы выбранного участника")
    @app_commands.rename(member="участник")
    @app_commands.describe(member="У кого снять один или все штрафы")
    async def remove_fines(self, i: discord.Interaction, member: discord.Member):
        if not (await self.bot.is_staff(i.user) or i.user.guild_permissions.manage_guild):
            return await i.response.send_message("🔒 Снимать штрафы могут только руководство и администраторы.", ephemeral=True)
        rows = await self.bot.db.fetchall("SELECT * FROM fines WHERE guild_id=? AND user_id=? AND COALESCE(cancelled,0)=0 ORDER BY ts DESC", (i.guild_id, member.id))
        if not rows:
            return await i.response.send_message(f"У {member.mention} нет активных штрафов.", ephemeral=True)
        active = sum(max(0.0, float(r["amount"] or 0)-float(r["paid"] or 0)) for r in rows)
        view = FineRemovalView(self.bot, member, rows)
        text = f"**Управление штрафами: {member.mention}**\nАктивных штрафов: **{len(rows)}** · остаток: **{money(active)}**\nВыберите один штраф в списке или нажмите «Снять все штрафы участника». Снятые штрафы остаются в истории, но больше не входят в долг."
        await i.response.send_message(text, view=view, ephemeral=True)

    @app_commands.command(name="штраф_выдать", description="Выдать штраф участнику")
    @app_commands.rename(member="участник", amount="сумма", reason="причина")
    @app_commands.describe(member="Кому выдать штраф", amount="Сумма штрафа", reason="Причина штрафа")
    async def issue_fine(self, i: discord.Interaction, member: discord.Member, amount: app_commands.Range[float, 0.01, 1e12], reason: str):
        if not (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "fine_issue")):
            return await i.response.send_message("🔒 У вашей роли нет права выдавать штрафы.", ephemeral=True)
        await i.response.defer(ephemeral=True, thinking=True)
        try:
            fine_id = await self.bot.db.execute(
                "INSERT INTO fines(guild_id, user_id, amount, reason, issuer_id, ts) VALUES(?,?,?,?,?,?)",
                (i.guild_id, member.id, float(amount), reason.strip(), i.user.id, int(time.time())))
        except Exception:
            logging.exception("Не удалось записать штраф через /штраф_выдать (guild=%s, user=%s)", i.guild_id, member.id)
            return await i.followup.send("❌ Не удалось сохранить штраф в базе данных. Ошибка записана в консоль бота.", ephemeral=True)
        try:
            await self.refresh_panel(i.guild)
        except Exception:
            logging.exception("Штраф #%s сохранён, но панель не обновилась", fine_id)
        try:
            await self.bot.log(i.guild, "fines", "Выдан штраф", f"Штраф №{fine_id}\nКому: {member.mention}\nСумма: {money(amount)}\nПричина: {reason}\nВыдал: {i.user.mention}", i.user, COLOR["bad"])
        except Exception:
            logging.exception("Штраф #%s сохранён, но лог не записан", fine_id)
        try:
            await member.send(f"⚖️ Вам выписан штраф на сервере **{i.guild.name}**.\n**Сумма:** {money(amount)}\n**Причина:** {reason.strip()}\n**Кто выписал:** {i.user.mention}\nОплатить штраф можно через панель казны на сервере.")
        except (discord.Forbidden, discord.HTTPException):
            logging.info("Не удалось отправить ЛС о штрафе участнику %s", member.id)
        await i.followup.send(f"✅ Штраф на **{money(amount)}** выдан участнику {member.mention}.", ephemeral=True)

    @app_commands.command(name="лог_штрафов", description="Показать последние действия со штрафами")
    @app_commands.describe(количество="Сколько записей показать (1–20)")
    @app_commands.default_permissions()
    async def fine_audit(self, i: discord.Interaction, количество: app_commands.Range[int, 1, 20] = 10):
        if not (await self.bot.is_staff(i.user) or i.user.guild_permissions.manage_guild):
            return await i.response.send_message("🔒 Просматривать журнал штрафов могут только сотрудники.", ephemeral=True)
        rows = await self.bot.db.fetchall("SELECT title,description,ts FROM logs WHERE guild_id=? AND category='fines' ORDER BY id DESC LIMIT ?", (i.guild_id, количество))
        if not rows:
            return await i.response.send_message("Записей в журнале штрафов пока нет.", ephemeral=True, delete_after=18)
        lines = [f"**{r['title']}** · <t:{int(r['ts'])}:f>\n{r['description']}" for r in rows]
        await i.response.send_message(embed=make("⚖️ Журнал штрафов", "\n\n".join(lines)[:4000], COLOR["bad"]), ephemeral=True, delete_after=25)


async def setup(bot):
    await bot.add_cog(Fines(bot))
