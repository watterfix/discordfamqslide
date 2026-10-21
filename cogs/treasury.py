"""Казна: платежи штрафов/контрактов и предоплаты со скриншотом и модерацией."""
import time
import discord
from discord import app_commands
from discord.ext import commands, tasks
from utils import make, money, COLOR, staff_only

KINDS = {
    "fine": ("Оплата штрафа", "fine"),
    "prepay_fine": ("Предоплата штрафа", "fine"),
    "contract": ("Оплата контракта", "contract"),
    "prepay": ("Предоплата контракта", "contract"),
}


def parse_amount(value):
    try:
        amount = float(value.strip().replace(" ", "").replace(",", "."))
        return amount if 0 < amount <= 1e12 else None
    except (ValueError, AttributeError):
        return None


async def personal_obligations(bot, guild_id, user_id, kind):
    """Return outstanding items for the current member; IDs are kept internal only."""
    items = []
    if kind in ("fine", "prepay_fine"):
        rows = await bot.db.fetchall(
            "SELECT * FROM fines WHERE guild_id=? AND user_id=? AND COALESCE(cancelled,0)=0 ORDER BY ts DESC",
            (guild_id, user_id))
        for r in rows:
            debt = max(0.0, float(r["amount"] or 0) - float(r["paid"] or 0))
            if debt > 0.005:
                label = f"Штраф · {str(r['reason'])[:65]}"
                desc = f"Остаток {money(debt)}"
                items.append((label[:100], desc[:100], str(r["id"]), debt))
    else:
        from cogs.contracts import execs, fee_map
        rows = await bot.db.fetchall("SELECT * FROM contracts WHERE guild_id=? AND status!='closed' ORDER BY created DESC", (guild_id,))
        for c in rows:
            if user_id not in execs(c):
                continue
            fee = float(fee_map(c).get(user_id, 0))
            total = round(fee * max(0.0, float(c["percent"] or 0)) / 100, 2)
            paid_row = await bot.db.fetchone(
                "SELECT COALESCE(SUM(amount),0) AS total FROM payments WHERE contract_id=? AND user_id=?",
                (c["id"], user_id))
            debt = max(0.0, round(total - float(paid_row["total"] or 0), 2))
            if debt > 0.005:
                label = f"Контракт · {str(c['title'] or 'Без названия')[:65]}"
                desc = f"Личный долг {money(debt)}"
                items.append((label[:100], desc[:100], str(c["id"]), debt))
    return items


class TreasuryPaymentModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="Сумма платежа", placeholder="Например: 5000", max_length=20)
    note = discord.ui.TextInput(label="Комментарий (необязательно)", required=False, max_length=150)

    def __init__(self, bot, kind, target_id, max_due):
        self.bot, self.kind, self.target_id, self.max_due = bot, kind, int(target_id), float(max_due)
        super().__init__(title=KINDS[kind][0])

    async def on_submit(self, i: discord.Interaction):
        amount = parse_amount(self.amount.value)
        if amount is None:
            return await i.response.send_message("Укажите корректную сумму больше нуля.", ephemeral=True)
        if amount > self.max_due + 0.005:
            return await i.response.send_message(f"Сумма больше вашего личного остатка: {money(self.max_due)}.", ephemeral=True)
        target_id = self.target_id
        if self.kind in ("fine", "prepay_fine"):
            row = await self.bot.db.fetchone("SELECT * FROM fines WHERE id=? AND guild_id=? AND user_id=?", (target_id, i.guild_id, i.user.id))
            if not row or int(row["cancelled"] or 0):
                return await i.response.send_message("Этот штраф не найден в вашем личном балансе.", ephemeral=True)
            due = max(0.0, float(row["amount"]) - float(row["paid"] or 0))
            target_label = f"Штраф · {row['reason']}"
        else:
            row = await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=? AND guild_id=? AND status!='closed'", (target_id, i.guild_id))
            if not row:
                return await i.response.send_message("Этот контракт не найден в вашем личном балансе.", ephemeral=True)
            from cogs.contracts import execs, fee_map
            if i.user.id not in execs(row):
                return await i.response.send_message("Этот долг не относится к вашему личному балансу.", ephemeral=True)
            fee = float(fee_map(row).get(i.user.id, 0))
            personal_due = round(fee * max(0.0, float(row["percent"] or 0)) / 100, 2)
            paid_row = await self.bot.db.fetchone("SELECT COALESCE(SUM(amount),0) AS total FROM payments WHERE contract_id=? AND user_id=?", (target_id, i.user.id))
            due = max(0.0, round(personal_due - float(paid_row["total"] or 0), 2))
            target_label = f"Контракт · {row['title']}"
        if amount > due + 0.005:
            return await i.response.send_message(f"Остаток вашего долга изменился: {money(due)}.", ephemeral=True)
        await i.response.defer(ephemeral=True, thinking=True)
        try:
            setting_key = "payment_fines_channel" if self.kind in ("fine", "prepay_fine") else "payment_contracts_channel"
            configured_id = await self.bot.db.get_setting(i.guild_id, setting_key)
            channel = i.guild.get_channel(int(configured_id)) if configured_id else (i.guild.get_channel(i.channel_id) if i.guild else None)
            if channel is None or not hasattr(channel, "create_thread"):
                raise RuntimeError("Канал не настроен или не поддерживает создание веток")
            thread = await channel.create_thread(name=f"{KINDS[self.kind][0]} · {i.user.display_name}"[:95], type=discord.ChannelType.public_thread, auto_archive_duration=1440, reason="Проверка платежа личного баланса")
        except (discord.HTTPException, AttributeError, RuntimeError):
            return await i.followup.send("Не удалось создать тему для скриншота. Проверьте права бота на создание тем.", ephemeral=True)
        note = self.note.value.strip()
        await self.bot.db.execute(
            "INSERT INTO treasury_submissions(guild_id,kind,target_id,user_id,amount,note,thread_id,status,created) VALUES(?,?,?,?,?,?,?,'waiting_screenshot',?)",
            (i.guild_id, self.kind, target_id, i.user.id, amount, note, thread.id, int(time.time())))
        # Ping configured role(s) in the payment thread; fine and contract roles are configured separately.
        setting_key = "treasury_fine_ping_roles" if self.kind in ("fine", "prepay_fine") else "treasury_contract_ping_roles"
        raw_roles = await self.bot.db.get_setting(i.guild_id, setting_key)
        role_ids = []
        if raw_roles:
            for value in str(raw_roles).split(","):
                try:
                    role_id = int(value.strip())
                    if i.guild.get_role(role_id):
                        role_ids.append(role_id)
                except (TypeError, ValueError):
                    continue
        role_mentions = " ".join(f"<@&{rid}>" for rid in role_ids)
        content = (f"{role_mentions}\n" if role_mentions else "") + f"📸 {i.user.mention}, прикрепите **скриншот платежа** одним сообщением.\nЛичный баланс: **{target_label}**\nСумма: **{money(amount)}**\nОстаток до оплаты: **{money(due)}**\nКомментарий: {note or '—'}\nПлатёж будет учтён только после одобрения."
        await thread.send(content, allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False))
        await i.followup.send(f"Создана тема для оплаты вашего личного долга: {thread.mention}. Платёж будет учтён после одобрения.", ephemeral=True)


class PersonalObligationSelect(discord.ui.Select):
    def __init__(self, bot, kind, user_id, items):
        self.bot, self.kind, self.user_id, self.items = bot, kind, user_id, {x[2]: x[3] for x in items}
        options = [discord.SelectOption(label=x[0], description=x[1], value=x[2]) for x in items[:25]]
        super().__init__(placeholder="Выберите долг из вашего личного баланса…", min_values=1, max_values=1, options=options)

    async def callback(self, i: discord.Interaction):
        if i.user.id != self.user_id:
            return await i.response.send_message("Это меню личного баланса другого участника.", ephemeral=True)
        await i.response.send_modal(TreasuryPaymentModal(self.bot, self.kind, self.values[0], self.items[self.values[0]]))


class PersonalObligationView(discord.ui.View):
    def __init__(self, bot, kind, user_id, items):
        super().__init__(timeout=180)
        self.add_item(PersonalObligationSelect(bot, kind, user_id, items))


class PrepaymentTypeSelect(discord.ui.Select):
    def __init__(self, bot, user_id):
        self.bot, self.user_id = bot, user_id
        super().__init__(
            placeholder="Выберите, что хотите внести заранее…",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="Предоплата штрафа", value="prepay_fine", emoji="💳", description="Частично погасить штраф"),
                discord.SelectOption(label="Предоплата контракта", value="prepay", emoji="⏩", description="Частично погасить личный долг по контракту"),
            ])

    async def callback(self, i: discord.Interaction):
        if i.user.id != self.user_id:
            return await i.response.send_message("Это меню предоплаты другого участника.", ephemeral=True)
        kind = self.values[0]
        items = await personal_obligations(self.bot, i.guild_id, i.user.id, kind)
        if not items:
            label = "штрафов" if kind == "prepay_fine" else "долгов по контрактам"
            return await i.response.send_message(f"В вашем личном балансе нет неоплаченных {label} для предоплаты.", ephemeral=True)
        await i.response.send_message(
            "**Предоплата · ваш личный баланс**\nВыберите обязательство. Вводить номера штрафов и контрактов не нужно.",
            view=PersonalObligationView(self.bot, kind, i.user.id, items), ephemeral=True)


