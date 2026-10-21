import time, logging, discord
import media
from discord import app_commands
from discord.ext import commands, tasks
from utils import staff_only, staff_or_permission, money, make, bar, COLOR


def execs(c):
    """ID исполнителей контракта (поддерживает старые контракты с одним исполнителем)."""
    s = c["executors"]
    if s:
        return [int(x) for x in s.strip(",").split(",") if x]
    return [c["executor_id"]] if c["executor_id"] else []

def pack(ids): return "," + ",".join(str(x) for x in ids) + ","
def pings(c): return " ".join(f"<@{x}>" for x in execs(c))
def fee_map(c):
    """Персональные гонорары. Старые контракты автоматически делят процент поровну."""
    raw = c["fees"] if "fees" in c.keys() else ""
    result = {}
    if raw:
        for part in raw.split("|"):
            try:
                uid, amount = part.split(":", 1)
                result[int(uid)] = max(0.0, float(amount))
            except (ValueError, TypeError):
                continue
    ids = execs(c)
    if not result and ids:
        # Для старых контрактов без персональных гонораров считаем amount
        # общей суммой гонораров исполнителей; процент семьи не прибавляется к ней.
        fees_total = max(0.0, float(c["amount"] or 0))
        result = {uid: round(fees_total / len(ids), 2) for uid in ids}
    return {uid: result.get(uid, 0.0) for uid in ids}

def calculate_contract_amount(fees_total, family_percent):
    """Общая сумма контракта равна сумме гонораров исполнителей.
    Доля семьи — процент от этой суммы, но не добавляется к сумме контракта.

    Например: два гонорара по 10 000 и процент семьи 10% ->
    сумма контракта 20 000, семье нужно сдать 2 000.
    """
    fees_total = round(max(0.0, float(fees_total or 0)), 2)
    family_percent = max(0.0, float(family_percent or 0))
    family_amount = round(fees_total * family_percent / 100, 2)
    return fees_total, family_amount

def family_share(c):
    """Сколько семье причитается: процент от суммы гонораров плюс штрафы."""
    fees_total = sum(fee_map(c).values())
    percent = max(0.0, float(c["percent"] or 0))
    return round(fees_total * percent / 100 + float(c["extra"] or 0), 2)

def due(c):
    if "funded" in c.keys() and not c["funded"]:
        return 0.0
    return family_share(c)
def left(c):
    """Остаток долга с учётом точности отображения денежных сумм.

    В интерфейсе суммы показываются целыми единицами валюты. Не оставляем
    технический остаток вроде 0.2 как «долг 0» и не блокируем из-за него закрытие.
    """
    remainder = max(0.0, round(float(due(c) or 0) - float(c["paid"] or 0), 2))
    return 0.0 if remainder < 0.5 else remainder

def executor_debts(c):
    """Остаток долга семьи по каждому исполнителю: его гонорар * процент семьи.
    Подтверждённые платежи вычитаются из долга того участника, от чьего имени
    они записаны в payments.user_id.
    """
    percent = max(0.0, float(c["percent"] or 0))
    result = {}
    for uid, fee in fee_map(c).items():
        total_due = round(max(0.0, fee) * percent / 100, 2)
        result[uid] = total_due
    return result


class ContractCardView(discord.ui.View):
    """Persistent action attached to each contract card."""
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot



