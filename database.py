import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild_id,key));
CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, category TEXT,
  user_id INTEGER, title TEXT, description TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS applications(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER,
  data TEXT, status TEXT, created INTEGER, message_id INTEGER, reviewer_id INTEGER);
CREATE TABLE IF NOT EXISTS afk(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER,
  reason TEXT, start_ts INTEGER, end_ts INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS vehicles(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, plate TEXT,
  status TEXT DEFAULT 'available', holder_id INTEGER);
CREATE TABLE IF NOT EXISTS vehicle_history(id INTEGER PRIMARY KEY AUTOINCREMENT, vehicle_id INTEGER,
  user_id INTEGER, action TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS contracts(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, title TEXT, client TEXT,
  executor_id INTEGER, creator_id INTEGER, amount REAL, paid REAL DEFAULT 0, status TEXT DEFAULT 'open',
  deadline INTEGER, created INTEGER, message_id INTEGER, overdue_reminded_at INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id INTEGER, user_id INTEGER,
  amount REAL, note TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS contract_submissions(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
  contract_id INTEGER NOT NULL, user_id INTEGER NOT NULL, amount REAL NOT NULL, thread_id INTEGER,
  screenshot_url TEXT, screenshot_message_id INTEGER, review_message_id INTEGER, status TEXT NOT NULL DEFAULT 'waiting_screenshot',
  reviewer_id INTEGER, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS members(guild_id INTEGER, user_id INTEGER, rank INTEGER, static_id TEXT, nick TEXT, joined INTEGER,
  PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS treasury(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER,
  amount REAL, note TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS common_fund_transactions(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL, direction TEXT NOT NULL CHECK(direction IN ('in','out')), amount REAL NOT NULL,
  note TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS media(guild_id INTEGER, kind TEXT, ref_id INTEGER, filename TEXT, PRIMARY KEY(guild_id,kind,ref_id));
CREATE TABLE IF NOT EXISTS last_seen(guild_id INTEGER, user_id INTEGER, ts INTEGER, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS activity(guild_id INTEGER, user_id INTEGER, day TEXT, messages INTEGER DEFAULT 0,
  voice_sec INTEGER DEFAULT 0, PRIMARY KEY(guild_id,user_id,day));
CREATE TABLE IF NOT EXISTS prepaid(guild_id INTEGER, user_id INTEGER, balance REAL DEFAULT 0, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS prepaid_fines(guild_id INTEGER, user_id INTEGER, balance REAL DEFAULT 0, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS fines(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
  amount REAL NOT NULL, reason TEXT NOT NULL, issuer_id INTEGER NOT NULL, ts INTEGER NOT NULL, paid REAL DEFAULT 0, cancelled INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS fine_payments(id INTEGER PRIMARY KEY AUTOINCREMENT, fine_id INTEGER NOT NULL, user_id INTEGER NOT NULL, amount REAL NOT NULL, note TEXT, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS treasury_submissions(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
  kind TEXT NOT NULL, target_id INTEGER NOT NULL, user_id INTEGER NOT NULL, amount REAL NOT NULL, note TEXT,
  thread_id INTEGER, screenshot_url TEXT, screenshot_message_id INTEGER, review_message_id INTEGER,
  status TEXT NOT NULL DEFAULT 'waiting_screenshot', reviewer_id INTEGER, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS temp_voices(channel_id INTEGER PRIMARY KEY, guild_id INTEGER, owner_id INTEGER,
  created INTEGER, empty_since INTEGER);
"""

MIGRATIONS = [
    "ALTER TABLE vehicles ADD COLUMN message_id INTEGER",
    "ALTER TABLE contracts ADD COLUMN executors TEXT",
    "ALTER TABLE contracts ADD COLUMN percent REAL DEFAULT 0",
    "ALTER TABLE contracts ADD COLUMN extra REAL DEFAULT 0",
    "ALTER TABLE contracts ADD COLUMN fees TEXT DEFAULT ''",
    "ALTER TABLE contracts ADD COLUMN funded INTEGER DEFAULT 1",
    "ALTER TABLE contracts ADD COLUMN overdue_reminded_at INTEGER DEFAULT 0",
    "ALTER TABLE afk ADD COLUMN message_id INTEGER",
    "ALTER TABLE afk ADD COLUMN auto INTEGER DEFAULT 0",
    "ALTER TABLE afk ADD COLUMN old_nick TEXT",
    "ALTER TABLE vehicles ADD COLUMN until_ts INTEGER",
    "ALTER TABLE fines ADD COLUMN paid REAL DEFAULT 0",
]

class DB:
    def __init__(self, path):
        self.path, self.conn = path, None

    async def connect(self):
        import logging, os
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        logging.info("SQLite database: %s", os.path.abspath(self.path))
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

        # Apply migrations based on the actual columns in the selected database.
        # This also repairs databases that were created by older bot versions.
        migrations = {
            "vehicles": [("message_id", "INTEGER"), ("until_ts", "INTEGER")],
            "contracts": [
                ("executors", "TEXT"), ("percent", "REAL DEFAULT 0"),
                ("extra", "REAL DEFAULT 0"), ("fees", "TEXT DEFAULT ''"),
                ("funded", "INTEGER DEFAULT 1"), ("overdue_reminded_at", "INTEGER DEFAULT 0"),
            ],
            "afk": [("message_id", "INTEGER"), ("auto", "INTEGER DEFAULT 0"), ("old_nick", "TEXT")],
            "fines": [("paid", "REAL DEFAULT 0"), ("cancelled", "INTEGER DEFAULT 0")],
        }
        for table, columns in migrations.items():
            # SCHEMA should create these tables, but ensure them explicitly in case
            # an older/incomplete database file was selected.
            if table == "fines":
                await self.conn.execute("""CREATE TABLE IF NOT EXISTS fines(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL, amount REAL NOT NULL, reason TEXT NOT NULL,
                    issuer_id INTEGER NOT NULL, ts INTEGER NOT NULL, paid REAL DEFAULT 0, cancelled INTEGER DEFAULT 0)""")
            elif table == "contracts":
                await self.conn.execute("""CREATE TABLE IF NOT EXISTS contracts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, title TEXT, client TEXT,
                    executor_id INTEGER, creator_id INTEGER, amount REAL, paid REAL DEFAULT 0,
                    status TEXT DEFAULT 'open', deadline INTEGER, created INTEGER, message_id INTEGER,
                    executors TEXT, percent REAL DEFAULT 0, extra REAL DEFAULT 0,
                    fees TEXT DEFAULT '', funded INTEGER DEFAULT 1, overdue_reminded_at INTEGER DEFAULT 0)""")
            existing = {row[1] for row in await (await self.conn.execute(f"PRAGMA table_info({table})")).fetchall()}
            for column, definition in columns:
                if column not in existing:
                    try:
                        await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                        logging.info("DB migration: added %s.%s", table, column)
                    except aiosqlite.OperationalError:
                        logging.exception("DB migration failed for %s.%s", table, column)
        # Explicitly ensure financial tables used by the current cogs exist.
        await self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS fine_payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, fine_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, amount REAL NOT NULL, note TEXT, ts INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS contract_submissions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
            contract_id INTEGER NOT NULL, user_id INTEGER NOT NULL, amount REAL NOT NULL,
            thread_id INTEGER, screenshot_url TEXT, screenshot_message_id INTEGER,
            review_message_id INTEGER, status TEXT NOT NULL DEFAULT 'waiting_screenshot',
            reviewer_id INTEGER, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS treasury_submissions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, kind TEXT NOT NULL,
            target_id INTEGER NOT NULL, user_id INTEGER NOT NULL, amount REAL NOT NULL, note TEXT,
            thread_id INTEGER, screenshot_url TEXT, screenshot_message_id INTEGER,
            review_message_id INTEGER, status TEXT NOT NULL DEFAULT 'waiting_screenshot',
            reviewer_id INTEGER, created INTEGER NOT NULL);
        """)
        await self.conn.commit()

    async def execute(self, sql, params=()):
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid

    async def fetchall(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def fetchone(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def get_setting(self, guild_id, key):
        r = await self.fetchone("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
        return r["value"] if r else None

    async def set_setting(self, guild_id, key, value):
        await self.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
                           (guild_id, key, str(value)))
