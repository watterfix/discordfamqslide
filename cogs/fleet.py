import time, logging, discord
from discord import app_commands
from discord.ext import commands, tasks
import media
from utils import staff_only, staff_or_permission, make, COLOR

ST = {"available": "🟢 Свободна", "in_use": "🟡 Занята", "repair": "🔧 В ремонте"}
DEFAULT_HOURS, MIN_HOURS, MAX_HOURS = 2.0, 0.5, 24.0   # срок аренды машины по умолчанию / минимум / максимум (часы)
CARD = {"available": ("Свободен", COLOR["ok"]), "in_use": ("Занят", COLOR["warn"]), "repair": ("В ремонте", COLOR["bad"])}


def fmt_hours(h):
    return f"{h:g} ч."


class TakeModal(discord.ui.Modal, title="Взять машину"):
    hours = discord.ui.TextInput(label=f"На сколько часов ({MIN_HOURS:g}–{MAX_HOURS:g})", default=f"{DEFAULT_HOURS:g}", max_length=5)

    def __init__(self, bot, car_id):
        super().__init__()
        self.bot, self.car_id = bot, car_id

    async def on_submit(self, i):
        try:
            h = float(self.hours.value.replace(",", "."))
            assert MIN_HOURS <= h <= MAX_HOURS
        except (ValueError, AssertionError):
            return await i.response.send_message(f"Укажите срок числом от {MIN_HOURS:g} до {MAX_HOURS:g} часов.", ephemeral=True)
        cog = self.bot.get_cog("Fleet")
        car = await self.bot.db.fetchone("SELECT * FROM vehicles WHERE id=? AND guild_id=?", (self.car_id, i.guild_id))
        if not car:
            return await i.response.send_message("Машина не найдена.", ephemeral=True)
        err = await cog.do_take(i.guild, i.user, car, h)
        if err:
            return await i.response.send_message(err, ephemeral=True)
        await cog.refresh_card(i.guild, car["id"])
        until = int(time.time() + h * 3600)
        await i.response.send_message(f"🔑 Вы взяли **{car['name']}** `{car['plate']}` на **{fmt_hours(h)}**\n"
                                      f"Она вернётся в автопарк <t:{until}:R> (<t:{until}:t>). Сдать раньше можно кнопкой «Сдать».", ephemeral=True)


class CarView(discord.ui.View):
    """Кнопки под карточкой машины (работают и после перезапуска бота)."""
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _click(self, i, take):
        cog = self.bot.get_cog("Fleet")
        car = await self.bot.db.fetchone("SELECT * FROM vehicles WHERE message_id=? AND guild_id=?", (i.message.id, i.guild_id))
        if not car:
            return await i.response.send_message("Эта карточка устарела: машины нет в автопарке.", ephemeral=True)
        if take:   # сначала спрашиваем срок
            err = cog.take_error(car)
            if err:
                return await i.response.send_message(err, ephemeral=True)
            return await i.response.send_modal(TakeModal(self.bot, car["id"]))
        err = await cog.do_return(i.guild, i.user, car)
        if err:
            return await i.response.send_message(err, ephemeral=True)
        car = await self.bot.db.fetchone("SELECT * FROM vehicles WHERE id=?", (car["id"],))
        e, _ = await cog.build(i.guild, car)
        await i.response.edit_message(embed=e)

    @discord.ui.button(label="Взять", style=discord.ButtonStyle.success, custom_id="car:take")
    async def take(self, i, b): await self._click(i, True)

    @discord.ui.button(label="Сдать", style=discord.ButtonStyle.danger, custom_id="car:return")
    async def ret(self, i, b): await self._click(i, False)