class ContractReviewView(discord.ui.View):
    def __init__(self, bot, submission_id):
        super().__init__(timeout=None)
        self.bot, self.submission_id = bot, submission_id
        yes = discord.ui.Button(label="Внес", emoji="✅", style=discord.ButtonStyle.success, custom_id=f"contract_review:yes:{submission_id}")
        no = discord.ui.Button(label="Не внес", emoji="❌", style=discord.ButtonStyle.danger, custom_id=f"contract_review:no:{submission_id}")
        yes.callback = self.approve
        no.callback = self.reject
        self.add_item(yes); self.add_item(no)

    async def _allowed(self, i):
        if await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "contracts_review") or await self.bot.has_role_permission(i.user, "contracts_manage"):
            return True
        await i.response.send_message("🔒 Проверять оплату могут только следящие за контрактами.", ephemeral=True)
        return False

    async def approve(self, i):
        if not await self._allowed(i): return
        row = await self.bot.db.fetchone("SELECT * FROM contract_submissions WHERE id=? AND guild_id=?", (self.submission_id, i.guild_id))
        if not row or row["status"] != "pending_review":
            return await i.response.send_message("Эта заявка уже обработана или не содержит скриншота.", ephemeral=True)
        c = await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=? AND guild_id=?", (row["contract_id"], i.guild_id))
        if not c: return await i.response.send_message("Контракт не найден.", ephemeral=True)
        # Подтверждённая оплата контракта зачисляется в общий фонд (верхняя панель).
        await self.bot.db.execute(
            "INSERT INTO common_fund_transactions(guild_id,user_id,direction,amount,note,ts) VALUES(?,?,'in',?,?,?)",
            (i.guild_id, row["user_id"], row["amount"], f"Оплата контракта #{c['id']} подтверждена", int(time.time())))
        await self.bot.db.execute("UPDATE contracts SET funded=1 WHERE id=?", (c["id"],))
        await self.bot.db.execute("UPDATE contract_submissions SET status='approved', reviewer_id=? WHERE id=?", (i.user.id, self.submission_id))
        await self.bot.get_cog("Contracts").refresh(i.guild, c["id"])
        treasury_cog = self.bot.get_cog("Treasury")
        if treasury_cog:
            await treasury_cog.refresh_common_fund_panel(i.guild)
            await treasury_cog.refresh_panel(i.guild)
        await self.bot.log(i.guild, "treasury", f"Оплата контракта #{c['id']} подтверждена", f"+{money(row['amount'])} в общак\nПлательщик: <@{row['user_id']}>\nПроверил: {i.user.mention}\nИсполнители: {pings(c)}", i.user, 0x57F287)
        await i.response.edit_message(content=f"✅ **ВНЕСЕНО** · Контракт №{c['id']} · {money(row['amount'])} зачислено в общак.\nПроверил: {i.user.mention}", view=None)
        thread = i.guild.get_thread(row["thread_id"]) if row["thread_id"] else None
        if thread:
            try:
                await thread.send(f"✅ Оплата подтверждена {i.user.mention}. Деньги зачислены в общак.")
                # Mark the payment thread's starter message with the review result.
                try:
                    starter = await thread.fetch_message(thread.id)
                    await starter.add_reaction('✅')
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    pass
            except discord.HTTPException: pass

    async def reject(self, i):
        if not await self._allowed(i): return
        row = await self.bot.db.fetchone("SELECT * FROM contract_submissions WHERE id=? AND guild_id=?", (self.submission_id, i.guild_id))
        if not row or row["status"] != "pending_review":
            return await i.response.send_message("Эта заявка уже обработана или не содержит скриншота.", ephemeral=True)
        await self.bot.db.execute("UPDATE contract_submissions SET status='rejected', reviewer_id=? WHERE id=?", (i.user.id, self.submission_id))
        await i.response.edit_message(content=f"❌ **НЕ ВНЕСЕНО** · Контракт №{row['contract_id']}\nОтметил: {i.user.mention}. Можно отправить новую заявку с корректным скриншотом.", view=None)
        thread = i.guild.get_thread(row["thread_id"]) if row["thread_id"] else None
        if thread:
            try:
                await thread.send(f"❌ Оплата не подтверждена. Проверил: {i.user.mention}. При необходимости отправьте новую заявку через карточку контракта.")
                # Mark the payment thread's starter message with the review result.
                try:
                    starter = await thread.fetch_message(thread.id)
                    await starter.add_reaction('❌')
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    pass
            except discord.HTTPException: pass


