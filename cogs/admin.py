import discord
from typing import Union
from utils import CAT, LOG_CATS
from discord import app_commands
from discord.ext import commands

CHANNELS = {"log_channel": "Логи сервера", "applications_channel": "Рассмотрение заявок",
            "afk_channel": "Канал АФК", "fleet_channel": "Автопарк", "call_channel": "Обзвон: канал по умолчанию", "contracts_channel": "Контракты",
            "tempvoice_hub": "Временные войсы: канал «зайди, чтобы создать»",
            "payment_fines_channel": "Ветки оплаты штрафов", "payment_contracts_channel": "Ветки оплаты контрактов",
            "payment_prepay_fines_channel": "Ветки предоплаты штрафов", "payment_prepay_contracts_channel": "Ветки предоплаты контрактов"}
ROLES = {
    "accepted_role": "Роль при принятии заявки",
    "afk_role": "Роль АФК",
    "staff_role": "Роль персонала",
    "fine_issue": "Может выдавать штрафы",
    "contracts_create": "Может создавать контракты",
    "contracts_manage": "Может закрывать/редактировать контракты",
    "applications_review": "Может рассматривать, принимать и отклонять заявки",
    "summon_manage": "Может проводить обзвон/созыв в войс",
    "contracts_review": "Может проверять оплату контрактов (скриншоты)",
    "treasury_fine_review": "Может одобрять/отклонять оплату штрафов",
    "treasury_contract_review": "Может одобрять/отклонять оплату контрактов",
    "vzp_create": "Может создавать объявления ВЗП",
    "afk_pull": "Может принудительно возвращать участников из АФК",
    "fleet_take_away": "Может принудительно забирать выданные машины",
    "fleet_return_any": "Может возвращать чужие машины",
    "fleet_manage": "Может управлять автопарком (добавить/списать/статус)",
    "treasury_deposit": "Может вносить деньги в общак",
    "treasury_withdraw": "Может снимать деньги из общака",
    "family_manage": "Может управлять составом семьи и рангами",
    "photos_manage": "Может управлять фотографиями",
}

