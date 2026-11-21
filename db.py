import os
import shutil
import sqlite3
import threading
import time
import json

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, first_name TEXT, username TEXT,
    joined_at INTEGER, last_seen INTEGER, banned INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS states(user_id INTEGER PRIMARY KEY, state TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS channels(
    chat_id INTEGER PRIMARY KEY, title TEXT, username TEXT, added_by INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS giveaways(
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, owner_id INTEGER,
    chat_id INTEGER, chat_title TEXT, chat_username TEXT, invite_link TEXT,
    title TEXT, status TEXT DEFAULT 'active', created_at INTEGER, ends_at INTEGER,
    ended_at INTEGER, announce_msg_id INTEGER, votes_per_user INTEGER DEFAULT 1,
    allow_self_vote INTEGER DEFAULT 0, require_join INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS participants(
    giveaway_id INTEGER, user_id INTEGER, name TEXT, username TEXT, msg_id INTEGER DEFAULT 0,
    votes INTEGER DEFAULT 0, disq INTEGER DEFAULT 0, joined_at INTEGER,
    PRIMARY KEY(giveaway_id, user_id));
CREATE TABLE IF NOT EXISTS votes(
    giveaway_id INTEGER, voter_id INTEGER, candidate_id INTEGER, created_at INTEGER,
    PRIMARY KEY(giveaway_id, voter_id, candidate_id));
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, giveaway_id INTEGER, votes INTEGER, amount INTEGER,
    status TEXT DEFAULT 'created', utr TEXT, proof TEXT, created_at INTEGER, done_at INTEGER);
CREATE INDEX IF NOT EXISTS idx_part_votes ON participants(giveaway_id, votes DESC);
CREATE INDEX IF NOT EXISTS idx_gv_owner ON giveaways(owner_id);
"""

REQUIRED_TABLES = {"users", "giveaways", "participants", "votes", "settings"}


class DB:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self._open()

    def _open(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.executescript(SCHEMA)

    # ---------- low level ----------
    def q(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchone()

    def ex(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            return cur.rowcount if cur.lastrowid in (None, 0) else cur.lastrowid

    def rc(self, sql, args=()):
        """execute and return rowcount"""
        with self.lock:
            return self.c.execute(sql, args).rowcount

    # ---------- users ----------
    def upsert_user(self, u):
        now = int(time.time())
        self.ex("""INSERT INTO users(id, first_name, username, joined_at, last_seen) VALUES(?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET first_name=excluded.first_name,
                   username=excluded.username, last_seen=excluded.last_seen""",
                (u["id"], u.get("first_name", ""), u.get("username"), now, now))

    def is_banned(self, uid):
        r = self.one("SELECT banned FROM users WHERE id=?", (uid,))
        return bool(r and r["banned"])

    def set_ban(self, uid, val):
        self.ex("INSERT OR IGNORE INTO users(id, first_name, joined_at, last_seen) VALUES(?,?,?,?)",
                (uid, "", int(time.time()), int(time.time())))
        return self.rc("UPDATE users SET banned=? WHERE id=?", (1 if val else 0, uid))

    def all_user_ids(self):
        return [r["id"] for r in self.q("SELECT id FROM users WHERE banned=0")]

    # ---------- settings / state ----------
    def get_setting(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set_setting(self, key, value):
        if value is None:
            self.ex("DELETE FROM settings WHERE key=?", (key,))
        else:
            self.ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)))

    def set_state(self, uid, state, data=None):
        self.ex("INSERT INTO states(user_id,state,data) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, data=excluded.data",
                (uid, state, json.dumps(data or {})))

    def get_state(self, uid):
        r = self.one("SELECT state,data FROM states WHERE user_id=?", (uid,))
        if not r:
            return None, {}
        try:
            return r["state"], json.loads(r["data"] or "{}")
        except ValueError:
            return r["state"], {}

    def clear_state(self, uid):
        self.ex("DELETE FROM states WHERE user_id=?", (uid,))

    # ---------- channels ----------
    def upsert_channel(self, chat_id, title, username, added_by):
        self.ex("""INSERT INTO channels(chat_id,title,username,added_by,active) VALUES(?,?,?,?,1)
                   ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username,
                   added_by=excluded.added_by, active=1""", (chat_id, title, username, added_by))

    def deactivate_channel(self, chat_id):
        self.ex("UPDATE channels SET active=0 WHERE chat_id=?", (chat_id,))

    def list_channels(self, uid):
        return self.q("SELECT * FROM channels WHERE added_by=? AND active=1 ORDER BY rowid DESC LIMIT 8", (uid,))

    # ---------- giveaways ----------
    def create_giveaway(self, **k):
        cols = ["code", "owner_id", "chat_id", "chat_title", "chat_username", "invite_link", "title", "created_at", "ends_at"]
        vals = [k.get(c) for c in cols]
        with self.lock:
            cur = self.c.execute(f"INSERT INTO giveaways({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
            return cur.lastrowid

    def delete_giveaway(self, gid):
        with self.lock:
            self.c.execute("DELETE FROM participants WHERE giveaway_id=?", (gid,))
            self.c.execute("DELETE FROM votes WHERE giveaway_id=?", (gid,))
            self.c.execute("DELETE FROM giveaways WHERE id=?", (gid,))

    def giveaway(self, gid):
        return self.one("SELECT * FROM giveaways WHERE id=?", (gid,))

    def giveaway_by_code(self, code):
        return self.one("SELECT * FROM giveaways WHERE code=?", (code,))

    def giveaways_of(self, owner, limit=10):
        return self.q("SELECT * FROM giveaways WHERE owner_id=? ORDER BY id DESC LIMIT ?", (owner, limit))

    def all_giveaways(self, limit=15):
        return self.q("SELECT * FROM giveaways ORDER BY id DESC LIMIT ?", (limit,))

    def expired_giveaways(self, now):
        return self.q("SELECT id FROM giveaways WHERE status IN ('active','paused') AND ends_at IS NOT NULL AND ends_at<=?", (now,))

    def set_announce(self, gid, mid):
        self.ex("UPDATE giveaways SET announce_msg_id=? WHERE id=?", (mid, gid))

    def set_gstatus(self, gid, status):
        return self.rc("UPDATE giveaways SET status=? WHERE id=? AND status!='ended'", (status, gid))

    def end_giveaway(self, gid):
        return self.rc("UPDATE giveaways SET status='ended', ended_at=? WHERE id=? AND status!='ended'",
                       (int(time.time()), gid))

    def set_gopt(self, gid, col, val):
        if col not in ("votes_per_user", "allow_self_vote", "require_join"):
            return
        self.ex(f"UPDATE giveaways SET {col}=? WHERE id=?", (val, gid))

    def gstats(self, gid):
        r = self.one("SELECT COUNT(*) n, COALESCE(SUM(votes),0) v FROM participants WHERE giveaway_id=? AND disq=0", (gid,))
        return r["n"], r["v"]

    # ---------- participants / votes ----------
    def add_participant(self, gid, user_id, name, username):
        """True agar naya add hua, False agar pehle se hai"""
        return self.rc("INSERT OR IGNORE INTO participants(giveaway_id,user_id,name,username,joined_at) VALUES(?,?,?,?,?)",
                       (gid, user_id, name, username, int(time.time()))) == 1

    def remove_participant_row(self, gid, user_id):
        self.ex("DELETE FROM participants WHERE giveaway_id=? AND user_id=?", (gid, user_id))

    def set_part_msg(self, gid, user_id, mid):
        self.ex("UPDATE participants SET msg_id=? WHERE giveaway_id=? AND user_id=?", (mid, gid, user_id))

    def participant(self, gid, uid):
        return self.one("SELECT * FROM participants WHERE giveaway_id=? AND user_id=?", (gid, uid))

    def leaderboard(self, gid, limit=10):
        return self.q("""SELECT * FROM participants WHERE giveaway_id=? AND disq=0
                         ORDER BY votes DESC, joined_at ASC LIMIT ?""", (gid, limit))

    def all_participants(self, gid):
        return self.q("SELECT * FROM participants WHERE giveaway_id=? ORDER BY votes DESC, joined_at ASC", (gid,))

    def adjust_votes(self, gid, uid, delta):
        """admin add/remove. new total ya None"""
        with self.lock:
            p = self.participant(gid, uid)
            if not p or p["disq"]:
                return None
            new = max(0, p["votes"] + delta)
            self.c.execute("UPDATE participants SET votes=? WHERE giveaway_id=? AND user_id=?", (new, gid, uid))
            return new

    def disqualify(self, gid, uid):
        p = self.participant(gid, uid)
        if not p:
            return None
        self.ex("UPDATE participants SET disq=1 WHERE giveaway_id=? AND user_id=?", (gid, uid))
        return p

    def cast_vote(self, gid, voter, cand, limit):
        """('ok', total) | ('nocand',0) | ('dup',0) | ('limit',0)"""
        with self.lock:
            self.c.execute("BEGIN IMMEDIATE")
            try:
                p = self.c.execute("SELECT votes FROM participants WHERE giveaway_id=? AND user_id=? AND disq=0",
                                   (gid, cand)).fetchone()
                if not p:
                    self.c.execute("ROLLBACK")
                    return "nocand", 0
                if self.c.execute("SELECT 1 FROM votes WHERE giveaway_id=? AND voter_id=? AND candidate_id=?",
                                  (gid, voter, cand)).fetchone():
                    self.c.execute("ROLLBACK")
                    return "dup", 0
                used = self.c.execute("SELECT COUNT(*) c FROM votes WHERE giveaway_id=? AND voter_id=?",
                                      (gid, voter)).fetchone()["c"]
                if used >= limit:
                    self.c.execute("ROLLBACK")
                    return "limit", 0
                self.c.execute("INSERT INTO votes VALUES(?,?,?,?)", (gid, voter, cand, int(time.time())))
                self.c.execute("UPDATE participants SET votes=votes+1 WHERE giveaway_id=? AND user_id=?", (gid, cand))
                total = p["votes"] + 1
                self.c.execute("COMMIT")
                return "ok", total
            except Exception:
                try:
                    self.c.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ---------- vote selling orders ----------
    def create_order(self, uid, gid, votes, amount):
        with self.lock:
            cur = self.c.execute("INSERT INTO orders(user_id,giveaway_id,votes,amount,created_at) VALUES(?,?,?,?,?)",
                                 (uid, gid, votes, amount, int(time.time())))
            return cur.lastrowid

    def order(self, oid):
        return self.one("SELECT * FROM orders WHERE id=?", (oid,))

    def set_order(self, oid, from_status, to_status):
        return self.rc("UPDATE orders SET status=?, done_at=? WHERE id=? AND status=?",
                       (to_status, int(time.time()), oid, from_status))

    def attach_proof(self, oid, proof, utr):
        return self.rc("UPDATE orders SET status='pending', proof=?, utr=? WHERE id=? AND status='created'",
                       (proof, utr, oid))

    def utr_used(self, utr):
        return bool(self.one("SELECT 1 FROM orders WHERE utr=? AND status IN ('pending','approved')", (utr,)))

    def pending_orders(self, limit=10):
        return self.q("SELECT * FROM orders WHERE status='pending' ORDER BY id LIMIT ?", (limit,))

    def user_pending_count(self, uid):
        return self.one("SELECT COUNT(*) FROM orders WHERE user_id=? AND status IN ('created','pending')", (uid,))[0]

    def user_participations(self, uid):
        return self.q("""SELECT g.* FROM participants p JOIN giveaways g ON g.id=p.giveaway_id
                         WHERE p.user_id=? AND p.disq=0 AND g.status='active' ORDER BY g.id DESC LIMIT 8""", (uid,))

    # ---------- admin stats ----------
    def stats(self):
        one = lambda s: self.one(s)[0]
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "banned": one("SELECT COUNT(*) FROM users WHERE banned=1"),
            "giveaways": one("SELECT COUNT(*) FROM giveaways"),
            "active": one("SELECT COUNT(*) FROM giveaways WHERE status='active'"),
            "participants": one("SELECT COUNT(*) FROM participants WHERE disq=0"),
            "votes": one("SELECT COALESCE(SUM(votes),0) FROM participants WHERE disq=0"),
            "channels": one("SELECT COUNT(*) FROM channels WHERE active=1"),
            "sales": one("SELECT COUNT(*) FROM orders WHERE status='approved'"),
            "revenue": one("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='approved'"),
            "pending": one("SELECT COUNT(*) FROM orders WHERE status='pending'"),
        }

    # ---------- backup / restore ----------
    def backup_to(self, dest_path):
        dest = sqlite3.connect(dest_path)
        try:
            with self.lock:
                self.c.backup(dest)
        finally:
            dest.close()

    @staticmethod
    def validate_file(path):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            con.close()
            return REQUIRED_TABLES.issubset(names)
        except sqlite3.Error:
            return False

    def restore(self, src_path):
        if not self.validate_file(src_path):
            return False
        with self.lock:
            self.c.close()
            shutil.copyfile(src_path, self.path)
            for ext in ("-wal", "-shm", "-journal"):
                try:
                    os.remove(self.path + ext)
                except OSError:
                    pass
            self._open()
        return True
