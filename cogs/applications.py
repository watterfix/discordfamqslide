import json, time, discord
from pathlib import Path
import media
from discord import app_commands
from discord.ext import commands
from utils import staff_only, staff_or_permission, make, COLOR, FAMILY

OPEN = ("pending", "reviewing", "called")
STATUS_TXT = {"pending": "⏳ Ожидает решения", "reviewing": "👀 На рассмотрении", "called": "📞 Приглашён на обзвон"}
STATUS_COLOR = {"pending": COLOR["gold"], "reviewing": COLOR["main"], "called": 0x9B59B6}
STATUS_FIELD = "📌 Статус"


def set_status(e, text):
    for n, f in enumerate(e.fields):
        if f.name == STATUS_FIELD:
            e.set_field_at(n, name=STATUS_FIELD, value=text, inline=False)
            return
    e.add_field(name=STATUS_FIELD, value=text, inline=False)

BANNERS = {True: Path(__file__).resolve().parent.parent / "assets" / "banner_open.png",
           False: Path(__file__).resolve().parent.parent / "assets" / "banner_closed.png"}

def panel_payload(is_open):
    """Панель заявок: только баннер (открыто/закрыто) без текста. Возвращает (embed, [файл])."""
    path = BANNERS[bool(is_open)]
    f = discord.File(path, filename=path.name)
    e = discord.Embed(color=0x0B0B0B)
    e.set_image(url=f"attachment://{f.filename}")
    return e, [f]

async def dm(member, text):
    if not member: return False
    try:
        await member.send(text)
        return True
    except discord.HTTPException:
        return False


class ApplicationModal(discord.ui.Modal, title="Заявка на вступление"):
    nick = discord.ui.TextInput(label="Имя Фамилия | Static ID", max_length=60, placeholder="John_Smith | 12345")
    age = discord.ui.TextInput(label="Возраст (IRL) | Часов в игре", max_length=40, placeholder="18 | 250")
    prev = discord.ui.TextInput(label="Прошлые семьи / причина ухода", style=discord.TextStyle.paragraph, required=False, max_length=400)
    online = discord.ui.TextInput(label="Прайм-тайм онлайн (МСК)", max_length=60, placeholder="18:00–01:00")
    about = discord.ui.TextInput(label="О себе, стрельба, опыт в крайме", style=discord.TextStyle.paragraph, max_length=700)

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, i):
        bot, db = self.bot, self.bot.db
        if await db.get_setting(i.guild_id, "applications_open") == "0":
            return await i.response.send_message("🔒 Набор сейчас закрыт.", ephemeral=True)
        ch = await bot.get_ch(i.guild, "applications_channel")
        if not ch:
            return await i.response.send_message("Канал заявок не настроен.", ephemeral=True)
        if await db.fetchone("SELECT 1 FROM applications WHERE guild_id=? AND user_id=? AND status IN ('pending','reviewing','called')", (i.guild_id, i.user.id)):
            return await i.response.send_message("У вас уже есть заявка на рассмотрении.", ephemeral=True)
        data = {"Имя | Static ID": self.nick.value, "Возраст | Часы": self.age.value, "Прошлые семьи": self.prev.value or "—",
                "Прайм-тайм": self.online.value, "О себе": self.about.value}
        app_id = await db.execute("INSERT INTO applications(guild_id,user_id,data,status,created) VALUES(?,?,?,?,?)",
                                  (i.guild_id, i.user.id, json.dumps(data, ensure_ascii=False), "pending", int(time.time())))
        e = make(f"📝 Заявка №{app_id}", f"### {i.user.display_name}\n{i.user.mention} · `{i.user.id}`\nАккаунт создан <t:{int(i.user.created_at.timestamp())}:R>",
                 COLOR["gold"], footer=STATUS_TXT["pending"])
        e.set_thumbnail(url=i.user.display_avatar.url)
        for k, v in data.items():
            e.add_field(name=k, value=v, inline=k in ("Имя | Static ID", "Возраст | Часы", "Прайм-тайм"))
        set_status(e, STATUS_TXT["pending"])
        # Упоминать выбранные роли в сообщении новой заявки.
        raw_roles = await db.get_setting(i.guild_id, "application_ping_roles")
        role_mentions = []
        if raw_roles:
            for raw_id in raw_roles.split(","):
                try:
                    role = i.guild.get_role(int(raw_id.strip()))
                    if role and not role.is_default():
                        role_mentions.append(role.mention)
                except (ValueError, AttributeError):
                    continue
        # Список разрешённых упоминаний ограничен только настроенными ролями.
        msg = await ch.send(
            content=" ".join(role_mentions) if role_mentions else None,
            embed=e, view=ReviewView(bot),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False)
        )
        await db.execute("UPDATE applications SET message_id=? WHERE id=?", (msg.id, app_id))
        await bot.log(i.guild, "application", f"Новая заявка #{app_id}", i.user.mention, i.user, 0xF1C40F)
        await i.response.send_message("✅ Заявка отправлена, ожидайте решения.", ephemeral=True)


