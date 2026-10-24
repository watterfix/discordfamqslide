import os, time, logging, discord
from pathlib import Path
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
from database import DB
from utils import make, CAT

ADMIN_ONLY_COMMANDS = {
    "настроить_канал", "настроить_доступ_панели", "настроить_роль", "настроить_тег_оплаты",
    "настроить_логи", "настроить_лог_канал", "настроить_процент", "настроить_неактивность",
    "статистика_создать", "статистика_удалить", "заявки_панель", "заявки_роль_тег",
    "заявки_открыть", "заявки_закрыть", "заявки_статистика", "срок_веток_оплат", "лог_оплат",
    "общак_панель", "казна_панель", "логи", "взп_панель", "созыв_панель", "созыв",
    "фото_машины", "фото_контракта", "фото_заявки", "фото_участника", "фото_удалить",
    "штрафы_панель", "штрафы_снять", "штраф_выдать", "лог_штрафов", "участник_внести",
    "участник_убрать", "общак_выдать", "неактивные", "контракты_панель",
    "временные_войсы_панель", "афк_панель", "афк_вытащить", "контракт_добавить_исполнителя",
    "контракт_убрать_исполнителя", "контракт_штраф", "контракт_закрыть", "машина_добавить",
    "машина_списать", "машина_статус", "автопарк_пересоздать", "машина_история",
}

COGS = ["admin", "logs", "applications", "afk", "fleet", "contracts", "tempvoice", "summon", "panel", "family", "activity", "photos", "stats", "help", "fines", "treasury", "vzp"]

class Tree(app_commands.CommandTree):
    async def interaction_check(self, i):
        if not i.guild:
            await i.response.send_message("Команды работают только на сервере.", ephemeral=True)
            return False
        # Эти slash-команды относятся к администрированию. Проверяем их на уровне
        # CommandTree, чтобы ни одна команда не обходила ограничения из-за
        # отсутствующего декоратора в конкретном cog.
        command_name = getattr(i.command, "name", None)
        if command_name in ADMIN_ONLY_COMMANDS:
            member = i.user
            is_owner = getattr(member, "id", None) in Bot.OWNER_IDS
            is_admin = bool(getattr(getattr(member, "guild_permissions", None), "administrator", False)
                            or getattr(getattr(member, "guild_permissions", None), "manage_guild", False))
            # Команды с делегируемыми точечными правами должны учитывать
            # назначенные роли и на глобальном уровне CommandTree, иначе до
            # проверки в cog/декораторе пользователь не дойдёт.
            command_permissions = {
                "штраф_выдать": ("fine_issue",),
                "контракт_закрыть": ("contracts_manage",),
                "контракт_добавить_исполнителя": ("contracts_manage",),
                "контракт_убрать_исполнителя": ("contracts_manage",),
                "контракт_штраф": ("contracts_manage",),
                "взп_панель": ("vzp_create",),
                "созыв": ("summon_manage",),
                "созыв_панель": ("summon_manage",),
                "машина_добавить": ("fleet_manage",),
                "машина_списать": ("fleet_manage",),
                "машина_статус": ("fleet_manage",),
                "автопарк_пересоздать": ("fleet_manage",),
                "машина_история": ("fleet_manage",),
                "афк_вытащить": ("afk_pull",),
                "фото_машины": ("photos_manage",),
                "фото_контракта": ("photos_manage",),
                "фото_заявки": ("photos_manage",),
                "фото_участника": ("photos_manage",),
                "фото_удалить": ("photos_manage",),
                "участник_внести": ("family_manage",),
                "участник_убрать": ("family_manage",),
                "общак_выдать": ("family_manage",),
            }
            role_allowed = False
            if not (is_owner or is_admin):
                for permission in command_permissions.get(command_name, ()):
                    try:
                        if await i.client.has_role_permission(member, permission):
                            role_allowed = True
                            break
                    except Exception:
                        logging.exception("Ошибка проверки точечного права %s для /%s", permission, command_name)
            if not (is_owner or is_admin or role_allowed):
                await i.response.send_message(
                    "🔒 Для этой команды нужны права администратора или соответствующее точечное право роли в /панель.",
                    ephemeral=True)
                return False
        return True

    async def on_error(self, i, error):
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error) or "Недостаточно прав."
        else:
            logging.error("Command error", exc_info=error)
            msg = "Произошла ошибка, подробности в консоли бота."
        if i.response.is_done():
            await i.followup.send(msg, ephemeral=True)
        else:
            await i.response.send_message(msg, ephemeral=True)