class Fleet(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.expire.start()

    async def cog_load(self):
        self.bot.add_view(CarView(self.bot))

    def cog_unload(self):
        self.expire.cancel()

    # ---------- автовозврат по истечении времени ----------
    @tasks.loop(seconds=15)
    async def expire(self):
        # Never let a single database/Discord error kill the expiry loop.
        try:
            now = int(time.time())
            db = self.bot.db
            expired = await db.fetchall(
                "SELECT * FROM vehicles WHERE status='in_use' AND until_ts IS NOT NULL AND until_ts<=?",
                (now,),
            )
        except Exception:
            logging.exception("Не удалось проверить истёкшие сроки автопарка; повторим через 15 секунд")
            return

        for c in expired:
            try:
                # Conditional update prevents duplicate processing if two loop runs overlap.
                await db.execute(
                    "UPDATE vehicles SET status='available', holder_id=NULL, until_ts=NULL "
                    "WHERE id=? AND status='in_use' AND until_ts IS NOT NULL AND until_ts<=?",
                    (c["id"], now),
                )
                updated = await db.fetchone("SELECT * FROM vehicles WHERE id=?", (c["id"],))
                if not updated or updated["status"] != "available" or updated["holder_id"] is not None:
                    continue
                try:
                    await self._hist(c["id"], c["holder_id"], "авто-возврат: время вышло")
                except Exception:
                    logging.exception("Не удалось записать историю авто-возврата машины #%s", c["id"])

                g = self.bot.get_guild(c["guild_id"])
                if not g:
                    logging.warning("Машина #%s истекла, но сервер %s не найден в кэше", c["id"], c["guild_id"])
                    continue
                member = g.get_member(c["holder_id"])
                try:
                    await self.bot.log(g, "fleet", "Машина вернулась в автопарк (время вышло)",
                                       f"#{c['id']} {c['name']} [{c['plate']}] · <@{c['holder_id']}>", member, 0x57F287)
                except Exception:
                    logging.exception("Не удалось записать лог авто-возврата машины #%s", c["id"])
                try:
                    await self.refresh_card(g, c["id"])
                except Exception:
                    logging.exception("Не удалось обновить карточку машины #%s после авто-возврата", c["id"])
                if member:
                    try:
                        await member.send(f"⏰ Время аренды вышло: **{c['name']}** `{c['plate']}` возвращена в автопарк на **{g.name}**.")
                    except discord.HTTPException:
                        pass
            except Exception:
                logging.exception("Автовозврат машины #%s", c["id"])

    @expire.before_loop
    async def _w(self): await self.bot.wait_until_ready()

    # ---------- вспомогательное ----------
    async def _car(self, i, cid):
        c = await self.bot.db.fetchone("SELECT * FROM vehicles WHERE id=? AND guild_id=?", (cid, i.guild_id))
        if not c: await i.response.send_message("Машина не найдена.", ephemeral=True)
        return c

    async def _hist(self, cid, uid, action):
        await self.bot.db.execute("INSERT INTO vehicle_history(vehicle_id,user_id,action,ts) VALUES(?,?,?,?)", (cid, uid, action, int(time.time())))

    # ---------- карточки в канале автопарка ----------
    def card(self, guild, c):
        label, color = CARD[c["status"]]
        desc, text = f"Номер: `{c['plate']}`", label
        if c["holder_id"]:
            desc += f"\nВзял: <@{c['holder_id']}>"
            if c["until_ts"]:
                desc += f"\n⏰ Вернётся в автопарк: <t:{c['until_ts']}:R> (<t:{c['until_ts']}:t>)"
            m = guild.get_member(c["holder_id"])
            if m: text += f" · {m.display_name}"
        e = discord.Embed(title=c["name"], description=desc, color=color, timestamp=discord.utils.utcnow())
        e.set_footer(text=text, icon_url=guild.icon.url if guild.icon else None)
        return e

    async def build(self, guild, c, upload=False):
        e, files = self.card(guild, c), []
        if upload:
            files = await media.attach(self.bot.db, guild.id, "car", c["id"], e, thumb=True)
        else:
            name = await media.filename(self.bot.db, guild.id, "car", c["id"])
            if name: e.set_thumbnail(url=f"attachment://{name}")
        return e, files

    async def post_card(self, guild, car_id):
        ch = await self.bot.get_ch(guild, "fleet_channel")
        c = await self.bot.db.fetchone("SELECT * FROM vehicles WHERE id=?", (car_id,))
        if not ch or not c: return None
        e, files = await self.build(guild, c, upload=True)
        msg = await ch.send(embed=e, view=CarView(self.bot), **media.fk(files))
        await self.bot.db.execute("UPDATE vehicles SET message_id=? WHERE id=?", (msg.id, car_id))
        return msg

    async def _message(self, guild, c):
        ch = await self.bot.get_ch(guild, "fleet_channel")
        if not ch or not c["message_id"]: return None
        try: return await ch.fetch_message(c["message_id"])
        except discord.HTTPException: return None

    async def refresh_card(self, guild, car_id, upload=False):
        c = await self.bot.db.fetchone("SELECT * FROM vehicles WHERE id=?", (car_id,))
        msg = await self._message(guild, c) if c else None
        if not msg: return
        e, files = await self.build(guild, c, upload)
        await msg.edit(embed=e, **({"attachments": files} if upload else {}))

    async def drop_card(self, guild, c):
        msg = await self._message(guild, c)
        if msg:
            try: await msg.delete()
            except discord.HTTPException: pass

    # ---------- логика взять / сдать (общая для кнопок и команд) ----------
    def take_error(self, c):
        if c["status"] == "repair": return "🔧 Машина в ремонте."
        if c["status"] == "in_use": return f"🟡 Машина уже у <@{c['holder_id']}>."

    async def do_take(self, guild, user, c, hours=DEFAULT_HOURS):
        """Берёт машину на время; по истечении она сама вернётся в автопарк."""
        err = self.take_error(c)
        if err: return err
        until = int(time.time() + hours * 3600)
        await self.bot.db.execute("UPDATE vehicles SET status='in_use', holder_id=?, until_ts=? WHERE id=?", (user.id, until, c["id"]))
        await self._hist(c["id"], user.id, f"взята на {fmt_hours(hours)}")
        await self.bot.log(guild, "fleet", "Машина взята", f"#{c['id']} {c['name']} [{c['plate']}] · на {fmt_hours(hours)}, вернётся <t:{until}:t>", user, 0xFEE75C)

    async def do_return(self, guild, user, c):
        if c["status"] == "repair": return "🔧 Машина в ремонте."
        if c["status"] != "in_use": return "Машина и так свободна."
        if c["holder_id"] != user.id and not await self.bot.is_staff(user) and not await self.bot.has_role_permission(user, "fleet_return_any"):
            return f"Сдать машину может только <@{c['holder_id']}>, руководство или участник с правом «Возврат чужой машины»."
        await self.bot.db.execute("UPDATE vehicles SET status='available', holder_id=NULL, until_ts=NULL WHERE id=?", (c["id"],))
        await self._hist(c["id"], user.id, "возвращена")
        await self.bot.log(guild, "fleet", "Машина возвращена", f"#{c['id']} {c['name']} [{c['plate']}]", user, 0x57F287)

    async def take_away(self, guild, staff, c):
        """Руководство забирает машину у того, кто не сдал. Возвращает (ошибка, дошло_ли_ЛС)."""
        if c["status"] != "in_use": return "Машина уже свободна.", False
        holder = c["holder_id"]
        await self.bot.db.execute("UPDATE vehicles SET status='available', holder_id=NULL, until_ts=NULL WHERE id=?", (c["id"],))
        await self._hist(c["id"], staff.id, f"изъята у <@{holder}>")
        await self.bot.log(guild, "fleet", "Машина изъята руководством", f"#{c['id']} {c['name']} [{c['plate']}] · у <@{holder}> · {staff.mention}", staff, 0xED4245)
        await self.refresh_card(guild, c["id"])
        member = guild.get_member(holder)
        if not member: return None, False
        try:
            await member.send(f"🚗 Руководство ({staff.display_name}) забрало у вас **{c['name']}** `{c['plate']}` и вернуло в автопарк **{guild.name}**.")
            return None, True
        except discord.HTTPException:
            return None, False

    # ---------- команды ----------
    @app_commands.command(name="машина_добавить", description="Добавить машину (карточка появится в канале автопарка)")
    @app_commands.rename(name="название", plate="номер", photo="фото")
    @app_commands.describe(name="Название машины", plate="Номерной знак", photo="Фото машины (необязательно)")
    @staff_or_permission("fleet_manage")
    async def add(self, i, name: str, plate: str, photo: discord.Attachment = None):
        await i.response.defer(ephemeral=True)
        cid = await self.bot.db.execute("INSERT INTO vehicles(guild_id,name,plate) VALUES(?,?,?)", (i.guild_id, name, plate.upper()))
        await self._hist(cid, i.user.id, "добавлена")
        await self.bot.log(i.guild, "fleet", "Машина добавлена", f"#{cid} {name} [{plate.upper()}]", i.user, 0x57F287)
        warn = ""
        if photo:
            try: await media.save(self.bot.db, i.guild_id, "car", cid, photo)
            except ValueError as ex: warn = f"\n⚠️ Фото не добавлено: {ex}"
        msg = await self.post_card(i.guild, cid)
        tip = f"Карточка: {msg.jump_url}" if msg else "⚠️ Канал автопарка не задан: `/настроить_канал` → Автопарк, затем `/автопарк_пересоздать`."
        await i.followup.send(f"✅ Машина **#{cid} {name}** добавлена.\n{tip}{warn}", ephemeral=True)

    @app_commands.command(name="машина_списать", description="Списать машину")
    @app_commands.rename(car_id="машина")
    @app_commands.describe(car_id="Номер машины")
    @staff_or_permission("fleet_manage")
    async def remove(self, i, car_id: int):
        c = await self._car(i, car_id)
        if not c: return
        await self.drop_card(i.guild, c)
        await self.bot.db.execute("DELETE FROM vehicles WHERE id=?", (car_id,))
        await media.remove(self.bot.db, i.guild_id, "car", car_id)
        await self.bot.log(i.guild, "fleet", "Машина списана", f"#{car_id} {c['name']} [{c['plate']}]", i.user, 0xED4245)
        await i.response.send_message("🗑 Списана.", ephemeral=True)

    @app_commands.command(name="машина_взять", description="Взять машину на время (потом она вернётся сама)")
    @app_commands.rename(car_id="машина", hours="часы")
    @app_commands.describe(car_id="Номер машины", hours=f"На сколько часов ({MIN_HOURS:g}–{MAX_HOURS:g}, по умолчанию {DEFAULT_HOURS:g})")
    async def take(self, i, car_id: int, hours: app_commands.Range[float, MIN_HOURS, MAX_HOURS] = DEFAULT_HOURS):
        c = await self._car(i, car_id)
        if not c: return
        err = await self.do_take(i.guild, i.user, c, hours)
        if err: return await i.response.send_message(err, ephemeral=True)
        await self.refresh_card(i.guild, car_id)
        until = int(time.time() + hours * 3600)
        await i.response.send_message(f"🔑 Вы взяли **{c['name']}** `{c['plate']}` на **{fmt_hours(hours)}**\nВернётся в автопарк <t:{until}:R>.", ephemeral=True)

    @app_commands.command(name="машина_вернуть", description="Вернуть машину")
    @app_commands.rename(car_id="машина")
    @app_commands.describe(car_id="Номер машины")
    async def ret(self, i, car_id: int):
        c = await self._car(i, car_id)
        if not c: return
        err = await self.do_return(i.guild, i.user, c)
        if err: return await i.response.send_message(err, ephemeral=True)
        await self.refresh_card(i.guild, car_id)
        await i.response.send_message(f"✅ **{c['name']}** возвращена.", ephemeral=True)

    @app_commands.command(name="машина_статус", description="Сменить статус (ремонт/свободна)")
    @app_commands.rename(car_id="машина", status="статус")
    @app_commands.describe(car_id="Номер машины", status="Новый статус машины")
    @app_commands.choices(status=[app_commands.Choice(name=v, value=k) for k, v in ST.items() if k != "in_use"])
    @staff_or_permission("fleet_manage")
    async def status(self, i, car_id: int, status: app_commands.Choice[str]):
        c = await self._car(i, car_id)
        if not c: return
        await self.bot.db.execute("UPDATE vehicles SET status=?, holder_id=NULL, until_ts=NULL WHERE id=?", (status.value, car_id))
        await self._hist(car_id, i.user.id, f"статус: {status.value}")
        await self.bot.log(i.guild, "fleet", "Статус машины изменён", f"#{car_id} {c['name']} → {status.name}", i.user)
        await self.refresh_card(i.guild, car_id)
        await i.response.send_message(f"Статус #{car_id}: {status.name}", ephemeral=True)

    @app_commands.command(name="автопарк_пересоздать", description="Пересоздать все карточки автопарка в канале")
    @staff_or_permission("fleet_manage")
    async def rebuild(self, i):
        await i.response.defer(ephemeral=True)
        ch = await self.bot.get_ch(i.guild, "fleet_channel")
        if not ch:
            return await i.followup.send("Сначала задайте канал: `/настроить_канал` → Автопарк.", ephemeral=True)
        rows = await self.bot.db.fetchall("SELECT * FROM vehicles WHERE guild_id=? ORDER BY id", (i.guild_id,))
        for r in rows:
            await self.drop_card(i.guild, r)
            await self.post_card(i.guild, r["id"])
        await self.bot.log(i.guild, "fleet", "Карточки автопарка пересозданы", f"{len(rows)} шт. → {ch.mention}", i.user)
        await i.followup.send(f"✅ Карточек создано: {len(rows)} → {ch.mention}", ephemeral=True)

    @app_commands.command(name="автопарк", description="Список автопарка")
    async def lst(self, i):
        await i.response.send_message(embed=await self.list_embed(i.guild_id))

    async def list_embed(self, gid):
        rows = await self.bot.db.fetchall("SELECT * FROM vehicles WHERE guild_id=? ORDER BY id", (gid,))
        if not rows:
            return make("🚗 Автопарк", "*Пока пусто. Добавьте машину: `/машина_добавить`*")
        groups = {k: [] for k in ST}
        for r in rows: groups[r["status"]].append(r)
        cnt = "  ┃  ".join(f"{ST[k].split()[0]} {len(v)}" for k, v in groups.items())
        e = make("🚗 Автопарк", f"Всего: **{len(rows)}**  ┃  {cnt}", COLOR["main"])
        for k, v in groups.items():
            if v:
                lines = [f"`#{r['id']}` **{r['name']}** · `{r['plate']}`" + (f" → <@{r['holder_id']}>" if r["holder_id"] else "") + (f" · вернётся <t:{r['until_ts']}:R>" if r["holder_id"] and r["until_ts"] else "") for r in v]
                e.add_field(name=ST[k], value="\n".join(lines)[:1024], inline=False)
        return e

    @app_commands.command(name="машина_инфо", description="Карточка машины с фото")
    @app_commands.rename(car_id="машина")
    @app_commands.describe(car_id="Номер машины")
    async def info(self, i, car_id: int):
        c = await self._car(i, car_id)
        if not c: return
        e = make(f"🚗 {c['name']}", f"`{c['plate']}`  ┃  {ST[c['status']]}", COLOR["main"])
        if c["holder_id"]: e.add_field(name="🔑 Сейчас у", value=f"<@{c['holder_id']}>" + (f"\n⏰ вернётся <t:{c['until_ts']}:R>" if c["until_ts"] else ""), inline=False)
        hist = await self.bot.db.fetchall("SELECT * FROM vehicle_history WHERE vehicle_id=? ORDER BY id DESC LIMIT 3", (car_id,))
        if hist:
            e.add_field(name="🕘 Последние действия", value="\n".join(f"<t:{h['ts']}:R> <@{h['user_id']}> — {h['action']}" for h in hist), inline=False)
        files = await media.attach(self.bot.db, i.guild_id, "car", car_id, e)
        await i.response.send_message(embed=e, **media.fk(files))

    @app_commands.command(name="машина_история", description="История использования машины")
    @app_commands.rename(car_id="машина")
    @app_commands.describe(car_id="Номер машины")
    @staff_or_permission("fleet_manage")
    async def hist(self, i, car_id: int):
        rows = await self.bot.db.fetchall("SELECT * FROM vehicle_history WHERE vehicle_id=? ORDER BY id DESC LIMIT 15", (car_id,))
        text = "\n".join(f"<t:{r['ts']}:f> <@{r['user_id']}> — {r['action']}" for r in rows) or "Пусто."
        await i.response.send_message(text, ephemeral=True)

async def setup(bot): await bot.add_cog(Fleet(bot))
