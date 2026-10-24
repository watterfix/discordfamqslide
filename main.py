import os, time, logging, discord
from pathlib import Path
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
from database import DB
from utils import make, CAT

COGS = ["admin", "logs", "applications", "afk", "fleet", "contracts", "tempvoice", "summon", "panel", "family", "activity", "photos", "stats", "help", "fines", "treasury", "vzp"]

class Tree(app_commands.CommandTree):
    async def interaction_check(self, i):
        if not i.guild:
            await i.response.send_message("Команды работают только на сервере.", ephemeral=True)
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
        if member.guild_permissions.manage_guild:
            return True
        rid = await self.db.get_setting(member.guild.id, "staff_role")
        return bool(rid) and any(r.id == int(rid) for r in member.roles)

    async def has_panel_access(self, member):
        """Доступ к административной панели: администраторы/руководство или явно разрешённые роли."""
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
        """Проверяет точечное право, выданное роли через панель руководства."""
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
