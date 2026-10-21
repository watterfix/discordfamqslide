import time, discord
from discord import app_commands
from discord.ext import commands
from utils import staff_only, make, CAT, COLOR

CATS = list(CAT)

class Logs(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, m):
        await self.bot.log(m.guild, "server", "Участник зашёл", f"{m.mention}, аккаунт создан <t:{int(m.created_at.timestamp())}:R>", m, 0x57F287)

    @commands.Cog.listener()
    async def on_member_remove(self, m):
        await self.bot.log(m.guild, "server", "Участник вышел", m.mention, m, 0xED4245)

    @commands.Cog.listener()
    async def on_message_delete(self, msg):
        if msg.guild and not msg.author.bot:
            await self.bot.log(msg.guild, "messages", "Сообщение удалено", f"{msg.channel.mention}\n{msg.content[:1500] or '*вложение*'}", msg.author, 0xED4245)

    @commands.Cog.listener()
    async def on_message_edit(self, a, b):
        if a.guild and not a.author.bot and a.content != b.content:
            await self.bot.log(a.guild, "messages", "Сообщение изменено",
                               f"{a.channel.mention} [перейти]({b.jump_url})\n**Было:** {a.content[:900]}\n**Стало:** {b.content[:900]}", a.author, 0xFEE75C)

    @commands.Cog.listener()
    async def on_voice_state_update(self, m, a, b):
        if a.channel == b.channel:
            return
        if not a.channel:   t = f"зашёл в {b.channel.mention}"
        elif not b.channel: t = f"вышел из {a.channel.mention}"
        else:               t = f"{a.channel.mention} → {b.channel.mention}"
        await self.bot.log(m.guild, "voice", "Голосовой канал", f"{m.mention} {t}", m, 0x99AAB5)

    @commands.Cog.listener()
    async def on_member_update(self, a, b):
        if a.roles != b.roles:
            add = [r.mention for r in b.roles if r not in a.roles]
            rem = [r.mention for r in a.roles if r not in b.roles]
            await self.bot.log(b.guild, "server", "Изменены роли", f"{b.mention}\n+ {' '.join(add) or '—'}\n− {' '.join(rem) or '—'}", b, 0x5865F2)
        elif a.nick != b.nick:
            await self.bot.log(b.guild, "server", "Смена ника", f"{b.mention}: `{a.nick}` → `{b.nick}`", b, 0x5865F2)

    @app_commands.command(name="логи", description="Поиск по логам (персонал)")
    @app_commands.rename(member="участник", category="категория", limit="лимит")
    @app_commands.describe(member="Фильтр по участнику", category="Фильтр по функции", limit="Сколько записей (1–25)")
    @app_commands.choices(category=[app_commands.Choice(name=f"{CAT[c][0]} {CAT[c][1]}", value=c) for c in CATS])
    @staff_only()
    async def logs(self, i, member: discord.Member = None, category: app_commands.Choice[str] = None,
                   limit: app_commands.Range[int, 1, 25] = 10):
        q, p = "SELECT * FROM logs WHERE guild_id=?", [i.guild_id]
        if member: q += " AND user_id=?"; p.append(member.id)
        if category: q += " AND category=?"; p.append(category.value)
        rows = await self.bot.db.fetchall(q + " ORDER BY id DESC LIMIT ?", (*p, limit))
        if not rows:
            return await i.response.send_message("Ничего не найдено.", ephemeral=True)
        text = "\n".join(f"{CAT.get(r['category'], ('📌',))[0]} <t:{r['ts']}:R> **{r['title']}**\n┕ {(r['description'] or '—')[:90]}" for r in rows)
        await i.response.send_message(embed=make("🗂 Журнал событий", text[:4000], COLOR["main"]), ephemeral=True)

async def setup(bot): await bot.add_cog(Logs(bot))