class PrepaymentTypeView(discord.ui.View):
    def __init__(self, bot, user_id):
        super().__init__(timeout=180)
        self.add_item(PrepaymentTypeSelect(bot, user_id))


class TreasuryPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def open_personal(self, i, kind):
        items = await personal_obligations(self.bot, i.guild_id, i.user.id, kind)
        if not items:
            label = "штрафов" if kind == "fine" else "долгов по контрактам"
            return await i.response.send_message(f"В вашем личном балансе нет неоплаченных {label}.", ephemeral=True)
        await i.response.send_message("**Ваш личный баланс**\nВыберите долг для оплаты. Номера штрафов и контрактов вводить не нужно.", view=PersonalObligationView(self.bot, kind, i.user.id, items), ephemeral=True)

    @discord.ui.button(label="Оплатить штраф", emoji="⚖️", style=discord.ButtonStyle.danger, custom_id="treasury:pay_fine", row=0)
    async def pay_fine(self, i, button):
        await self.open_personal(i, "fine")

    @discord.ui.button(label="Оплатить контракт", emoji="📄", style=discord.ButtonStyle.primary, custom_id="treasury:pay_contract", row=0)
    async def pay_contract(self, i, button):
        await self.open_personal(i, "contract")

    @discord.ui.button(label="Предоплата", emoji="💳", style=discord.ButtonStyle.success, custom_id="treasury:prepayment", row=0)
    async def prepayment(self, i, button):
        await i.response.send_message("**Предоплата**\nВыберите тип обязательства:", view=PrepaymentTypeView(self.bot, i.user.id), ephemeral=True)


class CommonFundModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="Сумма", placeholder="Например: 5000", max_length=20)
    note = discord.ui.TextInput(label="Назначение / комментарий", placeholder="За что внесение или снятие", max_length=250)

    def __init__(self, bot, direction):
        self.bot, self.direction = bot, direction
        super().__init__(title="Внести в общак" if direction == "in" else "Снять из общака")

    async def on_submit(self, i: discord.Interaction):
        if self.direction == "out":
            allowed = await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "treasury_withdraw")
        else:
            allowed = await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "treasury_deposit")
        if not allowed:
            return await i.response.send_message("🔒 У вашей роли нет права выполнять эту операцию с общаком.", ephemeral=True)
        amount = parse_amount(self.amount.value)
        note = self.note.value.strip()
        if amount is None:
            return await i.response.send_message("Укажите корректную сумму больше нуля.", ephemeral=True)
        if not note:
            return await i.response.send_message("Укажите назначение операции.", ephemeral=True)
        if self.direction == "out":
            balance = await self.bot.db.fetchone("SELECT COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE -amount END),0) AS balance FROM common_fund_transactions WHERE guild_id=?", (i.guild_id,))
            current = float(balance["balance"] or 0)
            if amount > current + 0.005:
                return await i.response.send_message(f"Недостаточно денег в общаке. Текущий баланс: **{money(current)}**.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO common_fund_transactions(guild_id,user_id,direction,amount,note,ts) VALUES(?,?,?,?,?,?)",
            (i.guild_id, i.user.id, self.direction, amount, note, int(time.time())))
        await self.bot.log(i.guild, "treasury", "Внесение в общак" if self.direction == "in" else "Снятие из общака",
                           f"Сумма: {money(amount)}\nКомментарий: {note}\nВыполнил: {i.user.mention}", i.user, COLOR["ok"] if self.direction == "in" else COLOR["bad"])
        cog = self.bot.get_cog("Treasury")
        await cog.refresh_common_fund_panel(i.guild)
        bal = await self.bot.db.fetchone("SELECT COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE -amount END),0) AS balance FROM common_fund_transactions WHERE guild_id=?", (i.guild_id,))
        await i.followup.send(f"✅ {'Внесено' if self.direction == 'in' else 'Снято'} **{money(amount)}**. Новый баланс общака: **{money(bal['balance'])}**.", ephemeral=True)


class CommonFundPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Внести деньги", emoji="➕", style=discord.ButtonStyle.success, custom_id="common_fund:deposit")
    async def deposit(self, i: discord.Interaction, button: discord.ui.Button):
        if not (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "treasury_deposit")):
            return await i.response.send_message("🔒 Вносить деньги в общак могут руководство и назначенная роль казны.", ephemeral=True)
        await i.response.send_modal(CommonFundModal(self.bot, "in"))

    @discord.ui.button(label="Снять деньги", emoji="➖", style=discord.ButtonStyle.danger, custom_id="common_fund:withdraw")
    async def withdraw(self, i: discord.Interaction, button: discord.ui.Button):
        if not (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "treasury_withdraw")):
            return await i.response.send_message("🔒 Снимать деньги из общака может только руководство или роль с правом «Выдавать деньги из общака».", ephemeral=True)
        await i.response.send_modal(CommonFundModal(self.bot, "out"))


class TreasuryReviewView(discord.ui.View):
    def __init__(self, bot, submission_id):
        super().__init__(timeout=None)
        self.bot, self.submission_id = bot, submission_id
        yes = discord.ui.Button(label="Одобрить · внесено", emoji="✅", style=discord.ButtonStyle.success, custom_id=f"treasury_review:approve:{submission_id}")
        no = discord.ui.Button(label="Отклонить · не внесено", emoji="❌", style=discord.ButtonStyle.danger, custom_id=f"treasury_review:reject:{submission_id}")
        yes.callback = self.approve
        no.callback = self.reject
        self.add_item(yes); self.add_item(no)

    async def allowed(self, i):
        row = await self.bot.db.fetchone("SELECT kind FROM treasury_submissions WHERE id=? AND guild_id=?", (self.submission_id, i.guild_id))
        if row and row["kind"] in ("fine", "prepay_fine"):
            permission = "treasury_fine_review"
            legacy = "treasury_review"
            label = "оплату штрафов"
        else:
            permission = "treasury_contract_review"
            legacy = "treasury_review"
            label = "оплату контрактов"
        if (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, permission)
                or await self.bot.has_role_permission(i.user, legacy)):
            return True
        await i.response.send_message(f"🔒 Одобрять и отклонять {label} могут только руководство или назначенные роли.", ephemeral=True)
        return False

    async def approve(self, i):
        if not await self.allowed(i): return
        r = await self.bot.db.fetchone("SELECT * FROM treasury_submissions WHERE id=? AND guild_id=?", (self.submission_id, i.guild_id))
        if not r or r['status'] != 'pending_review':
            return await i.response.send_message("Заявка уже обработана или скриншот не приложен.", ephemeral=True)
        if r['kind'] in ('fine', 'prepay_fine'):
            fine = await self.bot.db.fetchone("SELECT * FROM fines WHERE id=? AND guild_id=?", (r['target_id'], i.guild_id))
            if not fine: return await i.response.send_message("Штраф не найден.", ephemeral=True)
            due = max(0.0, float(fine['amount']) - float(fine['paid'] or 0))
            if r['amount'] > due + 0.005: return await i.response.send_message(f"Остаток штрафа изменился: {money(due)}. Заявка не проведена.", ephemeral=True)
            await self.bot.db.execute("UPDATE fines SET paid=COALESCE(paid,0)+? WHERE id=?", (r['amount'], fine['id']))
            await self.bot.db.execute("INSERT INTO fine_payments(fine_id,user_id,amount,note,ts) VALUES(?,?,?,?,?)", (fine['id'], r['user_id'], r['amount'], r['note'], int(time.time())))
            note = f"{'Предоплата' if r['kind'] == 'prepay_fine' else 'Оплата'} штрафа №{fine['id']}"
            target_desc = f"Штраф №{fine['id']} · <@{fine['user_id']}>"
            await self.bot.log(i.guild, "fines", "Предоплата штрафа одобрена" if r['kind'] == 'prepay_fine' else "Оплата штрафа одобрена", f"{target_desc}\nСумма: {money(r['amount'])}\nПлательщик: <@{r['user_id']}>\nПроверил: {i.user.mention}", i.user, COLOR['ok'])
            try: await self.bot.get_cog("Fines").refresh_panel(i.guild)
            except Exception: pass
        else:
            c = await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=? AND guild_id=?", (r['target_id'], i.guild_id))
            if not c: return await i.response.send_message("Контракт не найден.", ephemeral=True)
            cog = self.bot.get_cog("Contracts")
            due = await cog._get_debt_for_treasury(c)
            if c['status'] == 'closed' or r['amount'] > due + 0.005:
                return await i.response.send_message(f"Контракт закрыт или остаток изменился ({money(due)}). Заявка не проведена.", ephemeral=True)
            await cog._pay_row(c['id'], r['user_id'], r['amount'], f"{'Предоплата' if r['kind']=='prepay' else 'Оплата'} через казну: {r['note']}")
            if 'funded' in c.keys() and not c['funded']:
                await self.bot.db.execute("UPDATE contracts SET funded=1 WHERE id=?", (c['id'],))
            await cog.refresh(i.guild, c['id'])
            note = f"{'Предоплата' if r['kind']=='prepay' else 'Оплата'} контракта №{c['id']}"
            target_desc = f"Контракт №{c['id']} · {c['title']}"
            await self.bot.log(i.guild, "treasury", "Платёж контракта одобрен", f"{target_desc}\nСумма: {money(r['amount'])}\nПлательщик: <@{r['user_id']}>\nПроверил: {i.user.mention}", i.user, COLOR['ok'])
        # Одобренный платёж по штрафу/контракту — это поступление в верхний общак,
        # а не ручная запись в старый реестр «Общак семьи» (таблица treasury).
        await self.bot.db.execute(
            "INSERT INTO common_fund_transactions(guild_id,user_id,direction,amount,note,ts) VALUES(?,?,'in',?,?,?)",
            (i.guild_id, r['user_id'], r['amount'], note, int(time.time())))
        now = int(time.time())
        target_type = "штраф" if r['kind'] in ('fine', 'prepay_fine') else "контракт"
        await self.bot.db.execute(
            "INSERT INTO treasury_audit(guild_id,submission_id,payer_id,target_type,target_id,target_description,payment_kind,amount,note,reviewer_id,result,thread_id,created_at,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'approved',?,?,?)",
            (i.guild_id, self.submission_id, r['user_id'], target_type, r['target_id'], target_desc, KINDS[r['kind']][0], r['amount'], r['note'], i.user.id, r['thread_id'], r['created'], now))
        await self.bot.db.execute("UPDATE treasury_submissions SET status='approved', reviewer_id=?, resolved_at=? WHERE id=?", (i.user.id, now, self.submission_id))
        treasury_cog = self.bot.get_cog("Treasury")
        await treasury_cog.refresh_panel(i.guild)
        await treasury_cog.refresh_common_fund_panel(i.guild)
        await i.response.edit_message(content=f"✅ **ОДОБРЕНО · ВНЕСЕНО**\n{target_desc}\nСумма: **{money(r['amount'])}**\nПроверил: {i.user.mention}", view=None)
        thread = i.guild.get_thread(r['thread_id']) if r['thread_id'] else None
        if thread:
            try:
                await thread.send(f"✅ Платёж одобрен {i.user.mention}. Он учтён в системе.")
                # Mark the payment thread's starter message with the review result.
                try:
                    starter = await thread.fetch_message(thread.id)
                    await starter.add_reaction('✅')
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    pass
            except discord.HTTPException: pass

    async def reject(self, i):
        if not await self.allowed(i): return
        r = await self.bot.db.fetchone("SELECT * FROM treasury_submissions WHERE id=? AND guild_id=?", (self.submission_id, i.guild_id))
        if not r or r['status'] != 'pending_review':
            return await i.response.send_message("Заявка уже обработана или скриншот не приложен.", ephemeral=True)
        now = int(time.time())
        target_type = "штраф" if r['kind'] in ('fine', 'prepay_fine') else "контракт"
        if target_type == "штраф":
            target = await self.bot.db.fetchone("SELECT reason,user_id FROM fines WHERE id=?", (r['target_id'],))
            target_desc = f"Штраф: {target['reason']} · участник <@{target['user_id']}>" if target else f"Штраф #{r['target_id']}"
        else:
            target = await self.bot.db.fetchone("SELECT title FROM contracts WHERE id=?", (r['target_id'],))
            target_desc = f"Контракт: {target['title']}" if target else f"Контракт #{r['target_id']}"
        await self.bot.db.execute(
            "INSERT INTO treasury_audit(guild_id,submission_id,payer_id,target_type,target_id,target_description,payment_kind,amount,note,reviewer_id,result,thread_id,created_at,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'rejected',?,?,?)",
            (i.guild_id, self.submission_id, r['user_id'], target_type, r['target_id'], target_desc, KINDS[r['kind']][0], r['amount'], r['note'], i.user.id, r['thread_id'], r['created'], now))
        await self.bot.db.execute("UPDATE treasury_submissions SET status='rejected', reviewer_id=?, resolved_at=? WHERE id=?", (i.user.id, now, self.submission_id))
        if target_type == "штраф":
            try: await self.bot.log(i.guild, "fines", "Оплата штрафа отклонена", f"{target_desc}\nСумма: {money(r['amount'])}\nПлательщик: <@{r['user_id']}>\nПроверил: {i.user.mention}\nКомментарий: {r['note'] or '—'}", i.user, COLOR['bad'])
            except Exception: pass
        await i.response.edit_message(content=f"❌ **ОТКЛОНЕНО · НЕ ВНЕСЕНО**\nТип: {KINDS[r['kind']][0]} №{r['target_id']}\nСумма: {money(r['amount'])}\nПроверил: {i.user.mention}. Можно создать новую заявку с корректным скриншотом.", view=None)
        thread = i.guild.get_thread(r['thread_id']) if r['thread_id'] else None
        if thread:
            try:
                await thread.send(f"❌ Платёж отклонён {i.user.mention}. Для повторной попытки отправьте новую заявку через панель казны.")
                # Mark the payment thread's starter message with the review result.
                try:
                    starter = await thread.fetch_message(thread.id)
                    await starter.add_reaction('❌')
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    pass
            except discord.HTTPException: pass