class Bot(commands.Bot):
    # Постоянный владелец бота: полный доступ к функциям бота независимо от ролей сервера.
    OWNER_IDS = {724908658298716251}

    def is_owner_user(self, member):
        return getattr(member, "id", None) in self.OWNER_IDS

    def __init__(self):
        intents = discord.Intents.default()
        intents.members = intents.message_content = intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents, tree_cls=Tree)
        # Always resolve relative database paths from the project directory, not
        # from the process working directory. Otherwise launching the bot from a
        # shortcut, task scheduler, or a different shell can silently open a new
        # empty bot.db and make saved role permissions appear to have disappeared.
        configured_db_path = os.getenv("DB_PATH", "bot.db")
        db_path = Path(configured_db_path).expanduser()
        if not db_path.is_absolute():
            db_path = Path(__file__).resolve().parent / db_path
        self.db = DB(str(db_path.resolve()))

    async def setup_hook(self):
        await self.db.connect()
        logging.info("Database schema and compatibility migrations are ready")
        for c in COGS:
            await self.load_extension(f"cogs.{c}")
        gid = os.getenv("GUILD_ID")
        if gid:
            g = discord.Object(int(gid))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()

    async def is_staff(self, member):
        if self.is_owner_user(member):
            return True
        if member.guild_permissions.manage_guild:
            return True
        rid = await self.db.get_setting(member.guild.id, "staff_role")
        return bool(rid) and any(r.id == int(rid) for r in member.roles)

    async def has_panel_access(self, member):
        """Доступ к административной панели: владелец бота, администраторы/руководство или явно разрешённые роли."""
        if self.is_owner_user(member):
            return True
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        if await self.is_staff(member):
            return True
        raw = await self.db.get_setting(member.guild.id, "admin_panel_roles")
        if not raw:
            return False
        try:
            allowed = {int(x.strip()) for x in str(raw).split(",") if x.strip()}
        except ValueError:
            return False
        return any(role.id in allowed for role in getattr(member, "roles", []))

    async def has_role_permission(self, member, permission):
        """Проверяет точечное право; владелец бота всегда имеет доступ."""
        if self.is_owner_user(member):
            return True
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        raw = await self.db.get_setting(member.guild.id, f"role_permission:{permission}")
        if not raw:
            return False
        try:
            allowed = {int(x) for x in raw.split(",") if x.strip()}
        except ValueError:
            return False
        return any(role.id in allowed for role in member.roles)

    async def get_ch(self, guild, key):
        cid = await self.db.get_setting(guild.id, key)
        return guild.get_channel(int(cid)) if cid else None

    async def log(self, guild, category, title, desc="", user=None, color=0x5865F2):
        """Единая точка логирования: пишет в БД и в лог-канал."""
        await self.db.execute(
            "INSERT INTO logs(guild_id,category,user_id,title,description,ts) VALUES(?,?,?,?,?,?)",
            (guild.id, category, user.id if user else None, title, desc, int(time.time())))
        # у каждой функции свой канал (log_<категория>); если не задан — общий log_channel
        ch = await self.get_ch(guild, f"log_{category}") or await self.get_ch(guild, "log_channel")
        if ch:
            icon, label = CAT.get(category, ("📌", category))
            e = make(f"{icon} {title}", desc[:4000], color, footer=f"{label} • лог")
            if user:
                e.set_author(name=str(user), icon_url=user.display_avatar.url)
            try:
                await ch.send(embed=e)
            except discord.HTTPException:
                pass

logging.basicConfig(level=logging.INFO)
Bot().run(os.environ["DISCORD_TOKEN"])
