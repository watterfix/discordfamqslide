import os
import discord
from discord import app_commands

CUR = os.getenv("CURRENCY", "$")
FAMILY = os.getenv("FAMILY_NAME", "Family")

COLOR = dict(main=0x5865F2, ok=0x57F287, warn=0xFEE75C, bad=0xED4245,
             gold=0xF1C40F, gray=0x99AAB5, orange=0xE67E22)
# категории логов: у каждой функции свой лог-канал (ключ настройки: log_<категория>)
CAT = {"server": ("🛰️", "Сервер"), "messages": ("💬", "Сообщения"), "voice": ("🎙", "Голосовые"),
       "tempvoice": ("⏳", "Временные войсы"), "summon": ("📣", "Созыв"), "application": ("📝", "Заявки"),
       "afk": ("💤", "АФК"), "fleet": ("🚗", "Автопарк"), "fines": ("⚖️", "Штрафы"), "contract": ("📄", "Контракты"),
       "treasury": ("🏦", "Общак"), "family": ("👥", "Семья"), "activity": ("📊", "Активность")}
LOG_CATS = [k for k in CAT if k != "activity"]   # activity оставлена только для старых записей в БД
LINE = "▬" * 16
SPARK = "▁▂▃▄▅▆▇█"

def money(x):
    return f"{x:,.0f}{CUR}".replace(",", " ")

def bar(cur, total, n=12):
    ratio = 0 if total <= 0 else max(0, min(1, cur / total))
    f = round(ratio * n)
    return "▰" * f + "▱" * (n - f)

def spark(vals):
    mx = max(vals) if vals else 0
    return "".join(SPARK[min(7, int(v / mx * 7))] if mx else SPARK[0] for v in vals)

def rank_icon(rank):
    return "👑" if rank >= 9 else "⭐" if rank >= 7 else "🔹" if rank >= 4 else "▫️"

def make(title=None, desc=None, color=COLOR["main"], footer=None):
    e = discord.Embed(title=title, description=desc, color=color, timestamp=discord.utils.utcnow())
    e.set_footer(text=footer or f"{FAMILY} • Family Manager")
    return e

def staff_only():
    async def pred(i):
        if await i.client.is_staff(i.user):
            return True
        raise app_commands.CheckFailure("🔒 Эта команда только для руководства.")
    return app_commands.check(pred)

def staff_or_permission(permission):
    """Доступ руководству или роли, которой выдали конкретное право через панель."""
    async def pred(i):
        if await i.client.is_staff(i.user) or await i.client.has_role_permission(i.user, permission):
            return True
        raise app_commands.CheckFailure("🔒 У вашей роли нет разрешения на это действие.")
    return app_commands.check(pred)