class Treasury(commands.Cog):
    def __init__(self, bot): self.bot = bot

    def cog_unload(self):
        self.cleanup_payment_threads.cancel()

    @tasks.loop(hours=1)
    async def cleanup_payment_threads(self):
        now = int(time.time())
        for guild in self.bot.guilds:
            raw = await self.bot.db.get_setting(guild.id, "payment_thread_retention_days")
            try: days = max(1, min(3650, int(raw or 30)))
            except (TypeError, ValueError): days = 30
            rows = await self.bot.db.fetchall("SELECT id,thread_id FROM treasury_submissions WHERE status IN ('approved','rejected') AND resolved_at IS NOT NULL AND resolved_at<=? AND thread_id IS NOT NULL", (now-days*86400,))
            for row in rows:
                try:
                    thread = guild.get_thread(int(row['thread_id'])) or await self.bot.fetch_channel(int(row['thread_id']))
                    await thread.delete(reason=f"Платёжная ветка старше {days} дней; аудит сохранён")
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                await self.bot.db.execute("UPDATE treasury_submissions SET thread_id=NULL WHERE id=?", (row['id'],))

    @cleanup_payment_threads.before_loop
    async def before_cleanup_payment_threads(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="срок_веток_оплат", description="Настроить срок хранения закрытых веток оплат")
    @app_commands.describe(дни="Через сколько дней удалять ветки после решения (1–3650)")
    @staff_only()
    async def set_payment_thread_retention(self, i: discord.Interaction, дни: app_commands.Range[int, 1, 3650]):
        await self.bot.db.set_setting(i.guild_id, "payment_thread_retention_days", дни)
        await i.response.send_message(f"✅ Закрытые ветки оплат будут удаляться через {дни} дн. Журнал оплат сохраняется.", ephemeral=True, delete_after=18)

    @app_commands.command(name="лог_оплат", description="Показать последние решения по оплатам")
    @app_commands.describe(количество="Количество записей (1–20)")
    @staff_only()
    async def payment_audit(self, i: discord.Interaction, количество: app_commands.Range[int, 1, 20] = 10):
        rows = await self.bot.db.fetchall("SELECT * FROM treasury_audit WHERE guild_id=? ORDER BY id DESC LIMIT ?", (i.guild_id, количество))
        if not rows:
            return await i.response.send_message("Записей оплат пока нет; журнал начнёт заполняться после новых решений.", ephemeral=True, delete_after=18)
        lines=[]
        for r in rows:
            status = "✅ Одобрена" if r['result']=='approved' else "❌ Отклонена"
            lines.append(f"**{status} · {r['payment_kind']} — {money(r['amount'])}**\nПлательщик: <@{r['payer_id']}> · Проверил: <@{r['reviewer_id']}>\nОбъект: {r['target_description']}\nКомментарий: {r['note'] or '—'} · <t:{int(r['resolved_at'] or r['created_at'])}:f>")
        await i.response.send_message(embed=make("📚 Журнал оплат", "\n\n".join(lines)[:4000], COLOR['main']), ephemeral=True, delete_after=25)

    async def cog_load(self):
        # Defensive schema guard: create the table even if an older database.py
        # or an existing bot.db was retained during an update.
        await self.bot.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS treasury_submissions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                target_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                note TEXT,
                thread_id INTEGER,
                screenshot_url TEXT,
                screenshot_message_id INTEGER,
                review_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'waiting_screenshot',
                reviewer_id INTEGER,
                created INTEGER NOT NULL
            )
        """)
        await self.bot.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS treasury_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                submission_id INTEGER, payer_id INTEGER NOT NULL, target_type TEXT NOT NULL,
                target_id INTEGER NOT NULL, target_description TEXT NOT NULL,
                payment_kind TEXT NOT NULL, amount REAL NOT NULL, note TEXT,
                reviewer_id INTEGER, result TEXT NOT NULL, thread_id INTEGER,
                created_at INTEGER NOT NULL, resolved_at INTEGER)
        """)
        cols = {row[1] for row in await (await self.bot.db.conn.execute("PRAGMA table_info(treasury_submissions)")).fetchall()}
        if "resolved_at" not in cols:
            await self.bot.db.conn.execute("ALTER TABLE treasury_submissions ADD COLUMN resolved_at INTEGER")
        await self.bot.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS common_fund_transactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('in','out')),
                amount REAL NOT NULL,
                note TEXT NOT NULL,
                ts INTEGER NOT NULL
            )
        """)
        await self.bot.db.conn.commit()
        self.bot.add_view(TreasuryPanelView(self.bot))
        self.bot.add_view(CommonFundPanelView(self.bot))
        rows = await self.bot.db.fetchall("SELECT id,review_message_id FROM treasury_submissions WHERE status='pending_review' AND review_message_id IS NOT NULL")
        for row in rows:
            self.bot.add_view(TreasuryReviewView(self.bot, row['id']), message_id=row['review_message_id'])
        if not self.cleanup_payment_threads.is_running():
            self.cleanup_payment_threads.start()

    async def _update_review(self, guild, r, message, image):
        if r['kind'] == 'fine':
            target = await self.bot.db.fetchone("SELECT * FROM fines WHERE id=?", (r['target_id'],))
            target_text = f"Штраф №{r['target_id']} · {('участник <@'+str(target['user_id'])+'>') if target else '—'}"
        else:
            target = await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=?", (r['target_id'],))
            target_text = f"Контракт №{r['target_id']} · {target['title'] if target else '—'}"
        title, _ = KINDS[r['kind']]
        e = make(f"📸 Проверка казны · {title}", f"**Объект:** {target_text}\n**Сумма:** {money(r['amount'])}\n**Отправил:** <@{r['user_id']}>\n**Комментарий:** {r['note'] or '—'}\n**Скриншот:** [Открыть изображение]({image.url})\n\nНазначенная роль должна одобрить или отклонить платёж.", COLOR['warn'])
        e.set_image(url=image.url)
        msg = await message.channel.send(embed=e, view=TreasuryReviewView(self.bot, r['id']))
        await self.bot.db.execute("UPDATE treasury_submissions SET screenshot_url=?, screenshot_message_id=?, review_message_id=?, status='pending_review' WHERE id=?", (image.url, message.id, msg.id, r['id']))
        self.bot.add_view(TreasuryReviewView(self.bot, r['id']), message_id=msg.id)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild or not message.attachments: return
        r = await self.bot.db.fetchone("SELECT * FROM treasury_submissions WHERE guild_id=? AND thread_id=? AND user_id=? AND status='waiting_screenshot' ORDER BY id DESC LIMIT 1", (message.guild.id, message.channel.id, message.author.id))
        if not r: return
        image = next((a for a in message.attachments if (a.content_type and a.content_type.startswith('image/')) or a.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))), None)
        if not image:
            await message.reply("Нужен файл изображения PNG, JPG или WEBP.", mention_author=False); return
        await self._update_review(message.guild, r, message, image)
        await message.reply("✅ Скриншот получен и отправлен на проверку. Платёж будет учтён только после одобрения.", mention_author=False)

    async def refresh_panel(self, guild):
        cid = await self.bot.db.get_setting(guild.id, "treasury_panel_channel_id")
        mid = await self.bot.db.get_setting(guild.id, "treasury_panel_message_id")
        if not cid or not mid: return
        ch = guild.get_channel(int(cid))
        if not ch: return
        try: msg = await ch.fetch_message(int(mid))
        except discord.HTTPException: return
        total = await self.bot.db.fetchone("SELECT COALESCE(SUM(amount),0) AS total FROM treasury WHERE guild_id=?", (guild.id,))
        fine = await self.bot.db.fetchone("SELECT COALESCE(SUM(amount-COALESCE(paid,0)),0) AS debt FROM fines WHERE guild_id=? AND COALESCE(cancelled,0)=0", (guild.id,))
        contracts = await self.bot.db.fetchall("SELECT * FROM contracts WHERE guild_id=? AND status!='closed'", (guild.id,))
        cog = self.bot.get_cog("Contracts")
        debt = 0.0
        if cog:
            for contract in contracts:
                debt += await cog._get_debt_for_treasury(contract)
        e = make("🏦 Казна · платежи и предоплаты", "Все платежи отправляются **только со скриншотом**. После отправки скриншота назначенная роль проверит его и одобрит или отклонит заявку.\n\n"
                 f"**Зачислено через панель:** {money(total['total'])}\n**Остаток штрафов:** {money(fine['debt'])}\n**Остаток долгов по открытым контрактам:** {money(debt)}\n\n"
                 "⚖️ **Оплатить штраф** — выбрать штраф из личного баланса.\n📄 **Оплатить контракт** — выбрать долг по контракту.\n💳 **Предоплата** — нажмите одну кнопку и выберите: штраф или контракт.", COLOR['main'], footer="Состояние обновляется автоматически после подтверждения платежей")
        try: await msg.edit(embed=e, view=TreasuryPanelView(self.bot))
        except discord.HTTPException: pass

    async def refresh_common_fund_panel(self, guild):
        cid = await self.bot.db.get_setting(guild.id, "common_fund_panel_channel_id")
        mid = await self.bot.db.get_setting(guild.id, "common_fund_panel_message_id")
        if not cid or not mid:
            return
        ch = guild.get_channel(int(cid))
        if not ch:
            return
        try:
            msg = await ch.fetch_message(int(mid))
        except discord.HTTPException:
            return
        row = await self.bot.db.fetchone("SELECT COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE -amount END),0) AS balance, COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE 0 END),0) AS deposits, COALESCE(SUM(CASE WHEN direction='out' THEN amount ELSE 0 END),0) AS withdrawals FROM common_fund_transactions WHERE guild_id=?", (guild.id,))
        recent = await self.bot.db.fetchall("SELECT * FROM common_fund_transactions WHERE guild_id=? ORDER BY id DESC LIMIT 5", (guild.id,))
        lines = []
        for r in recent:
            icon = "➕" if r["direction"] == "in" else "➖"
            lines.append(f"{icon} **{money(r['amount'])}** · {r['note']} · <@{r['user_id']}> · <t:{int(r['ts'])}:R>")
        description = (f"## Баланс общака: {money(row['balance'])}\n\n"
                       f"**Всего внесено:** {money(row['deposits'])}\n**Всего снято:** {money(row['withdrawals'])}\n\n"
                       f"### Последние операции\n" + ("\n".join(lines) if lines else "Операций пока нет."))
        e = make("🏦 Общак · баланс и операции", description[:4000], COLOR["main"], footer="Баланс автоматически обновляется после каждой операции")
        try:
            await msg.edit(embed=e, view=CommonFundPanelView(self.bot), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    @app_commands.command(name="общак_панель", description="Создать панель баланса общака с внесением и снятием денег")
    @app_commands.rename(channel="канал")
    @app_commands.describe(channel="Канал, в котором будет панель общака")
    @staff_only()
    async def common_fund_panel(self, i: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.set_setting(i.guild_id, "common_fund_panel_channel_id", channel.id)
        e = make("🏦 Общак · баланс и операции", "Здесь отображается текущий баланс общака. Баланс меняется автоматически после внесения или снятия денег.\n\n➕ **Внести деньги**\n➖ **Снять деньги**", COLOR["main"])
        msg = await channel.send(embed=e, view=CommonFundPanelView(self.bot))
        await self.bot.db.set_setting(i.guild_id, "common_fund_panel_message_id", msg.id)
        await self.refresh_common_fund_panel(i.guild)
        await i.response.send_message(f"✅ Панель общака опубликована: {msg.jump_url}", ephemeral=True)

    @app_commands.command(name="казна_панель", description="Опубликовать отдельную панель платежей казны")
    @app_commands.rename(channel="канал")
    @app_commands.describe(channel="Канал для панели казны")
    @staff_only()
    async def treasury_panel(self, i: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.set_setting(i.guild_id, "treasury_panel_channel_id", channel.id)
        e = make("🏦 Казна · платежи и предоплаты", "Все платежи отправляются **только со скриншотом**. После отправки скриншота назначенная роль проверит его и одобрит или отклонит заявку.\n\n"
                 "⚖️ **Оплатить штраф** · 📄 **Оплатить контракт**\n💳 **Предоплата** — выберите штраф или контракт в следующем меню.", COLOR['main'])
        msg = await channel.send(embed=e, view=TreasuryPanelView(self.bot))
        await self.bot.db.set_setting(i.guild_id, "treasury_panel_message_id", msg.id)
        await self.refresh_panel(i.guild)
        await i.response.send_message(f"✅ Панель казны опубликована: {msg.jump_url}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Treasury(bot))