class Contracts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        if getattr(self, "_legacy_cards_cleaned", False): return
        self._legacy_cards_cleaned = True
        for guild in self.bot.guilds:
            rows = await self.bot.db.fetchall("SELECT id FROM contracts WHERE guild_id=? AND message_id IS NOT NULL", (guild.id,))
            for row in rows:
                await self.refresh(guild, row["id"])

    async def cog_load(self):
        # Defensive schema guard: create this table before querying it, even if an
        # older database.py / existing bot.db was left in place during an update.
        await self.bot.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS contract_submissions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                contract_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                thread_id INTEGER,
                screenshot_url TEXT,
                screenshot_message_id INTEGER,
                review_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'waiting_screenshot',
                reviewer_id INTEGER,
                created INTEGER NOT NULL
            )
        """)
        # Миграция контрактов, созданных предыдущими версиями: раньше amount
        # включал процент семьи поверх гонораров. В актуальной модели amount
        # равен сумме персональных гонораров, а доля семьи считается отдельно.
        try:
            rows = await self.bot.db.fetchall("SELECT id, fees FROM contracts WHERE fees IS NOT NULL AND fees != ''")
            for row in rows:
                total = 0.0
                valid = False
                for part in (row["fees"] or "").split("|"):
                    try:
                        _, raw_amount = part.split(":", 1)
                        total += max(0.0, float(raw_amount))
                        valid = True
                    except (ValueError, TypeError):
                        continue
                if valid:
                    await self.bot.db.execute("UPDATE contracts SET amount=? WHERE id=?", (round(total, 2), row["id"]))
        except Exception:
            logging.exception("Не удалось нормализовать суммы старых контрактов")
        await self.bot.db.conn.commit()
        self.bot.add_view(ContractCardView(self.bot))
        rows = await self.bot.db.fetchall("SELECT id, review_message_id FROM contract_submissions WHERE status='pending_review' AND review_message_id IS NOT NULL")
        for row in rows:
            self.bot.add_view(ContractReviewView(self.bot, row["id"]), message_id=row["review_message_id"])

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild or not message.attachments:
            return
        row = await self.bot.db.fetchone("SELECT * FROM contract_submissions WHERE thread_id=? AND user_id=? AND status='waiting_screenshot' ORDER BY id DESC LIMIT 1", (message.channel.id, message.author.id))
        if not row:
            return
        image = next((a for a in message.attachments if a.content_type and a.content_type.startswith("image/")), None)
        if not image:
            await message.reply("Нужен именно скриншот/изображение. Прикрепите PNG, JPG или WEBP.", mention_author=False)
            return
        await self.bot.db.execute("UPDATE contract_submissions SET screenshot_url=?, screenshot_message_id=?, status='pending_review' WHERE id=?", (image.url, message.id, row["id"]))
        c = await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=?", (row["contract_id"],))
        e = make(f"📸 Проверка оплаты · контракт №{row['contract_id']}", f"**Контракт:** {c['title'] if c else '—'}\n**Сумма:** {money(row['amount'])}\n**Кто отправил:** <@{row['user_id']}>\n**Исполнители:** {pings(c) if c else '—'}\n**Скриншот:** [Открыть изображение]({image.url})\n\nСледящий за контрактами должен выбрать **Внес** или **Не внес**.", COLOR["warn"])
        e.set_image(url=image.url)
        review_msg = await message.channel.send(embed=e, view=ContractReviewView(self.bot, row["id"]))
        await self.bot.db.execute("UPDATE contract_submissions SET review_message_id=? WHERE id=?", (review_msg.id, row["id"]))
        self.bot.add_view(ContractReviewView(self.bot, row["id"]), message_id=review_msg.id)


    # ---------- предоплата ----------
    async def _get_debt_for_treasury(self, c):
        # Для нового контракта первая оплата может быть подтверждением финансирования.
        if "funded" in c.keys() and not c["funded"]:
            return max(0.0, float(c["amount"] or 0))
        return left(c)

    async def prepaid(self, gid, uid):
        r = await self.bot.db.fetchone("SELECT balance FROM prepaid WHERE guild_id=? AND user_id=?", (gid, uid))
        return r["balance"] if r else 0.0

    async def _pay_row(self, cid, uid, amount, note):
        await self.bot.db.execute("INSERT INTO payments(contract_id,user_id,amount,note,ts) VALUES(?,?,?,?,?)",
                                  (cid, uid, amount, note, int(time.time())))
        await self.bot.db.execute("UPDATE contracts SET paid=paid+? WHERE id=?", (amount, cid))

    async def apply_prepaid(self, guild, cid):
        """Списывает предоплату исполнителей в счёт долга нового контракта (поровну на каждого). Возвращает [(uid, сумма)]."""
        c = await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=?", (cid,))
        ids, used = execs(c), []
        if not ids: return used
        share = round(due(c) / len(ids), 2)
        for uid in ids:
            take = round(min(await self.prepaid(guild.id, uid), share, left(await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=?", (cid,)))), 2)
            if take <= 0: continue
            await self.bot.db.execute("UPDATE prepaid SET balance=balance-? WHERE guild_id=? AND user_id=?", (take, guild.id, uid))
            await self._pay_row(cid, uid, take, "Предоплата (баланс)")
            used.append((uid, take))
        return used

    @staticmethod
    def embed(c):
        ids, d, paid = execs(c), due(c), c["paid"]
        debt = left(c)
        closed = c["status"] == "closed"
        overdue = bool(c["deadline"]) and c["deadline"] < time.time() and debt > 0 and not closed
        if closed:      st, color = "🔒 Закрыт", COLOR["gray"]
        elif "funded" in c.keys() and not c["funded"]: st, color = "⏳ Ожидает подтверждения оплаты", COLOR["orange"]
        elif debt <= 0: st, color = "🟢 Полностью сдан", COLOR["ok"]
        elif overdue:   st, color = "🔴 Просрочен", COLOR["bad"]
        else:           st, color = "🟠 Идёт выплата", COLOR["orange"]
        pct = 0 if ("funded" in c.keys() and not c["funded"]) else (100 if d <= 0 else int(min(paid, d) / d * 100))
        e = make(f"📄 Контракт №{c['id']}", f"### {c['title']}\n{bar(paid, d)} **{pct}%**", color, footer=f"Контракт №{c['id']}")
        e.add_field(name="👥 Исполнители", value=pings(c) or "—", inline=False)
        e.add_field(name="📌 Статус", value=st)
        e.add_field(name="💵 Сумма контракта", value=money(c["amount"]))
        pct_setting = float(c["percent"] or 0)
        e.add_field(name="📊 Процент семьи от гонораров", value=f"Семья: **{pct_setting:g}% от гонораров**")
        fmap = fee_map(c)
        fee_lines = [f"<@{uid}> — **{money(fmap.get(uid, 0))}**" for uid in ids]
        e.add_field(name="💵 Персональный гонорар", value="\n".join(fee_lines)[:1024] or "—", inline=False)
        # Показываем персональную обязанность каждого исполнителя по проценту семьи.
        personal_debts = executor_debts(c)
        debt_lines = []
        for uid in ids:
            member_due = personal_debts.get(uid, 0.0)
            if "funded" in c.keys() and not c["funded"]:
                member_text = "⏳ ожидает подтверждения оплаты контракта"
            else:
                member_text = money(member_due)
            debt_lines.append(f"<@{uid}> — **{member_text}**")
        e.add_field(name="👤 Долг семьи по каждому исполнителю", value="\n".join(debt_lines)[:1024] or "—", inline=False)
        e.add_field(name="🧾 Всего гонораров исполнителей", value=money(sum(fmap.values())))
        if "funded" in c.keys() and not c["funded"]:
            e.add_field(name="💳 Оплата контракта", value="⏳ Ожидает скриншот и подтверждение", inline=False)
        e.add_field(name="🏦 Доля семьи по проценту", value=money(family_share(c)))
        is_funded = "funded" not in c.keys() or bool(c["funded"])
        e.add_field(name="💰 Осталось сдать семье", value=money(debt if is_funded else family_share(c)), inline=False)
        e.add_field(name="✅ Сдано", value=money(paid))
        if not is_funded:
            debt_label = "⏳ Ожидает подтверждения оплаты контракта"
        elif debt > 0:
            debt_label = f"```diff\n- {money(debt)}\n```"
        else:
            debt_label = "```diff\n+ погашено\n```"
        e.add_field(name="🔻 Долг", value=debt_label)
        if c["deadline"]:
            e.add_field(name="⏳ Срок", value=f"<t:{c['deadline']}:f> · <t:{c['deadline']}:R>", inline=False)
        return e

    async def refresh(self, guild, cid):
        c = await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=?", (cid,))
        ch = await self.bot.get_ch(guild, "contracts_channel")
        if not (c and ch and c["message_id"]): return
        try:
            msg = await ch.fetch_message(c["message_id"])
            if msg.pinned:   # контракты не должны быть закреплены
                await msg.unpin(reason="Контракты не закрепляются")
            e = self.embed(c)
            files = await media.attach(self.bot.db, guild.id, "contract", cid, e)
            await msg.edit(content=pings(c) or None, embed=e, attachments=files, view=ContractCardView(self.bot))
        except discord.HTTPException:
            pass

    async def drop_card(self, guild, c):
        ch = await self.bot.get_ch(guild, "contracts_channel")
        if ch and c["message_id"]:
            try: await ch.get_partial_message(c["message_id"]).delete()
            except discord.HTTPException: pass
        await self.bot.db.execute("UPDATE contracts SET message_id=NULL WHERE id=?", (c["id"],))

    async def _get(self, i, cid):
        c = await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=? AND guild_id=?", (cid, i.guild_id))
        if not c: await i.response.send_message("Контракт не найден.", ephemeral=True)
        return c

    # ---------- создание ----------
    @app_commands.command(name="контракт_создать", description="Создать контракт (можно сразу несколько исполнителей)")
    @app_commands.rename(title="название", executor="исполнитель", percent="процент", executor2="исполнитель_2", executor3="исполнитель_3", executor4="исполнитель_4", executor5="исполнитель_5", photo="фото", fee1="гонорар_1", fee2="гонорар_2", fee3="гонорар_3", fee4="гонорар_4", fee5="гонорар_5")
    @app_commands.describe(title="Название контракта", executor="Исполнитель", percent="Процент семьи от гонораров (по умолчанию из настроек)", executor2="Ещё исполнитель", executor3="Ещё исполнитель", executor4="Ещё исполнитель", executor5="Ещё исполнитель", photo="Фото/скрин контракта", fee1="Гонорар первого исполнителя", fee2="Гонорар второго исполнителя", fee3="Гонорар третьего исполнителя", fee4="Гонорар четвёртого исполнителя", fee5="Гонорар пятого исполнителя")
    async def create(self, i, title: str, executor: discord.Member,
                     percent: app_commands.Range[float, 0.0, 100.0] = None,
                     executor2: discord.Member = None, executor3: discord.Member = None,
                     executor4: discord.Member = None, executor5: discord.Member = None,
                     photo: discord.Attachment = None,
                     fee1: app_commands.Range[float, 0.0, 1e12] = None, fee2: app_commands.Range[float, 0.0, 1e12] = None,
                     fee3: app_commands.Range[float, 0.0, 1e12] = None, fee4: app_commands.Range[float, 0.0, 1e12] = None,
                     fee5: app_commands.Range[float, 0.0, 1e12] = None):
        await i.response.defer(ephemeral=True)
        ch = await self.bot.get_ch(i.guild, "contracts_channel")
        if not ch:
            return await i.followup.send("Сначала задайте канал: `/настроить_канал` → Контракты.", ephemeral=True)
        ids = list(dict.fromkeys(m.id for m in (executor, executor2, executor3, executor4, executor5) if m))
        if percent is None:
            percent = float(await self.bot.db.get_setting(i.guild_id, "contract_percent") or 0)
        now = int(time.time())
        supplied_fees = [fee1, fee2, fee3, fee4, fee5]
        supplied_members = [executor, executor2, executor3, executor4, executor5]
        fee_map_new = {member.id: float(fee) for member, fee in zip(supplied_members, supplied_fees) if member and fee is not None}
        if len(fee_map_new) != len(ids):
            return await i.followup.send("Укажите персональный гонорар для каждого выбранного исполнителя.", ephemeral=True)
        fees_total = sum(fee_map_new.values())
        if fees_total <= 0:
            return await i.followup.send("Сумма персональных гонораров должна быть больше нуля.", ephemeral=True)
        amount, family_amount = calculate_contract_amount(fees_total, percent)
        fees_raw = "|".join(f"{uid}:{fee_map_new[uid]:.2f}" for uid in ids if uid in fee_map_new)
        cid = await self.bot.db.execute(
            "INSERT INTO contracts(guild_id,title,client,executor_id,executors,creator_id,amount,percent,deadline,created,fees,funded) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
            (i.guild_id, title, "", ids[0], pack(ids), i.user.id, amount, percent, None, now, fees_raw))
        # Списываем предоплату контрактов исполнителей сразу при назначении на контракт.
        try:
            prepaid_used = await self.apply_prepaid(i.guild, cid)
        except Exception:
            prepaid_used = []
        warn = ""
        if photo:
            try: await media.save(self.bot.db, i.guild_id, "contract", cid, photo)
            except ValueError as ex: warn = f"\n⚠️ Фото не добавлено: {ex}"
        # Долг начисляется только после подтверждения оплаты контракта следящим.
        c = await self.bot.db.fetchone("SELECT * FROM contracts WHERE id=?", (cid,))
        e = self.embed(c)
        files = await media.attach(self.bot.db, i.guild_id, "contract", cid, e)
        msg = await ch.send(content=pings(c), embed=e, view=ContractCardView(self.bot), **media.fk(files))   # сообщение НЕ закрепляем
        await self.bot.db.execute("UPDATE contracts SET message_id=? WHERE id=?", (msg.id, cid))
        await self.bot.log(i.guild, "contract", f"Контракт #{cid} создан",
                           f"{title} · сумма {money(amount)}\nИсполнители: {pings(c)}\nОжидает подтверждения оплаты.",
                           i.user, 0x57F287)
        prepaid_note = "\nПредоплата автоматически зачтена: " + ", ".join(f"<@{uid}> −{money(value)}" for uid, value in prepaid_used) if prepaid_used else ""
        await i.followup.send(f"✅ Контракт #{cid} создан: {msg.jump_url}\nГонорары исполнителей: {money(fees_total)}\nСумма контракта: {money(amount)}\nДоля семьи ({percent:g}% от гонораров): {money(family_amount)}\nДолг исполнителей активируется после подтверждения оплаты скриншотом.{prepaid_note}{warn}", ephemeral=True)

    # ---------- исполнители ----------
    @app_commands.command(name="контракт_добавить_исполнителя", description="Добавить исполнителя в контракт")
    @app_commands.rename(contract_id="контракт", member="участник")
    @app_commands.describe(contract_id="Номер контракта", member="Кого добавить")
    @staff_or_permission("contracts_manage")
    async def add_exec(self, i, contract_id: int, member: discord.Member):
        c = await self._get(i, contract_id)
        if not c: return
        ids = execs(c)
        if member.id in ids: return await i.response.send_message("Он уже в контракте.", ephemeral=True)
        if len(ids) >= 10: return await i.response.send_message("Максимум 10 исполнителей.", ephemeral=True)
        ids.append(member.id)
        await self.bot.db.execute("UPDATE contracts SET executors=?, executor_id=? WHERE id=?", (pack(ids), ids[0], contract_id))
        await self.refresh(i.guild, contract_id)
        ch = await self.bot.get_ch(i.guild, "contracts_channel")
        if ch: await ch.send(f"📌 {member.mention}, вы добавлены в контракт №{contract_id} · **{c['title']}**")
        await self.bot.log(i.guild, "contract", f"Контракт #{contract_id}: добавлен исполнитель", member.mention, i.user)
        await i.response.send_message(f"✅ {member.mention} добавлен в контракт №{contract_id}", ephemeral=True)

    @app_commands.command(name="контракт_убрать_исполнителя", description="Убрать исполнителя из контракта")
    @app_commands.rename(contract_id="контракт", member="участник")
    @app_commands.describe(contract_id="Номер контракта", member="Кого убрать")
    @staff_or_permission("contracts_manage")
    async def remove_exec(self, i, contract_id: int, member: discord.Member):
        c = await self._get(i, contract_id)
        if not c: return
        ids = execs(c)
        if member.id not in ids: return await i.response.send_message("Его нет в контракте.", ephemeral=True)
        if len(ids) == 1: return await i.response.send_message("Нельзя убрать единственного исполнителя.", ephemeral=True)
        ids.remove(member.id)
        await self.bot.db.execute("UPDATE contracts SET executors=?, executor_id=? WHERE id=?", (pack(ids), ids[0], contract_id))
        await self.refresh(i.guild, contract_id)
        await self.bot.log(i.guild, "contract", f"Контракт #{contract_id}: убран исполнитель", member.mention, i.user, 0xFEE75C)
        await i.response.send_message(f"✅ {member.mention} убран из контракта №{contract_id}", ephemeral=True)

    # ---------- платежи ----------
    @app_commands.command(name="контракт_штраф", description="Увеличить долг по контракту (штраф/доплата)")
    @app_commands.rename(contract_id="контракт", amount="сумма", reason="причина")
    @app_commands.describe(contract_id="Номер контракта", amount="На сколько увеличить долг", reason="Причина штрафа")
    @staff_or_permission("contracts_manage")
    async def fine(self, i, contract_id: int, amount: app_commands.Range[float, 1.0, 1e12], reason: str):
        c = await self._get(i, contract_id)
        if not c: return
        await self.bot.db.execute("UPDATE contracts SET extra=COALESCE(extra,0)+? WHERE id=?", (amount, contract_id))
        await self.refresh(i.guild, contract_id)
        await self.bot.log(i.guild, "contract", f"Штраф по контракту #{contract_id}", f"+{money(amount)}: {reason}\n{pings(c)}", i.user, 0xED4245)
        await i.response.send_message(f"Долг увеличен на {money(amount)}.")

    @app_commands.command(name="контракт_закрыть", description="Закрыть контракт")
    @app_commands.rename(contract_id="контракт", force="принудительно")
    @app_commands.describe(contract_id="Номер контракта", force="Закрыть, даже если долг не погашен")
    @staff_or_permission("contracts_manage")
    async def close(self, i, contract_id: int, force: bool = False):
        c = await self._get(i, contract_id)
        if not c: return
        debt = left(c)
        if debt >= 0.5 and not force:
            return await i.response.send_message(f"Долг {money(debt)} не погашен. Используйте `force: True`, чтобы закрыть с долгом.", ephemeral=True)
        await self.bot.db.execute("UPDATE contracts SET status='closed' WHERE id=?", (contract_id,))
        await self.drop_card(i.guild, c)   # закрытый контракт не висит в канале (инфо: /контракт_инфо)
        await self.bot.log(i.guild, "contract", f"Контракт #{contract_id} закрыт", f"Остаток долга: {money(debt)}", i.user, 0x99AAB5)
        await i.response.send_message("🔒 Контракт закрыт.")

    # ---------- предоплата (личный баланс исполнителя) ----------
    @app_commands.command(name="предоплата_баланс", description="Баланс предоплаты (свой или участника)")
    @app_commands.rename(member="участник")
    @app_commands.describe(member="Участник")
    async def prepay_balance(self, i, member: discord.Member = None):
        member = member or i.user
        if member.id != i.user.id and not await self.bot.is_staff(i.user):
            return await i.response.send_message("🔒 Чужой баланс может смотреть только руководство.", ephemeral=True)
        bal = await self.prepaid(i.guild_id, member.id)
        await i.response.send_message(embed=make("💳 Предоплата", f"{member.mention}: **{money(bal)}**", COLOR["main"]), ephemeral=True)

    @app_commands.command(name="контракт_инфо", description="Информация о контракте и платежах")
    @app_commands.rename(contract_id="контракт")
    @app_commands.describe(contract_id="Номер контракта")
    async def info(self, i, contract_id: int):
        c = await self._get(i, contract_id)
        if not c: return
        e, files = await self.info_embed(i.guild_id, c)
        await i.response.send_message(embed=e, **media.fk(files))

    async def info_embed(self, gid, c):
        pays = await self.bot.db.fetchall("SELECT * FROM payments WHERE contract_id=? ORDER BY id DESC LIMIT 10", (c["id"],))
        e = self.embed(c)
        e.add_field(name="Последние платежи", value="\n".join(f"➕ **{money(p['amount'])}** · <t:{p['ts']}:d> · <@{p['user_id']}> {p['note']}" for p in pays) or "—", inline=False)
        files = await media.attach(self.bot.db, gid, "contract", c["id"], e)
        return e, files

    @app_commands.command(name="контракты", description="Список контрактов")
    @app_commands.rename(status="статус")
    @app_commands.describe(status="Какие контракты показать")
    @app_commands.choices(status=[app_commands.Choice(name="Открытые", value="open"), app_commands.Choice(name="Закрытые", value="closed")])
    async def lst(self, i, status: app_commands.Choice[str] = None):
        await i.response.send_message(embed=await self.list_embed(i.guild_id, status.value if status else "open"))

    async def list_embed(self, gid, st="open"):
        rows = await self.bot.db.fetchall("SELECT * FROM contracts WHERE guild_id=? AND status=? ORDER BY id DESC LIMIT 20", (gid, st))
        lines = []
        for r in rows:
            debt = left(r)
            icon = "🟢" if debt <= 0 else ("🔴" if r["deadline"] and r["deadline"] < time.time() else "🟠")
            lines.append(f"{icon} `№{r['id']}` **{r['title']}** · {pings(r)[:120]}\n┕ {bar(r['paid'], due(r), 8)} долг **{money(debt)}**")
        return make("📄 Контракты · " + ("открытые" if st == "open" else "закрытые"), "\n".join(lines)[:4000] or "*Пока пусто*", COLOR["main"])

    @app_commands.command(name="долги", description="Сводка долгов по исполнителям")
    async def debts(self, i):
        await i.response.send_message(embed=await self.debts_embed(i.guild_id))

    async def debts_embed(self, gid):
        rows = await self.bot.db.fetchall("SELECT * FROM contracts WHERE guild_id=? AND status='open'", (gid,))
        per, cnt = {}, {}
        for c in rows:
            if "funded" in c.keys() and not c["funded"]:
                continue
            individual = executor_debts(c)
            for uid, gross_due in individual.items():
                paid_row = await self.bot.db.fetchone(
                    "SELECT COALESCE(SUM(amount),0) AS total FROM payments WHERE contract_id=? AND user_id=?",
                    (c["id"], uid))
                paid_by_member = float(paid_row["total"] or 0) if paid_row else 0.0
                personal_left = max(0.0, round(gross_due - paid_by_member, 2))
                if personal_left <= 0:
                    continue
                per[uid] = per.get(uid, 0) + personal_left
                cnt[uid] = cnt.get(uid, 0) + 1
        ranked = sorted(per.items(), key=lambda x: -x[1])
        lines = [f"`{k}.` <@{uid}> — **{money(d)}** · {cnt[uid]} контр." for k, (uid, d) in enumerate(ranked, 1)]
        e = make("💸 Персональные долги по контрактам", "\n".join(lines)[:3800] if lines else "### Долгов нет 🎉", COLOR["orange"] if lines else COLOR["ok"])
        if lines: e.add_field(name="Итого к выплате", value=f"```diff\n- {money(sum(per.values()))}\n```", inline=False)
        return e

async def setup(bot): await bot.add_cog(Contracts(bot))
