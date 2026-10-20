import time, discord
import media
from cogs.contracts import execs, left
from discord import app_commands
from discord.ext import commands
from utils import staff_only, staff_or_permission, money, make, COLOR, LINE, rank_icon

class Family(commands.Cog):
    def __init__(self, bot): self.bot = bot

    # ---------- Состав и ранги ----------
    @app_commands.command(name="участник_внести", description="Внести/обновить участника семьи (ранг, Static ID)")
    @app_commands.rename(member="участник", rank="ранг", static_id="статик", nick="ник")
    @app_commands.describe(member="Участник семьи", rank="Ранг от 1 до 10", static_id="Static ID", nick="Имя Фамилия")
    @staff_or_permission("family_manage")
    async def member_set(self, i, member: discord.Member, rank: app_commands.Range[int, 1, 10], static_id: str, nick: str):
        db = self.bot.db
        old = await db.fetchone("SELECT rank FROM members WHERE guild_id=? AND user_id=?", (i.guild_id, member.id))
        await db.execute("INSERT INTO members VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET "
                         "rank=excluded.rank, static_id=excluded.static_id, nick=excluded.nick",
                         (i.guild_id, member.id, rank, static_id, nick, int(time.time())))
        if old and old["rank"] != rank:
            kind = "Повышение" if rank > old["rank"] else "Понижение"
            await self.bot.log(i.guild, "family", f"{kind}: {old['rank']} → {rank}", f"{member.mention} ({nick} | {static_id})", i.user, 0x5865F2)
        else:
            await self.bot.log(i.guild, "family", "Участник семьи обновлён", f"{member.mention} · ранг {rank} · {nick} | {static_id}", i.user)
        await i.response.send_message(f"✅ {member.mention}: ранг **{rank}**, {nick} | {static_id}")

    @app_commands.command(name="участник_убрать", description="Убрать участника из состава (кик из семьи)")
    @app_commands.rename(member="участник", reason="причина")
    @app_commands.describe(member="Участник", reason="Причина исключения")
    @staff_or_permission("family_manage")
    async def member_remove(self, i, member: discord.Member, reason: str):
        await self.bot.db.execute("DELETE FROM members WHERE guild_id=? AND user_id=?", (i.guild_id, member.id))
        await self.bot.log(i.guild, "family", "Исключён из семьи", f"{member.mention}: {reason}", i.user, 0xED4245)
        await i.response.send_message("Участник удалён из состава.")

    @app_commands.command(name="участник", description="Карточка участника: ранг, активность, долги")
    @app_commands.rename(member="участник")
    @app_commands.describe(member="Участник")
    async def member_info(self, i, member: discord.Member = None):
        res = await self.member_embed(i.guild, member or i.user)
        if not res: return await i.response.send_message("Участник не внесён в состав.", ephemeral=True)
        e, files = res
        await i.response.send_message(embed=e, **media.fk(files))

    async def member_embed(self, guild, member):
        db = self.bot.db
        m = await db.fetchone("SELECT * FROM members WHERE guild_id=? AND user_id=?", (guild.id, member.id))
        if not m: return None
        open_c = await db.fetchall("SELECT * FROM contracts WHERE guild_id=? AND status='open'", (guild.id,))
        debt = {"d": sum(left(c) / len(execs(c)) for c in open_c if member.id in execs(c))}
        cars = await db.fetchall("SELECT name, plate FROM vehicles WHERE guild_id=? AND holder_id=?", (guild.id, member.id))
        afk = await db.fetchone("SELECT end_ts FROM afk WHERE guild_id=? AND user_id=? AND status='active'", (guild.id, member.id))
        e = make(f"{rank_icon(m['rank'])} {m['nick']}", f"`{m['static_id']}` · {member.mention}", COLOR["main"])
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="🎖 Ранг", value=f"**{m['rank']}** / 10")
        e.add_field(name="📅 В семье с", value=f"<t:{m['joined']}:d>")
        e.add_field(name="💸 Долг", value=f"**{money(debt['d'])}**" if debt["d"] > 0 else "нет ✅")
        e.add_field(name="🚗 Машины на руках", value=", ".join(f"{c['name']} `{c['plate']}`" for c in cars) or "—", inline=False)
        if afk: e.add_field(name="💤 АФК", value=f"вернётся <t:{afk['end_ts']}:R>", inline=False)
        files = await media.attach(self.bot.db, guild.id, "member", member.id, e)
        return e, files

    @app_commands.command(name="состав", description="Состав семьи по рангам")
    async def family_list(self, i):
        await i.response.send_message(embed=await self.family_embed(i.guild))

    async def family_embed(self, guild):
        rows = await self.bot.db.fetchall("SELECT * FROM members WHERE guild_id=? ORDER BY rank DESC, nick", (guild.id,))
        if not rows: return make("👥 Состав семьи", "Состав пуст. Добавьте через `/участник_внести`.", COLOR["main"])
        groups = {}
        for r in rows: groups.setdefault(r["rank"], []).append(r)
        e = make(f"👥 Состав семьи · {len(rows)} чел.", None, COLOR["main"])
        if guild.icon: e.set_thumbnail(url=guild.icon.url)
        for rank in sorted(groups, reverse=True):
            lines = [f"<@{r['user_id']}> · {r['nick']} `{r['static_id']}`" for r in groups[rank]]
            e.add_field(name=f"{rank_icon(rank)} Ранг {rank} · {len(lines)}", value="\n".join(lines)[:1024], inline=False)
        return e

    # ---------- Общак ----------
    async def balance(self, gid):
        r = await self.bot.db.fetchone("SELECT COALESCE(SUM(amount),0) b FROM treasury WHERE guild_id=?", (gid,))
        return r["b"]

    @app_commands.command(name="общак_внести", description="Положить деньги в общак")
    @app_commands.rename(amount="сумма", note="заметка")
    @app_commands.describe(amount="Сколько внести", note="Комментарий")
    async def deposit(self, i, amount: app_commands.Range[float, 1.0, 1e12], note: str = ""):
        await i.response.send_message(embed=await self.do_deposit(i.guild, i.user, amount, note))

    async def do_deposit(self, guild, user, amount, note=""):
        await self.bot.db.execute("INSERT INTO treasury(guild_id,user_id,amount,note,ts) VALUES(?,?,?,?,?)", (guild.id, user.id, amount, note, int(time.time())))
        bal = await self.balance(guild.id)
        await self.bot.log(guild, "treasury", "Общак: пополнение", f"+{money(amount)} {note}\nБаланс: {money(bal)}", user, 0x57F287)
        return make("💰 Общак пополнен", f"{user.mention} внёс **+{money(amount)}**\nБаланс: **{money(bal)}**", COLOR["ok"])

    @app_commands.command(name="общак_выдать", description="Выдать из общака (персонал)")
    @app_commands.rename(amount="сумма", member="участник", reason="причина")
    @app_commands.describe(amount="Сколько выдать", member="Кому выдать", reason="Зачем выдали")
    @staff_or_permission("treasury_withdraw")
    async def withdraw(self, i, amount: app_commands.Range[float, 1.0, 1e12], member: discord.Member, reason: str):
        bal = await self.balance(i.guild_id)
        if amount > bal: return await i.response.send_message(f"В общаке только {money(bal)}.", ephemeral=True)
        await self.bot.db.execute("INSERT INTO treasury(guild_id,user_id,amount,note,ts) VALUES(?,?,?,?,?)",
                                  (i.guild_id, i.user.id, -amount, f"{member.id}: {reason}", int(time.time())))
        await self.bot.log(i.guild, "treasury", "Общак: выдача", f"−{money(amount)} → {member.mention}: {reason}\nБаланс: {money(bal - amount)}", i.user, 0xED4245)
        await i.response.send_message(embed=make("🏦 Выдача из общака", f"**−{money(amount)}** → {member.mention}\n*{reason}*\nБаланс: **{money(bal - amount)}**", COLOR["bad"]))

    @app_commands.command(name="общак", description="Баланс и последние операции общака")
    async def treasury(self, i):
        await i.response.send_message(embed=await self.treasury_embed(i.guild_id))

    async def treasury_embed(self, gid):
        rows = await self.bot.db.fetchall("SELECT * FROM treasury WHERE guild_id=? ORDER BY id DESC LIMIT 10", (gid,))
        def note(n):
            a, _, b = n.partition(": ")
            return f"→ <@{a}>: {b}" if a.isdigit() else n
        lines = [f"{'🟩 ➕' if r['amount'] > 0 else '🟥 ➖'} **{money(abs(r['amount']))}** · <@{r['user_id']}> · <t:{r['ts']}:d> {note(r['note'])}" for r in rows]
        bal = await self.balance(gid)
        return make("🏦 Общак семьи", f"# {money(bal)}\n{LINE}\n" + ("\n".join(lines) or "*Операций пока нет*"), COLOR["gold"])

async def setup(bot): await bot.add_cog(Family(bot))