class PanelView(discord.ui.View):
    def __init__(self, bot, is_open=True):
        super().__init__(timeout=None)
        self.bot = bot
        self.apply.disabled = not is_open
        self.apply.label = "Подать заявку" if is_open else "Набор закрыт"

    @discord.ui.button(label="Подать заявку", style=discord.ButtonStyle.primary, emoji="📝", custom_id="app:open")
    async def apply(self, i, b):
        if await self.bot.db.get_setting(i.guild_id, "applications_open") == "0":
            return await i.response.send_message("🔒 Набор сейчас закрыт.", ephemeral=True)
        await i.response.send_modal(ApplicationModal(self.bot))


class ReviewView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _app(self, i):
        if not (await self.bot.is_staff(i.user) or await self.bot.has_role_permission(i.user, "applications_review")):
            await i.response.send_message("Нет прав на рассмотрение заявок.", ephemeral=True)
            return None
        app = await self.bot.db.fetchone("SELECT * FROM applications WHERE message_id=?", (i.message.id,))
        if not app or app["status"] not in OPEN:
            await i.response.send_message("Заявка уже обработана.", ephemeral=True)
            return None
        return app

    async def apply(self, guild, user, app, msg, status, vc=None):
        """Меняет статус заявки, обновляет карточку и оповещает заявителя. True — если ЛС дошло."""
        db = self.bot.db
        await db.execute("UPDATE applications SET status=?, reviewer_id=? WHERE id=?", (status, user.id, app["id"]))
        e = msg.embeds[0]
        e.color = STATUS_COLOR[status]
        where = f" · {vc.mention}" if vc else ""
        set_status(e, f"{STATUS_TXT[status]}{where} — {user.mention}")
        e.set_footer(text=f"{STATUS_TXT[status]} • {user.display_name}")
        name = await media.filename(db, guild.id, "application", app["id"])
        if name: e.set_image(url=f"attachment://{name}")
        await msg.edit(embed=e)
        member = guild.get_member(app["user_id"])
        if status == "reviewing":
            text = f"👀 Ваша заявка на **{guild.name}** взята на рассмотрение.\nРассматривает: {user.display_name}. Ожидайте решения."
        else:
            text = (f"📞 Вас приглашают на обзвон по заявке на **{guild.name}**.\n"
                    f"Зайдите в голосовой канал {vc.mention} и дождитесь сотрудника ({user.display_name}).")
        sent = await dm(member, text)
        await self.bot.log(guild, "application", f"Заявка #{app['id']}: {STATUS_TXT[status].split(' ', 1)[1].lower()}",
                           f"<@{app['user_id']}> · {user.mention}" + where, user, STATUS_COLOR[status])
        return sent

    async def start_review(self, i):
        app = await self._app(i)
        if not app: return
        if app["status"] == "reviewing":
            return await i.response.send_message("Заявка уже на рассмотрении.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        sent = await self.apply(i.guild, i.user, app, i.message, "reviewing")
        await i.followup.send("✅ Заявитель оповещён." if sent else "⚠️ Не удалось отправить ЛС (закрыты). Свяжитесь с ним вручную.", ephemeral=True)

    async def start_call(self, i):
        app = await self._app(i)
        if not app: return
        default = None   # канал из /настроить_канал подставляется как вариант по умолчанию
        cid = await self.bot.db.get_setting(i.guild_id, "call_channel")
        ch = i.guild.get_channel(int(cid)) if cid else None
        if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            default = ch
        who = i.guild.get_member(app["user_id"])
        await i.response.send_message(f"📞 Выберите голосовой канал, куда пригласить **{who.display_name if who else 'заявителя'}** (заявка №{app['id']}):",
                                      view=CallPicker(self, app["id"], i.message, default), ephemeral=True)

    async def decide(self, i, accepted):
        bot, db = self.bot, self.bot.db
        app = await self._app(i)
        if not app: return
        status = "accepted" if accepted else "rejected"
        await db.execute("UPDATE applications SET status=?, reviewer_id=? WHERE id=?", (status, i.user.id, app["id"]))
        e = i.message.embeds[0]
        e.color = COLOR["ok"] if accepted else COLOR["bad"]
        set_status(e, f"{'✅ Принята' if accepted else '❌ Отклонена'} — {i.user.mention}")
        e.set_footer(text=("✅ Принята" if accepted else "❌ Отклонена") + f" • {i.user.display_name}")
        files = await media.attach(db, i.guild_id, "application", app["id"], e)
        await i.response.edit_message(embed=e, attachments=files, view=None)
        member = i.guild.get_member(app["user_id"])
        if member:
            if accepted:
                rid = await db.get_setting(i.guild_id, "accepted_role")
                role = i.guild.get_role(int(rid)) if rid else None
                if role:
                    try: await member.add_roles(role, reason=f"Заявка #{app['id']}")
                    except discord.Forbidden: pass
            await dm(member, f"Ваша заявка на **{i.guild.name}** " + ("принята 🎉" if accepted else "отклонена."))
        await bot.log(i.guild, "application", f"Заявка #{app['id']} {'принята' if accepted else 'отклонена'}",
                      f"<@{app['user_id']}> · рассмотрел {i.user.mention}", i.user, e.color.value)

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.success, custom_id="app:accept", row=0)
    async def accept(self, i, b): await self.decide(i, True)

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger, custom_id="app:reject", row=0)
    async def reject(self, i, b): await self.decide(i, False)

    @discord.ui.button(label="На рассмотрение", style=discord.ButtonStyle.primary, emoji="👀", custom_id="app:review", row=1)
    async def review(self, i, b): await self.start_review(i)

    @discord.ui.button(label="Обзвон", style=discord.ButtonStyle.secondary, emoji="📞", custom_id="app:call", row=1)
    async def call(self, i, b): await self.start_call(i)


