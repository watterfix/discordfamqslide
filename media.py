"""Хранение фото на диске (ссылки Discord со временем протухают, поэтому файлы храним сами)."""
import os, time, discord
from pathlib import Path

MEDIA = Path(os.getenv("MEDIA_DIR", "media"))
EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_SIZE = 8 * 1024 * 1024

def fk(files):
    """kwargs для send(): передаём files только если они есть."""
    return {"files": files} if files else {}

async def remove(db, guild_id, kind, ref):
    row = await db.fetchone("SELECT filename FROM media WHERE guild_id=? AND kind=? AND ref_id=?", (guild_id, kind, ref))
    if not row:
        return False
    (MEDIA / str(guild_id) / row["filename"]).unlink(missing_ok=True)
    await db.execute("DELETE FROM media WHERE guild_id=? AND kind=? AND ref_id=?", (guild_id, kind, ref))
    return True

async def save(db, guild_id, kind, ref, att):
    ext = Path(att.filename).suffix.lower()
    if ext not in EXT or not (att.content_type or "").startswith("image/"):
        raise ValueError("Нужна картинка: png, jpg, gif или webp.")
    if att.size > MAX_SIZE:
        raise ValueError("Файл больше 8 МБ.")
    folder = MEDIA / str(guild_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{kind}_{ref}_{int(time.time())}{ext}"
    await att.save(folder / name)
    await remove(db, guild_id, kind, ref)          # убираем старое фото
    await db.execute("INSERT INTO media(guild_id,kind,ref_id,filename) VALUES(?,?,?,?)", (guild_id, kind, ref, name))
    return name

async def attach(db, guild_id, kind, ref, embed, thumb=False):
    """Вешает фото на embed и возвращает список файлов для отправки."""
    row = await db.fetchone("SELECT filename FROM media WHERE guild_id=? AND kind=? AND ref_id=?", (guild_id, kind, ref))
    if not row:
        return []
    path = MEDIA / str(guild_id) / row["filename"]
    if not path.exists():
        return []
    f = discord.File(path, filename=row["filename"])
    (embed.set_thumbnail if thumb else embed.set_image)(url=f"attachment://{f.filename}")
    return [f]

async def filename(db, guild_id, kind, ref):
    """Имя файла фото (если оно есть на диске), чтобы ссылаться на уже загруженное вложение."""
    row = await db.fetchone("SELECT filename FROM media WHERE guild_id=? AND kind=? AND ref_id=?", (guild_id, kind, ref))
    return row["filename"] if row and (MEDIA / str(guild_id) / row["filename"]).exists() else None
