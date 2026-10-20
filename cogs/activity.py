import time, datetime as dt, discord
from discord import app_commands
from discord.ext import commands
from utils import staff_only, make, bar, spark, COLOR

UPSERT = ("INSERT INTO activity(guild_id,user_id,day,messages,voice_sec) VALUES(?,?,?,?,?) "
          "ON CONFLICT(guild_id,user_id,day) DO UPDATE SET messages=messages+excluded.messages, voice_sec=voice_sec+excluded.voice_sec")

def today(): return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
def since(days): return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
def hm(sec): return f"{sec // 3600}ч {sec % 3600 // 60}м"

class Activity(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.voice = {}

    async def seen(self, gid, uid):
        await self.bot.db.execute("INSERT INTO last_seen VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET ts=excluded.ts",
                                  (gid, uid, int(time.time())))

    @commands.Cog.listener()
    async def on_message(self, msg):
        if msg.guild and not msg.author.bot:
            await self.bot.db.execute(UPSERT, (msg.guild.id, msg.author.id, today(), 1, 0))
            await self.seen(msg.guild.id, msg.author.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, m, a, b):
        if m.bot: return
        key = (m.guild.id, m.id)
        if a.channel != b.channel:
            await self.seen(m.guild.id, m.id)
        if not a.channel and b.channel:
            self.voice[key] = time.time()
        elif a.channel and not b.channel and key in self.voice:
            sec = int(time.time() - self.voice.pop(key))
            await self.bot.db.execute(UPSERT, (m.guild.id, m.id, today(), 0, sec))

    @app_commands.command(name="активность", description="Активность участника")
    @app_commands.rename(member="участник", days="дни")
    @app_commands.describe(member="Участник", days="За сколько дней (1–90)")
    async def activity(self, i, member: discord.Member = None, days: app_commands.Range[int, 1, 90] = 7):
        await i.response.send_message(embed=await self.activity_embed(i.guild_id, member or i.user, days))

    async def activity_embed(self, gid, member, days=7):
        db = self.bot.db
        r = await db.fetchone("SELECT SUM(messages) m, SUM(voice_sec) v, COUNT(*) d FROM activity WHERE guild_id=? AND user_id=? AND day>=?",
                              (gid, member.id, since(days)))
        rows = await db.fetchall("SELECT day, messages, voice_sec FROM activity WHERE guild_id=? AND user_id=? AND day>=?", (gid, member.id, since(days)))
        per = {x["day"]: x["messages"] + x["voice_sec"] // 60 for x in rows}
        e = make(f"📊 Активность · {member.display_name}", f"За последние **{days}** дн.", COLOR["main"])
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="💬 Сообщений", value=f"**{r['m'] or 0}**")
        e.add_field(name="🎙 В голосе", value=f"**{hm(r['v'] or 0)}**")
        e.add_field(name="📅 Активных дней", value=f"**{r['d'] or 0}** / {days}")
        if days <= 30:
            now = dt.datetime.now(dt.timezone.utc)
            vals = [per.get((now - dt.timedelta(days=d)).strftime("%Y-%m-%d"), 0) for d in range(days - 1, -1, -1)]
            e.add_field(name="📈 По дням (старые → новые)", value=f"`{spark(vals)}`", inline=False)
        return e

    @app_commands.command(name="топ", description="Топ активных участников")
    @app_commands.rename(days="дни")
    @app_commands.describe(days="За сколько дней (1–90)")
    async def top(self, i, days: app_commands.Range[int, 1, 90] = 7):
        await i.response.send_message(embed=await self.top_embed(i.guild_id, days))

    async def top_embed(self, gid, days=7):
        rows = await self.bot.db.fetchall(
            "SELECT user_id, SUM(messages) m, SUM(voice_sec) v FROM activity WHERE guild_id=? AND day>=? "
            "GROUP BY user_id ORDER BY m + v/60 DESC LIMIT 10", (gid, since(days)))
        medals, lines = ["🥇", "🥈", "🥉"], []
        best = (rows[0]["m"] + rows[0]["v"] // 60) if rows else 1
        for n, r in enumerate(rows, 1):
            score = r["m"] + r["v"] // 60
            lines.append(f"{medals[n - 1] if n <= 3 else f'`{n}.`'} <@{r['user_id']}>\n┕ {bar(score, best, 10)} 💬 **{r['m']}** · 🎙 **{hm(r['v'])}**")
        return make(f"🏆 Топ активности · {days} дн.", "\n".join(lines) or "*Данных пока нет*", COLOR["gold"])

    @app_commands.command(name="неактивные", description="Участники с ролью без активности (персонал)")
    @app_commands.rename(role="роль", days="дни")
    @app_commands.describe(role="Роль", days="Сколько дней без активности")
    @staff_only()
    async def inactive(self, i, role: discord.Role, days: app_commands.Range[int, 1, 90] = 7):
        rows = await self.bot.db.fetchall("SELECT DISTINCT user_id FROM activity WHERE guild_id=? AND day>=?", (i.guild_id, since(days)))
        active = {r["user_id"] for r in rows}
        quiet = [m.mention for m in role.members if m.id not in active and not m.bot]
        await i.response.send_message(f"Неактивны {days} дн. ({len(quiet)}):\n" + (" ".join(quiet)[:1900] or "никого"), ephemeral=True)

async def setup(bot): await bot.add_cog(Activity(bot))