class Admin(commands.Cog):
    def __init__(self, bot): self.bot = bot

    async def interaction_check(self, i: discord.Interaction):
        if self.bot.is_owner_user(i.user) or i.user.guild_permissions.administrator or i.user.guild_permissions.manage_guild:
            return True
        await i.response.send_message("🔒 Настройки бота доступны только владельцу бота и администраторам сервера.", ephemeral=True)
        return False

    @app_commands.command(name="настроить_канал", description="Назначить канал для функции бота")
    @app_commands.rename(type="тип", channel="канал")
    @app_commands.describe(type="Какую функцию настроить", channel="Канал для этой функции")
    @app_commands.choices(type=[app_commands.Choice(name=v, value=k) for k, v in CHANNELS.items()])
    async def setup_channel(self, i, type: app_commands.Choice[str], channel: Union[discord.TextChannel, discord.VoiceChannel]):
        await self.bot.db.set_setting(i.guild_id, type.value, channel.id)
        await self.bot.log(i.guild, "server", "Настройка канала", f"{type.name}: {channel.mention}", i.user)
        await i.response.send_message(f"✅ {type.name}: {channel.mention}", ephemeral=True)

    @app_commands.command(name="настроить_доступ_панели", description="Разрешить или запретить роли открывать /панель и её административные кнопки")
    @app_commands.rename(role="роль", action="действие")
    @app_commands.describe(role="Роль, которой настраивается доступ", action="Разрешить доступ или убрать его")
    @app_commands.choices(action=[
        app_commands.Choice(name="Разрешить доступ", value="add"),
        app_commands.Choice(name="Запретить доступ", value="remove"),
    ])
    async def configure_panel_access(self, i: discord.Interaction, role: discord.Role, action: app_commands.Choice[str]):
        if not (self.bot.is_owner_user(i.user) or i.user.guild_permissions.administrator or i.user.guild_permissions.manage_guild):
            return await i.response.send_message("🔒 Настраивать доступ к панели могут только администраторы сервера.", ephemeral=True)
        raw = await self.bot.db.get_setting(i.guild_id, "admin_panel_roles")
        ids = set()
        if raw:
            for part in str(raw).split(","):
                try: ids.add(int(part.strip()))
                except ValueError: pass
        if action.value == "add": ids.add(role.id)
        else: ids.discard(role.id)
        await self.bot.db.set_setting(i.guild_id, "admin_panel_roles", ",".join(str(x) for x in sorted(ids)))
        current = " ".join(f"<@&{rid}>" for rid in sorted(ids)) or "дополнительные роли не назначены"
        verb = "получила доступ к" if action.value == "add" else "лишена доступа к"
        await i.response.send_message(f"✅ Роль {role.mention} {verb} административной панели.\n**Разрешённые роли:** {current}\nАдминистраторы сервера и роль руководства сохраняют доступ.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="настроить_роль", description="Назначить роль для функции бота")
    @app_commands.rename(type="тип", role="роль")
    @app_commands.describe(type="Какую роль настроить", role="Какая роль")
    @app_commands.choices(type=[app_commands.Choice(name=v, value=k) for k, v in ROLES.items()])
    async def setup_role(self, i, type: app_commands.Choice[str], role: discord.Role):
        if type.value in {"accepted_role", "afk_role", "staff_role"}:
            await self.bot.db.set_setting(i.guild_id, type.value, role.id)
            message = f"✅ {type.name}: {role.mention}"
        else:
            key = f"role_permission:{type.value}"
            raw = await self.bot.db.get_setting(i.guild_id, key)
            ids = {int(x.strip()) for x in str(raw or "").split(",") if x.strip().isdigit()}
            ids.add(role.id)
            await self.bot.db.set_setting(i.guild_id, key, ",".join(str(x) for x in sorted(ids)))
            message = f"✅ Роли {role.mention} выдано право: **{type.name}**. Можно назначить несколько ролей. Для снятия прав открой `/панель` → «Права ролей»."
        await self.bot.log(i.guild, "server", "Настройка роли", f"{type.name}: {role.mention}", i.user)
        await i.response.send_message(message, ephemeral=True)

    # ---------- логи: отдельный канал под каждую функцию ----------

    @app_commands.command(name="настроить_тег_оплаты", description="Добавить или убрать роль, которую бот будет тегать в ветках оплаты")
    @app_commands.rename(kind="тип_оплаты", role="роль", action="действие")
    @app_commands.describe(kind="Для какого типа платежей тегать роль", role="Роль для упоминания", action="Добавить роль в список или убрать её")
    @app_commands.choices(kind=[
        app_commands.Choice(name="Оплата и предоплата штрафов", value="fine"),
        app_commands.Choice(name="Оплата и предоплата контрактов", value="contract"),
    ], action=[
        app_commands.Choice(name="Добавить", value="add"),
        app_commands.Choice(name="Убрать", value="remove"),
    ])
    async def configure_payment_ping_role(self, i: discord.Interaction, kind: app_commands.Choice[str], role: discord.Role, action: app_commands.Choice[str]):
        key = "treasury_fine_ping_roles" if kind.value == "fine" else "treasury_contract_ping_roles"
        raw = await self.bot.db.get_setting(i.guild_id, key)
        ids = set()
        if raw:
            for part in str(raw).split(","):
                try: ids.add(int(part.strip()))
                except ValueError: pass
        if action.value == "add":
            ids.add(role.id)
        else:
            ids.discard(role.id)
        await self.bot.db.set_setting(i.guild_id, key, ",".join(str(x) for x in sorted(ids)))
        current = " ".join(f"<@&{rid}>" for rid in sorted(ids)) or "не назначены"
        verb = "добавлена в список" if action.value == "add" else "убрана из списка"
        await i.response.send_message(f"✅ Роль {role.mention} {verb} для веток: **{kind.name}**.\nТекущий список: {current}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="настроить_логи", description="Автоматически создать категорию с отдельным лог-каналом для каждой функции")
    async def setup_logs(self, i):
        await i.response.defer(ephemeral=True)
        g, db = i.guild, self.bot.db
        ow = {g.default_role: discord.PermissionOverwrite(view_channel=False),
              g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)}
        rid = await db.get_setting(g.id, "staff_role")
        staff = g.get_role(int(rid)) if rid else None
        if staff:
            ow[staff] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
        cid = await db.get_setting(g.id, "log_category")
        cat = g.get_channel(int(cid)) if cid else None
        created = []
        try:
            if not isinstance(cat, discord.CategoryChannel):
                cat = await g.create_category("📋 Логи", overwrites=ow, reason="Логи бота")
                await db.set_setting(g.id, "log_category", cat.id)
            for key in LOG_CATS:
                cur = await db.get_setting(g.id, f"log_{key}")
                if cur and g.get_channel(int(cur)):
                    continue   # канал уже есть — не дублируем
                icon, label = CAT[key]
                ch = await g.create_text_channel(f"{icon}・лог-{key}", category=cat, overwrites=ow,
                                                 topic=f"Автоматические логи: {label}", reason="Логи бота")
                await db.set_setting(g.id, f"log_{key}", ch.id)
                created.append(ch.mention)
        except discord.Forbidden:
            return await i.followup.send("❌ У бота нет права **«Управление каналами»**. Выдайте его роли бота и повторите.", ephemeral=True)
        await self.bot.log(g, "server", "Лог-каналы созданы", " ".join(created) or "новых не потребовалось", i.user, 0x57F287)
        await i.followup.send("✅ Созданы лог-каналы: " + (" ".join(created) or "все уже существуют") +
                              ("" if staff else "\nℹ️ Роль персонала не задана — каналы видят только администраторы (`/настроить_роль`)."), ephemeral=True)

    @app_commands.command(name="настроить_лог_канал", description="Назначить свой канал для логов одной функции")
    @app_commands.rename(category="категория", channel="канал")
    @app_commands.describe(category="Для какой функции этот лог-канал", channel="Канал для логов")
    @app_commands.choices(category=[app_commands.Choice(name=f"{CAT[k][0]} {CAT[k][1]}", value=k) for k in LOG_CATS])
    async def setup_log_channel(self, i, category: app_commands.Choice[str], channel: discord.TextChannel):
        await self.bot.db.set_setting(i.guild_id, f"log_{category.value}", channel.id)
        await i.response.send_message(f"✅ Логи «{category.name}» → {channel.mention}", ephemeral=True)

    @app_commands.command(name="настроить_процент", description="Процент семьи от суммы гонораров исполнителей (по умолчанию)")
    @app_commands.rename(percent="процент")
    @app_commands.describe(percent="0–100. Процент рассчитывается от суммы гонораров исполнителей")
    async def setup_percent(self, i, percent: app_commands.Range[float, 0.0, 100.0]):
        await self.bot.db.set_setting(i.guild_id, "contract_percent", percent)
        await i.response.send_message(f"✅ Доля семьи установлена: **{percent:g}% от суммы гонораров исполнителей** (для новых контрактов)", ephemeral=True)

    @app_commands.command(name="настроить_неактивность", description="Через сколько дней без активности выдавать авто-АФК")
    @app_commands.rename(days="дни")
    @app_commands.describe(days="Число дней (0 — отключить)")
    async def setup_inactive(self, i, days: app_commands.Range[int, 0, 60]):
        await self.bot.db.set_setting(i.guild_id, "inactive_days", days)
        await i.response.send_message("✅ Авто-АФК отключён." if days == 0 else f"✅ Авто-АФК через **{days}** дн. без сообщений и голоса.", ephemeral=True)

    @app_commands.command(name="настройки", description="Показать текущие настройки")
    async def settings(self, i):
        lines = []
        for k, v in {**CHANNELS, **ROLES}.items():
            val = await self.bot.db.get_setting(i.guild_id, k)
            pre = "#" if k in CHANNELS else "@&"
            lines.append(f"**{v}:** " + (f"<{pre}{val}>" if val else "не задано"))
        for k in LOG_CATS:
            val = await self.bot.db.get_setting(i.guild_id, f"log_{k}")
            lines.append(f"**Лог · {CAT[k][1]}:** " + (f"<#{val}>" if val else "общий канал логов"))
        db, g = self.bot.db, i.guild_id
        fine_pings = await db.get_setting(i.guild_id, "treasury_fine_ping_roles")
        contract_pings = await db.get_setting(i.guild_id, "treasury_contract_ping_roles")
        lines.append("**Теги в ветках оплаты штрафов:** " + (" ".join(f"<@&{x}>" for x in str(fine_pings).split(",") if x.strip().isdigit()) if fine_pings else "не заданы"))
        lines.append("**Теги в ветках оплаты контрактов:** " + (" ".join(f"<@&{x}>" for x in str(contract_pings).split(",") if x.strip().isdigit()) if contract_pings else "не заданы"))
        lines.append(f"**Процент семьи от гонораров:** {float(await db.get_setting(g, 'contract_percent') or 0):g}%")
        days = int(await db.get_setting(g, "inactive_days") or 4)
        lines.append("**Авто-АФК:** " + (f"через {days} дн." if days else "выключен"))
        lines.append("**Набор заявок:** " + ("🔒 закрыт" if await db.get_setting(g, "applications_open") == "0" else "🟢 открыт"))
        await i.response.send_message("\n".join(lines), ephemeral=True)

async def setup(bot): await bot.add_cog(Admin(bot))