class CallPicker(discord.ui.View):
    """Меню выбора голосового канала для обзвона (видит только проводящий)."""
    def __init__(self, review, app_id, msg, default=None):
        super().__init__(timeout=180)
        self.review, self.app_id, self.msg = review, app_id, msg
        self.select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.voice, discord.ChannelType.stage_voice],
            placeholder="Голосовой канал для обзвона", min_values=1, max_values=1,
            default_values=[default] if default else [])
        self.select.callback = self.picked
        self.add_item(self.select)

    async def picked(self, i):
        await i.response.defer()
        vc = i.guild.get_channel(self.select.values[0].id) or self.select.values[0]
        app = await self.review.bot.db.fetchone("SELECT * FROM applications WHERE id=?", (self.app_id,))
        if not app or app["status"] not in OPEN:
            return await i.edit_original_response(content="Заявка уже обработана.", view=None)
        sent = await self.review.apply(i.guild, i.user, app, self.msg, "called", vc)
        await i.edit_original_response(
            content=f"✅ Приглашение в {vc.mention} отправлено." if sent else f"⚠️ ЛС заявителя закрыты, карточка обновлена. Позовите его в {vc.mention} вручную.",
            view=None)
        self.stop()


class Applications(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(PanelView(self.bot))
        self.bot.add_view(ReviewView(self.bot))

    @app_commands.command(name="заявки_панель", description="Отправить панель с баннером и кнопкой подачи заявки")
    @app_commands.rename(channel="канал")
    @app_commands.describe(channel="Канал для панели заявок")
    @staff_only()
    async def panel(self, i, channel: discord.TextChannel):
        await i.response.defer(ephemeral=True)
        is_open = await self.bot.db.get_setting(i.guild_id, "applications_open") != "0"
        e, files = panel_payload(is_open)
        msg = await channel.send(embed=e, view=PanelView(self.bot, is_open), files=files)
        await self.bot.db.set_setting(i.guild_id, "panel_channel", channel.id)
        await self.bot.db.set_setting(i.guild_id, "panel_message", msg.id)
        await self.bot.log(i.guild, "application", "Панель заявок отправлена", channel.mention, i.user)
        await i.followup.send(f"Панель отправлена в {channel.mention}", ephemeral=True)


    @app_commands.command(name="заявки_роль_тег", description="Настроить роли, которые бот упоминает при создании заявки")
    @app_commands.rename(role="роль", action="действие")
    @app_commands.describe(role="Роль для упоминания", action="Добавить роль или убрать её из списка")
    @app_commands.choices(action=[
        app_commands.Choice(name="Добавить", value="add"),
        app_commands.Choice(name="Убрать", value="remove"),
    ])
    @staff_only()
    async def application_ping_role(self, i: discord.Interaction, role: discord.Role, action: app_commands.Choice[str]):
        db = self.bot.db
        raw = await db.get_setting(i.guild_id, "application_ping_roles")
        ids = []
        for value in (raw or "").split(","):
            try:
                rid = int(value.strip())
                if i.guild.get_role(rid) and rid not in ids:
                    ids.append(rid)
            except ValueError:
                continue
        if action.value == "add":
            if role.id not in ids:
                ids.append(role.id)
            result = f"✅ Роль {role.mention} добавлена в упоминания новых заявок."
        else:
            ids = [rid for rid in ids if rid != role.id]
            result = f"✅ Роль {role.mention} убрана из упоминаний новых заявок."
        await db.set_setting(i.guild_id, "application_ping_roles", ",".join(map(str, ids)))
        current = [i.guild.get_role(rid).mention for rid in ids if i.guild.get_role(rid)]
        result += "\n\n**Сейчас упоминаются:** " + (", ".join(current) if current else "никто")
        await i.response.send_message(result, ephemeral=True)

    async def set_open(self, guild, user, is_open):
        """Открывает/закрывает набор и меняет баннер панели. Возвращает примечание (пустое, если всё ок)."""
        db = self.bot.db
        await db.set_setting(guild.id, "applications_open", "1" if is_open else "0")
        cid, mid = await db.get_setting(guild.id, "panel_channel"), await db.get_setting(guild.id, "panel_message")
        note = "\n⚠️ Панель не найдена: отправьте её заново через `/заявки_панель`."
        if cid and mid:
            try:
                msg = await guild.get_channel(int(cid)).fetch_message(int(mid))
                e, files = panel_payload(is_open)
                await msg.edit(embed=e, attachments=files, view=PanelView(self.bot, is_open))
                note = ""
            except (AttributeError, discord.HTTPException):
                pass
        await self.bot.log(guild, "application", "Набор открыт" if is_open else "Набор закрыт", "", user, COLOR["ok"] if is_open else COLOR["bad"])
        return note

    async def _toggle(self, i, is_open):
        await i.response.defer(ephemeral=True)
        note = await self.set_open(i.guild, i.user, is_open)
        await i.followup.send(("🟢 Набор открыт." if is_open else "🔒 Набор закрыт.") + note, ephemeral=True)

    @app_commands.command(name="заявки_открыть", description="Открыть приём заявок")
    @staff_or_permission("applications_review")
    async def open_(self, i): await self._toggle(i, True)

    @app_commands.command(name="заявки_закрыть", description="Закрыть приём заявок")
    @staff_or_permission("applications_review")
    async def close_(self, i): await self._toggle(i, False)

    @app_commands.command(name="заявки_статистика", description="Статистика заявок")
    @staff_or_permission("applications_review")
    async def stats(self, i):
        rows = await self.bot.db.fetchall("SELECT status, COUNT(*) c FROM applications WHERE guild_id=? GROUP BY status", (i.guild_id,))
        names = {**STATUS_TXT, "accepted": "✅ Принято", "rejected": "❌ Отклонено"}
        state = "🔒 закрыт" if await self.bot.db.get_setting(i.guild_id, "applications_open") == "0" else "🟢 открыт"
        text = "\n".join(f"{names.get(r['status'], r['status'])}: **{r['c']}**" for r in rows) or "Заявок нет."
        await i.response.send_message(embed=make("📝 Заявки", f"Набор: **{state}**\n\n{text}", COLOR["main"]), ephemeral=True)

async def setup(bot): await bot.add_cog(Applications(bot))
