import csv
import html
import io
import logging
import os
import random
import re
import string
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import config as C
from db import DB
from icons import ic
from tg import B, Tg, TgError

log = logging.getLogger("bot")


def esc(s):
    return html.escape(str("" if s is None else s), quote=False)


def fmt_ts(ts):
    tz = timezone(timedelta(minutes=C.TZ_OFFSET_MIN))
    return datetime.fromtimestamp(ts, tz=tz).strftime("%d %b %Y, %I:%M %p") + f" {C.TZ_NAME}"


def full_name(u):
    n = (u.get("first_name") or "").strip()
    l = (u.get("last_name") or "").strip()
    n = f"{n} {l}".strip()
    return (n or u.get("username") or f"User {u.get('id')}")[:60]


CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,32}$")


class Bot:
    def __init__(self, token=None, tg=None, db=None):
        self.tg = tg or Tg(token or C.BOT_TOKEN)
        self.db = db or DB(C.DB_PATH)
        self.pool = ThreadPoolExecutor(max_workers=C.WORKERS)
        self._locks = defaultdict(threading.Lock)
        self._guard = threading.Lock()
        self.stop = False
        self.bot_id = 0
        self.bot_username = ""

    # =====================================================================
    # startup / polling
    # =====================================================================
    def init_me(self):
        me = self.tg.call("getMe")
        self.bot_id = me["id"]
        self.bot_username = me.get("username", "")
        return me

    def run(self):
        try:
            self.tg.call("deleteWebhook", {"drop_pending_updates": False})
        except TgError as e:
            log.warning("deleteWebhook: %s", e)
        offset = None
        fails = 0
        while not self.stop:
            try:
                ups = self.tg.call("getUpdates", {
                    "offset": offset, "timeout": 50,
                    "allowed_updates": ["message", "callback_query", "my_chat_member"]},
                    timeout=65, retries=0)
                fails = 0
            except TgError as e:
                fails += 1
                if e.code == 409:
                    log.warning("409 conflict (dusra instance chal raha hoga) - 5s wait")
                    time.sleep(5)
                else:
                    log.warning("getUpdates error: %s", e)
                    time.sleep(min(3 * fails, 30))
                continue
            for u in ups:
                offset = u["update_id"] + 1
                self.pool.submit(self.safe_handle, u)

    def _uid_of(self, u):
        for k in ("message", "callback_query", "my_chat_member"):
            if k in u:
                f = u[k].get("from")
                if f:
                    return f.get("id")
        return None

    def safe_handle(self, u):
        uid = self._uid_of(u)
        with self._guard:
            lock = self._locks[uid]
        try:
            with lock:
                self.handle_update(u)
        except Exception:
            log.exception("update handle error")
            try:
                if "callback_query" in u:
                    self.tg.answer(u["callback_query"]["id"], f"{ic('warn')} Error, try again", True)
                elif "message" in u and u["message"]["chat"]["type"] == "private":
                    self.tg.send(u["message"]["chat"]["id"], f"{ic('warn')} Something went wrong. Please try again or use /start")
            except Exception:
                pass

    def handle_update(self, u):
        if "message" in u:
            self.on_message(u["message"])
        elif "callback_query" in u:
            self.on_callback(u["callback_query"])
        elif "my_chat_member" in u:
            self.on_my_chat_member(u["my_chat_member"])

    # =====================================================================
    # small helpers
    # =====================================================================
    def is_admin(self, uid):
        return uid in C.ADMIN_IDS

    def deep_link(self, code):
        return f"https://t.me/{self.bot_username}?start={code}"

    def post_link(self, g, mid):
        if g["chat_username"]:
            return f"https://t.me/{g['chat_username']}/{mid}"
        cid = str(g["chat_id"])
        return f"https://t.me/c/{cid[4:] if cid.startswith('-100') else cid.lstrip('-')}/{mid}"

    def new_code(self):
        for _ in range(20):
            c = "".join(random.choices(string.ascii_letters + string.digits, k=8))
            if not self.db.giveaway_by_code(c):
                return c
        return "".join(random.choices(string.ascii_letters + string.digits, k=12))

    def chan_name(self, g):
        return f"@{g['chat_username']}" if g["chat_username"] else (g["chat_title"] or str(g["chat_id"]))

    def show(self, chat_id, mid, text, kb):
        """message edit karo, na ho paye to naya bhejo"""
        try:
            self.tg.edit(chat_id, mid, text, kb)
        except TgError:
            self.tg.send(chat_id, text, kb)

    def card_kb(self, gid, uid, votes):
        return [[B(f"{ic('heart')} Vote - {votes}", f"v:{gid}:{uid}", style="success")]]

    def refresh_card(self, g, uid, mid, votes):
        if not mid:
            return
        try:
            self.tg.edit_markup(g["chat_id"], mid, self.card_kb(g["id"], uid, votes))
        except TgError as e:
            log.info("card refresh fail: %s", e)

    # =====================================================================
    # keyboards / screens
    # =====================================================================
    def main_kb(self, uid):
        add_url = (f"https://t.me/{self.bot_username}?startchannel=true&admin="
                   "post_messages+edit_messages+delete_messages+invite_users+pin_messages")
        rows = [
            [B(f"{ic('connect')} CONNECT", url=add_url, style="success")],
            [B(f"{ic('create')} CREATE", "create", style="primary"),
             B(f"{ic('manage')} MANAGE", "manage", style="primary")],
            [B(f"{ic('help')} HELP", "help"),
             B(f"{ic('contact')} CONTACT", url=f"https://t.me/{C.OWNER_USERNAME}")],
        ]
        if self.sell_on():
            rows.insert(2, [B(f"{ic('diamond')} BUY VOTES", "buy", style="success")])
        if self.is_admin(uid):
            rows.append([B(f"{ic('admin')} ADMIN PANEL", "ad", style="danger")])
        return rows

    def welcome_text(self):
        custom = self.db.get_setting("welcome_text")
        if custom:
            return custom
        return (f"{ic('bolt')} <b>WELCOME TO {esc(C.BRAND)}</b>\n{ic('div')}\n\n"
                f"<i>{ic('heart')} Create Powerful Vote Giveaways\n"
                f"{ic('heart')} Real Time Vote System\n"
                f"{ic('gear')} Advanced Management Tools\n"
                f"{ic('chart')} Leaderboard &amp; Analytics</i>\n\n"
                f"{ic('check')} <b>START CREATING NOW!</b>")

    def send_welcome(self, uid):
        text = self.welcome_text()
        kb = self.main_kb(uid)
        photo = self.db.get_setting("welcome_photo") or C.WELCOME_IMAGE
        if photo and len(text) <= 1000:
            try:
                self.tg.send_photo(uid, photo, text, kb)
                return
            except TgError as e:
                log.warning("welcome photo fail: %s", e)
        try:
            self.tg.send(uid, text, kb)
        except TgError:
            # custom welcome text me galat HTML ho to default bhej do
            self.db.set_setting("welcome_text", None)
            self.tg.send(uid, self.welcome_text(), kb)

    def help_text(self):
        return (f"{ic('help')} <b>HELP - HOW IT WORKS</b>\n{ic('div')}\n\n"
                f"<b>1.</b> {ic('connect')} CONNECT - bot ko apne channel me admin banao\n"
                f"<b>2.</b> {ic('create')} CREATE - channel verify karo, title/prize aur time set karo\n"
                f"<b>3.</b> Bot channel me giveaway post karega + join link dega\n"
                f"<b>4.</b> Participants link se register karte hain, unka card channel me aata hai\n"
                f"<b>5.</b> Log Vote button dabake vote karte hain (1 vote per user, settings se change)\n"
                f"<b>6.</b> {ic('manage')} MANAGE - votes add/remove, leaderboard, pause, end, CSV export\n\n"
                f"<b>Commands</b>\n/start - main menu\n/create - new giveaway\n/manage - your giveaways\n"
                f"/cancel - current step cancel\n/id - apna Telegram ID\n\n"
                f"{ic('diamond')} <b>BUY VOTES</b> (agar owner ne ON kiya ho) - pack chuno, UPI se pay karo, screenshot bhejo, admin approve karega")

    def back_home_kb(self):
        return [[B(f"{ic('home')} HOME", "home")]]

    def cancel_kb(self):
        return [[B(f"{ic('cross')} CANCEL", "cancel", style="danger")]]

    # =====================================================================
    # messages
    # =====================================================================
    def on_message(self, m):
        chat = m["chat"]
        if chat["type"] != "private":
            return
        user = m.get("from") or {}
        uid = user.get("id")
        if not uid or user.get("is_bot"):
            return
        self.db.upsert_user(user)
        if not self.is_admin(uid):
            if self.db.is_banned(uid):
                self.tg.send(uid, f"{ic('ban')} You are banned from using this bot.")
                return
            if self.db.get_setting("maintenance") == "1":
                self.tg.send(uid, f"{ic('gear')} Bot is under maintenance. Please try again later.")
                return
        text = (m.get("text") or "").strip()
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            return self.on_command(uid, user, cmd.split("@")[0].lower(), arg.strip())
        state, data = self.db.get_state(uid)
        if not state:
            self.tg.send(uid, f"{ic('point')} Use the buttons below or send /start", self.main_kb(uid))
            return
        self.on_state_input(uid, user, m, text, state, data)

    def on_command(self, uid, user, cmd, arg):
        if cmd == "/start":
            self.db.clear_state(uid)
            if arg and CODE_RE.match(arg):
                return self.participate(uid, user, arg)
            return self.send_welcome(uid)
        if cmd == "/help":
            return self.tg.send(uid, self.help_text(), self.back_home_kb())
        if cmd == "/cancel":
            self.db.clear_state(uid)
            self.tg.send(uid, f"{ic('check')} Cancelled.")
            return self.send_welcome(uid)
        if cmd == "/id":
            return self.tg.send(uid, f"{ic('id')} Your Telegram ID: <code>{uid}</code>")
        if cmd == "/create":
            return self.start_create(uid)
        if cmd == "/manage":
            return self.manage_list(uid, None, None)
        if cmd == "/admin" and self.is_admin(uid):
            return self.admin_panel(uid, None)
        self.send_welcome(uid)

    # ---------------------------------------------------------------------
    def on_state_input(self, uid, user, m, text, state, data):
        if state == "await_proof":
            return self.receive_proof(uid, user, m, text, data)
        if state == "await_channel":
            ref = text
            fwd = m.get("forward_origin")
            if fwd and fwd.get("type") == "channel":
                ref = str(fwd["chat"]["id"])
            elif m.get("forward_from_chat"):
                ref = str(m["forward_from_chat"]["id"])
            if not ref:
                return self.tg.send(uid, f"{ic('cross')} Send channel @username or ID.", self.cancel_kb())
            wait = self.tg.send(uid, f"{ic('loading')} Verifying...")
            return self.verify_and_continue(uid, ref, wait["message_id"])

        if state == "await_title":
            if len(text) < 3:
                return self.tg.send(uid, f"{ic('cross')} Title/prizes thoda detail me bhejo (min 3 characters).", self.cancel_kb())
            if len(text) > 800:
                return self.tg.send(uid, f"{ic('cross')} Maximum 800 characters allowed.", self.cancel_kb())
            data["title"] = text
            self.db.set_state(uid, "await_duration", data)
            kb = [
                [B("1 HOUR", "cr:dur:1", style="primary"), B("6 HOURS", "cr:dur:6", style="primary"),
                 B("12 HOURS", "cr:dur:12", style="primary")],
                [B("24 HOURS", "cr:dur:24", style="primary"), B("3 DAYS", "cr:dur:72", style="primary"),
                 B("7 DAYS", "cr:dur:168", style="primary")],
                [B(f"{ic('star')} NO TIME LIMIT", "cr:dur:0", style="success")],
                [B(f"{ic('cross')} CANCEL", "cancel", style="danger")],
            ]
            return self.tg.send(uid, f"{ic('clock')} <b>GIVEAWAY DURATION</b>\n{ic('div')}\n\nKitne time ke liye giveaway chalana hai?", kb)

        if state == "await_duration":
            return self.tg.send(uid, f"{ic('point')} Upar diye buttons se duration select karo.", self.cancel_kb())

        if state in ("mg_add", "mg_rm"):
            nums = re.findall(r"-?\d+", text)
            if len(nums) < 2:
                return self.tg.send(uid, f"{ic('cross')} Format: <code>USER_ID AMOUNT</code>\nExample: <code>8845565421 10</code>", self.cancel_kb())
            tid, amt = int(nums[0]), abs(int(nums[1]))
            g = self.owned(data.get("gid"), uid)
            if not g or g["status"] == "ended":
                self.db.clear_state(uid)
                return self.tg.send(uid, f"{ic('cross')} Giveaway not available.")
            delta = amt if state == "mg_add" else -amt
            new = self.db.adjust_votes(g["id"], tid, delta)
            if new is None:
                return self.tg.send(uid, f"{ic('cross')} Participant <code>{tid}</code> nahi mila. Sahi ID bhejo.", self.cancel_kb())
            p = self.db.participant(g["id"], tid)
            self.refresh_card(g, tid, p["msg_id"], new)
            self.db.clear_state(uid)
            verb = "added" if delta > 0 else "removed"
            self.tg.send(uid, f"{ic('check')} {amt} votes {verb}. <b>{esc(p['name'])}</b> now has <b>{new}</b> votes.")
            return self.send_panel(uid, g["id"])

        if state == "mg_dq":
            nums = re.findall(r"\d+", text)
            g = self.owned(data.get("gid"), uid)
            if not nums or not g:
                return self.tg.send(uid, f"{ic('cross')} Participant ID bhejo.", self.cancel_kb())
            tid = int(nums[0])
            p = self.db.disqualify(g["id"], tid)
            if not p:
                return self.tg.send(uid, f"{ic('cross')} Participant <code>{tid}</code> nahi mila.", self.cancel_kb())
            if p["msg_id"]:
                try:
                    self.tg.call("deleteMessage", {"chat_id": g["chat_id"], "message_id": p["msg_id"]})
                except TgError:
                    pass
            self.db.clear_state(uid)
            self.tg.send(uid, f"{ic('ban')} <b>{esc(p['name'])}</b> disqualified &amp; card removed from channel.")
            return self.send_panel(uid, g["id"])

        # ---------------- admin states ----------------
        if not self.is_admin(uid):
            self.db.clear_state(uid)
            return
        if state == "adm_bc":
            data = {"chat": uid, "mid": m["message_id"]}
            self.db.set_state(uid, "adm_bc_confirm", data)
            n = len(self.db.all_user_ids())
            kb = [[B(f"{ic('send')} SEND TO {n} USERS", "ad:bcgo", style="success")],
                  [B(f"{ic('cross')} CANCEL", "cancel", style="danger")]]
            return self.tg.send(uid, f"{ic('broadcast')} Upar wala message {n} users ko bhejna hai?", kb)
        if state in ("adm_ban", "adm_unban"):
            nums = re.findall(r"\d+", text)
            if not nums:
                return self.tg.send(uid, f"{ic('cross')} User ID bhejo (numbers).", self.cancel_kb())
            tid = int(nums[0])
            if tid in C.ADMIN_IDS:
                return self.tg.send(uid, f"{ic('cross')} Admin ko ban nahi kar sakte.", self.cancel_kb())
            self.db.set_ban(tid, state == "adm_ban")
            self.db.clear_state(uid)
            self.tg.send(uid, f"{ic('check')} User <code>{tid}</code> {'banned' if state == 'adm_ban' else 'unbanned'}.")
            return self.admin_panel(uid, None)
        if state == "adm_wt":
            if not text:
                return self.tg.send(uid, f"{ic('cross')} Text bhejo.", self.cancel_kb())
            try:
                self.tg.send(uid, text)  # preview + HTML check
            except TgError as e:
                return self.tg.send(uid, f"{ic('cross')} HTML galat hai: {esc(e.desc)}\nDobara bhejo.", self.cancel_kb())
            self.db.set_setting("welcome_text", text)
            self.db.clear_state(uid)
            self.tg.send(uid, f"{ic('check')} Welcome text saved.")
            return self.admin_panel(uid, None)
        if state == "adm_wp":
            photos = m.get("photo")
            if not photos:
                return self.tg.send(uid, f"{ic('cross')} Photo bhejo (as photo, file nahi).", self.cancel_kb())
            self.db.set_setting("welcome_photo", photos[-1]["file_id"])
            self.db.clear_state(uid)
            self.tg.send(uid, f"{ic('check')} Welcome image saved.")
            return self.admin_panel(uid, None)
        if state == "adm_rs":
            doc = m.get("document")
            if not doc:
                return self.tg.send(uid, f"{ic('cross')} Backup .db file bhejo.", self.cancel_kb())
            try:
                blob = self.tg.download(doc["file_id"])
            except TgError as e:
                return self.tg.send(uid, f"{ic('cross')} Download failed: {esc(e.desc)}")
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                with open(tmp, "wb") as f:
                    f.write(blob)
                ok = self.db.restore(tmp)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            self.db.clear_state(uid)
            if ok:
                self.tg.send(uid, f"{ic('check')} Database restored successfully.")
                return self.admin_panel(uid, None)
            return self.tg.send(uid, f"{ic('cross')} Ye valid bot backup file nahi hai.")
        if state == "adm_upi":
            if not re.fullmatch(r"[\w.\-]{2,64}@[A-Za-z]{2,32}", text):
                return self.tg.send(uid, f"{ic('cross')} Sahi UPI ID bhejo (example: name@okhdfc).", self.cancel_kb())
            self.db.set_setting("upi", text)
            self.db.clear_state(uid)
            self.tg.send(uid, f"{ic('check')} UPI ID saved: <code>{esc(text)}</code>")
            return self.sell_panel(uid, None)
        if state == "adm_packs":
            packs = self.parse_packs(text)
            if not packs:
                return self.tg.send(uid, f"{ic('cross')} Format galat. Example:\n<code>10=10\n50=45\n100=80</code> (votes=price)", self.cancel_kb())
            self.db.set_setting("packs", ",".join(f"{v}={p}" for v, p in packs))
            self.db.clear_state(uid)
            self.tg.send(uid, f"{ic('check')} {len(packs)} packs saved.")
            return self.sell_panel(uid, None)
        if state == "adm_qr":
            photos = m.get("photo")
            if not photos:
                return self.tg.send(uid, f"{ic('cross')} QR ki photo bhejo.", self.cancel_kb())
            self.db.set_setting("sell_qr", photos[-1]["file_id"])
            self.db.clear_state(uid)
            self.tg.send(uid, f"{ic('check')} QR saved.")
            return self.sell_panel(uid, None)
        self.db.clear_state(uid)
        self.send_welcome(uid)

    # =====================================================================
    # channel connect event
    # =====================================================================
    def on_my_chat_member(self, ev):
        chat = ev["chat"]
        if chat["type"] not in ("channel", "supergroup", "group"):
            return
        status = ev["new_chat_member"]["status"]
        by = (ev.get("from") or {}).get("id")
        if status == "administrator":
            self.db.upsert_channel(chat["id"], chat.get("title", ""), chat.get("username"), by)
            if by and by != self.bot_id:
                try:
                    kb = [[B(f"{ic('create')} CREATE GIVEAWAY", "create", style="primary")]]
                    self.tg.send(by, f"{ic('check')} <b>{esc(chat.get('title', 'Channel'))}</b> connected successfully!\n\n"
                                     f"Ab CREATE dabake giveaway start karo.", kb)
                except TgError:
                    pass
        elif status in ("left", "kicked"):
            self.db.deactivate_channel(chat["id"])

    # =====================================================================
    # callbacks
    # =====================================================================
    def on_callback(self, cq):
        cid = cq["id"]
        user = cq["from"]
        uid = user["id"]
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        mid = msg.get("message_id")
        self.db.upsert_user(user)
        answered = False

        def ans(text=None, alert=False):
            nonlocal answered
            answered = True
            self.tg.answer(cid, text, alert)

        try:
            if not self.is_admin(uid):
                if self.db.is_banned(uid):
                    return ans(f"{ic('ban')} You are banned.", True)
                if self.db.get_setting("maintenance") == "1" and not data.startswith("v:"):
                    return ans("Maintenance mode", True)
            if data == "noop":
                return
            if data.startswith("v:"):
                return self.on_vote(cq, user, data, ans)
            if chat_id is None:
                return
            self.route_callback(uid, user, data, chat_id, mid, ans)
        finally:
            if not answered:
                self.tg.answer(cid)

    def route_callback(self, uid, user, data, chat_id, mid, ans):
        if data == "home":
            self.db.clear_state(uid)
            return self.send_welcome(uid)
        if data == "help":
            return self.tg.send(uid, self.help_text(), self.back_home_kb())
        if data == "cancel":
            self.db.clear_state(uid)
            ans("Cancelled")
            return self.send_welcome(uid)
        if data == "create":
            return self.start_create(uid)
        if data == "manage":
            return self.manage_list(uid, chat_id, mid)
        if data.startswith("cr:ch:"):
            state, _ = self.db.get_state(uid)
            if state != "await_channel":
                return ans("Session expired. Tap CREATE again.", True)
            self.show(chat_id, mid, f"{ic('loading')} Verifying...", None)
            return self.verify_and_continue(uid, data[6:], mid)
        if data.startswith("cr:dur:"):
            return self.finish_create(uid, data[7:], chat_id, mid, ans)
        if data.startswith("chk:"):
            return self.participate(uid, user, data[4:])
        if data == "buy":
            return self.buy_start(uid, chat_id, mid, ans)
        if data.startswith("bg:"):
            return self.buy_packs(uid, int(data[3:] or 0), chat_id, mid, ans)
        if data.startswith("bp:"):
            _, g_, i_ = data.split(":")
            return self.buy_pick(uid, int(g_), int(i_), chat_id, mid, ans)
        if data.startswith("bpaid:"):
            return self.buy_paid(uid, int(data[6:]), chat_id, mid, ans)
        if data.startswith("bx:"):
            o = self.db.order(int(data[3:]))
            if o and o["user_id"] == uid and self.db.set_order(o["id"], "created", "cancelled"):
                ans("Order cancelled")
                return self.show(chat_id, mid, f"{ic('no')} Order #{o['id']} cancelled.", self.back_home_kb())
            return ans("Order not available", True)
        if data.startswith(("oa:", "or:")):
            if not self.is_admin(uid):
                return ans("Admin only", True)
            return self.order_decide(uid, int(data[3:]), data.startswith("oa:"), chat_id, mid, ans)
        if data == "ad" or data.startswith("ad:"):
            if not self.is_admin(uid):
                return ans("Admin only", True)
            return self.admin_callback(uid, data, chat_id, mid, ans)
        m = re.match(r"^(g|gl|ga|gr|gd|gp|gs|gx|gpost|ge|gey):(\d+)(?::(\w+))?$", data)
        if m:
            return self.giveaway_callback(uid, m.group(1), int(m.group(2)), m.group(3), chat_id, mid, ans)

    # =====================================================================
    # CREATE flow
    # =====================================================================
    def start_create(self, uid):
        self.db.set_state(uid, "await_channel")
        rows = []
        for c in self.db.list_channels(uid):
            rows.append([B(f"{ic('megaphone')} {(c['title'] or 'Channel')[:32]}", f"cr:ch:{c['chat_id']}")])
        rows.append([B(f"{ic('cross')} CANCEL", "cancel", style="danger")])
        extra = "\n\n<i>Ya channel ka koi post yahan forward karo. Neeche tumhare connected channels bhi hain.</i>" if len(rows) > 1 else \
            "\n\n<i>Ya channel ka koi post yahan forward kar do.</i>"
        self.tg.send(uid, f"{ic('bolt')} <b>CREATE GIVEAWAY</b>\n{ic('div')}\n\n"
                          f"{ic('point')} Send your channel information:\n\n"
                          f"<b>Public:</b> @username\n<b>Private:</b> -1001234567890\n\n"
                          f"{ic('warn')} Pehle bot ko channel me <b>admin</b> banao (CONNECT button).{extra}", rows)

    def resolve_chat_ref(self, ref):
        ref = ref.strip()
        mt = re.match(r"^(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,})/?$", ref)
        if mt:
            ref = "@" + mt.group(1)
        if re.fullmatch(r"-\d{5,}", ref):
            return int(ref)
        if re.fullmatch(r"\d{8,}", ref):
            return int("-100" + ref)
        if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", ref):
            return "@" + ref.lstrip("@")
        return None

    def verify_channel(self, uid, ref):
        chat_ref = self.resolve_chat_ref(str(ref))
        if chat_ref is None:
            return None, "Invalid format. Use @username ya -100xxxxxxxxxx"
        try:
            chat = self.tg.call("getChat", {"chat_id": chat_ref}, retries=1)
        except TgError:
            return None, "Channel nahi mila. Username/ID check karo aur bot ko channel me admin banao."
        if chat.get("type") not in ("channel", "supergroup"):
            return None, "Sirf channel ya supergroup allowed hai."
        bm = self.tg.get_member(chat["id"], self.bot_id)
        if not bm or bm.get("status") not in ("administrator", "creator"):
            return None, "Bot is channel me admin nahi hai. Pehle CONNECT dabake bot ko admin banao."
        if chat["type"] == "channel" and bm.get("status") == "administrator" and not bm.get("can_post_messages"):
            return None, "Bot ko 'Post Messages' permission do."
        um = self.tg.get_member(chat["id"], uid)
        if not um:
            return None, "Tumhari admin status verify nahi ho payi."
        if um.get("status") not in ("creator", "administrator"):
            return None, "Tum is channel ke admin nahi ho."
        return chat, None

    def verify_and_continue(self, uid, ref, wait_mid):
        chat, err = self.verify_channel(uid, ref)
        if err:
            return self.show(uid, wait_mid, f"{ic('cross')} {esc(err)}\n\nDobara channel info bhejo.", self.cancel_kb())
        invite = ""
        if chat.get("username"):
            invite = f"https://t.me/{chat['username']}"
        else:
            invite = chat.get("invite_link") or ""
            if not invite:
                try:
                    invite = self.tg.call("exportChatInviteLink", {"chat_id": chat["id"]}, retries=1)
                except TgError:
                    invite = ""
        data = {"chat_id": chat["id"], "chan_title": chat.get("title", ""), "username": chat.get("username"), "invite": invite}
        self.db.set_state(uid, "await_title", data)
        self.show(uid, wait_mid,
                  f"{ic('check')} <b>Channel verified:</b> {esc(chat.get('title', ''))}\n\n"
                  f"{ic('gift')} Ab giveaway ka <b>title / prizes / rules</b> bhejo.\n"
                  f"<i>Example: AJ giveaway hoga jiske sabse jyada vote usko Prizes milenge\n1st - Telegram Account</i>",
                  self.cancel_kb())

    def finish_create(self, uid, hours_s, chat_id, mid, ans):
        state, data = self.db.get_state(uid)
        if state != "await_duration" or not data.get("chat_id"):
            return ans("Session expired. Tap CREATE again.", True)
        try:
            hours = int(hours_s)
        except ValueError:
            return
        now = int(time.time())
        ends = now + hours * 3600 if hours > 0 else None
        code = self.new_code()
        gid = self.db.create_giveaway(code=code, owner_id=uid, chat_id=data["chat_id"], chat_title=data.get("chan_title", ""),
                                      chat_username=data.get("username"), invite_link=data.get("invite"),
                                      title=data["title"], created_at=now, ends_at=ends)
        g = self.db.giveaway(gid)
        link = self.deep_link(code)
        text = (f"{ic('party')} <b>NEW GIVEAWAY STARTED!</b>\n{ic('div')}\n\n<blockquote>{esc(g['title'])}</blockquote>\n\n"
                + (f"{ic('clock')} <b>Ends:</b> {fmt_ts(ends)}\n\n" if ends else "")
                + f"{ic('arrow')} <b>JOIN HERE:</b>\n{link}")
        kb = [[B(f"{ic('party')} JOIN GIVEAWAY", url=link, style="success")]]
        try:
            res = self.tg.send(g["chat_id"], text, kb)
        except TgError as e:
            self.db.delete_giveaway(gid)
            self.db.clear_state(uid)
            return self.show(chat_id, mid, f"{ic('cross')} Channel me post nahi ho paya: {esc(e.desc)}\n\n"
                                            f"Bot ko admin + Post Messages permission do aur dobara CREATE karo.",
                             self.back_home_kb())
        self.db.set_announce(gid, res["message_id"])
        try:
            self.tg.call("pinChatMessage", {"chat_id": g["chat_id"], "message_id": res["message_id"], "disable_notification": True}, retries=0)
        except TgError:
            pass
        self.db.clear_state(uid)
        self.show(chat_id, mid, f"{ic('check')} <b>GIVEAWAY CREATED!</b>\n{ic('div')}\n\n{ic('link')} Join link:\n{link}\n\nChannel me post ho gaya.", None)
        self.send_panel(uid, gid)

    # =====================================================================
    # participant flow
    # =====================================================================
    def participate(self, uid, user, code):
        g = self.db.giveaway_by_code(code)
        if not g:
            return self.tg.send(uid, f"{ic('cross')} Invalid or expired giveaway link.", self.back_home_kb())
        if g["status"] == "ended":
            return self.tg.send(uid, f"{ic('lock')} This giveaway has ended.", self.back_home_kb())
        if g["status"] == "paused":
            return self.tg.send(uid, f"{ic('pause')} Giveaway is paused right now. Try again later.", self.back_home_kb())
        if True:
            mem = self.tg.is_member(g["chat_id"], uid)
            if mem is None:
                return self.tg.send(uid, f"{ic('warn')} Channel verify nahi ho paya. Giveaway owner se bolo bot ko channel me admin rakhe.")
            if mem is False:
                rows = []
                if g["invite_link"]:
                    rows.append([B(f"{ic('megaphone')} JOIN CHANNEL", url=g["invite_link"], style="primary")])
                rows.append([B(f"{ic('check')} I JOINED - VERIFY", f"chk:{code}", style="success")])
                return self.tg.send(uid, f"{ic('lock')} Participate karne ke liye pehle channel join karo:\n<b>{esc(self.chan_name(g))}</b>", rows)
        p = self.db.participant(g["id"], uid)
        if p:
            if p["disq"]:
                return self.tg.send(uid, f"{ic('ban')} You were disqualified from this giveaway.")
            kb = [[B(f"{ic('link')} VIEW MY POST", url=self.post_link(g, p["msg_id"]), style="primary")]] if p["msg_id"] else None
            return self.tg.send(uid, f"{ic('check')} Tum already registered ho.\n\n{ic('heart')} Votes: <b>{p['votes']}</b>", kb)
        name = full_name(user)
        if not self.db.add_participant(g["id"], uid, name, user.get("username")):
            return self.tg.send(uid, f"{ic('check')} Already registered.")
        card = (f"{ic('smile')} <b>Name:</b> {esc(name)}\n{ic('heart')} <b>ID:</b> <code>{uid}</code>")
        try:
            res = self.tg.send(g["chat_id"], card, self.card_kb(g["id"], uid, 0))
        except TgError as e:
            self.db.remove_participant_row(g["id"], uid)
            log.warning("card post fail: %s", e)
            return self.tg.send(uid, f"{ic('cross')} Registration failed (channel me post nahi hua). Thodi der baad try karo.")
        self.db.set_part_msg(g["id"], uid, res["message_id"])
        plink = self.post_link(g, res["message_id"])
        share_target = plink if g["chat_username"] else self.deep_link(code)
        share = "https://t.me/share/url?url=" + quote(share_target, safe="") + "&text=" + quote("Vote for me! 💖", safe="")
        kb = [[B(f"{ic('link')} VIEW MY POST", url=plink, style="primary")],
              [B(f"{ic('send')} SHARE & GET VOTES", url=share, style="success")]]
        if self.sell_on():
            kb.append([B(f"{ic('diamond')} BUY VOTES", f"bg:{g['id']}", style="success")])
        self.tg.send(uid, f"{ic('check')} <b>Registration Successful!</b>\n\n{ic('smile')} {esc(name)}\n{ic('heart')} <code>{uid}</code>\n\n"
                          f"{ic('megaphone')} Profile shared in channel!", kb)

    def on_vote(self, cq, user, data, ans):
        try:
            _, gid_s, cand_s = data.split(":")
            gid, cand = int(gid_s), int(cand_s)
        except ValueError:
            return ans()
        uid = user["id"]
        g = self.db.giveaway(gid)
        if not g:
            return ans("Giveaway not found", True)
        if g["status"] == "ended":
            return ans(f"{ic('lock')} Giveaway has ended.", True)
        if g["status"] == "paused":
            return ans(f"{ic('pause')} Voting is paused.", True)
        if g["ends_at"] and g["ends_at"] <= int(time.time()):
            return ans(f"{ic('lock')} Giveaway time is over.", True)
        if uid == cand and not g["allow_self_vote"]:
            return ans(f"{ic('cross')} Apne aap ko vote nahi de sakte.", True)
        mem = self.tg.is_member(g["chat_id"], uid)
        if mem is False:
            return ans(f"{ic('lock')} Vote karne ke liye pehle channel join karo: {self.chan_name(g)}", True)
        if mem is None:
            return ans(f"{ic('warn')} Channel membership verify nahi ho paya. Owner se bolo bot ko admin rakhe.", True)
        res, total = self.db.cast_vote(gid, uid, cand, g["votes_per_user"])
        if res == "nocand":
            return ans("Participant removed", True)
        if res == "dup":
            return ans(f"{ic('check')} Tum is participant ko pehle hi vote de chuke ho.", True)
        if res == "limit":
            return ans(f"{ic('cross')} Vote limit reached ({g['votes_per_user']} per user).", True)
        ans(f"{ic('heart')} Vote counted! Total: {total}")
        msg = cq.get("message") or {}
        mid = msg.get("message_id")
        if mid:
            try:
                self.tg.edit_markup(msg["chat"]["id"], mid, self.card_kb(gid, cand, total))
            except TgError as e:
                log.info("vote edit fail: %s", e)

    # =====================================================================
    # MANAGE
    # =====================================================================
    def owned(self, gid, uid):
        if gid is None:
            return None
        g = self.db.giveaway(int(gid))
        if g and (g["owner_id"] == uid or self.is_admin(uid)):
            return g
        return None

    def manage_list(self, uid, chat_id, mid):
        gs = self.db.giveaways_of(uid)
        if not gs:
            text = f"{ic('manage')} <b>MANAGE</b>\n{ic('div')}\n\nTumne abhi tak koi giveaway nahi banaya. CREATE dabao."
            kb = [[B(f"{ic('create')} CREATE", "create", style="primary")], [B(f"{ic('home')} HOME", "home")]]
        else:
            dot = {"active": ic("green"), "paused": ic("yellow"), "ended": ic("red")}
            kb = [[B(f"{dot.get(g['status'], '')} {(g['chat_title'] or g['chat_username'] or 'Channel')[:26]} #{g['id']}", f"g:{g['id']}", style="primary")] for g in gs]
            kb.append([B(f"{ic('home')} HOME", "home")])
            text = f"{ic('manage')} <b>MANAGE GIVEAWAYS</b>\n{ic('div')}\n\nSelect a giveaway:"
        if chat_id and mid:
            self.show(chat_id, mid, text, kb)
        else:
            self.tg.send(uid, text, kb)

    def panel_content(self, g):
        n, v = self.db.gstats(g["id"])
        st = {"active": f"{ic('green')} ACTIVE", "paused": f"{ic('yellow')} PAUSED", "ended": f"{ic('red')} ENDED"}[g["status"]]
        text = (f"{ic('arrow')} <b>MANAGEMENT PANEL</b>\n{ic('div')}\n\n"
                f"<b>Status:</b> {st}\n<b>Channel:</b> {esc(self.chan_name(g))}\n"
                f"<b>Joined:</b> {n}\n<b>Total Votes:</b> {v}\n"
                f"<b>Votes per user:</b> {g['votes_per_user']}\n"
                f"<b>Ends:</b> {fmt_ts(g['ends_at']) if g['ends_at'] else 'No limit'}\n"
                f"<b>Link:</b> {self.deep_link(g['code'])}")
        i = g["id"]
        if g["status"] == "ended":
            kb = [[B(f"{ic('trophy')} LEADERBOARD", f"gl:{i}", style="primary")],
                  [B(f"{ic('send')} POST RESULTS", f"gpost:{i}"), B(f"{ic('file')} EXPORT CSV", f"gx:{i}")],
                  [B(f"{ic('back')} BACK", "manage")]]
        else:
            pr = (B(f"{ic('play')} RESUME", f"gp:{i}", style="success") if g["status"] == "paused"
                  else B(f"{ic('pause')} PAUSE", f"gp:{i}"))
            kb = [[B(f"{ic('plus')} ADD VOTES", f"ga:{i}", style="primary"), B(f"{ic('minus')} REMOVE VOTES", f"gr:{i}", style="danger")],
                  [B(f"{ic('trophy')} LEADERBOARD", f"gl:{i}", style="primary")],
                  [pr, B(f"{ic('gear')} SETTINGS", f"gs:{i}")],
                  [B(f"{ic('send')} POST LEADERBOARD", f"gpost:{i}"), B(f"{ic('file')} EXPORT CSV", f"gx:{i}")],
                  [B(f"{ic('ban')} DISQUALIFY", f"gd:{i}")],
                  [B(f"{ic('cross')} END GIVEAWAY", f"ge:{i}", style="danger")],
                  [B(f"{ic('back')} BACK", "manage", style="primary")]]
        return text, kb

    def send_panel(self, uid, gid, chat_id=None, mid=None):
        g = self.db.giveaway(gid)
        if not g:
            return
        text, kb = self.panel_content(g)
        if chat_id and mid:
            self.show(chat_id, mid, text, kb)
        else:
            self.tg.send(uid, text, kb)

    def lb_text(self, g, limit=10, title="LEADERBOARD"):
        rows = self.db.leaderboard(g["id"], limit)
        n, v = self.db.gstats(g["id"])
        medals = [ic("gold"), ic("silver"), ic("bronze")]
        lines = []
        for i, p in enumerate(rows):
            mark = medals[i] if i < 3 else f"<b>{i + 1}.</b>"
            lines.append(f"{mark} {esc(p['name'])} - <b>{p['votes']}</b> votes")
        body = "\n".join(lines) if lines else "<i>Abhi koi participant nahi hai.</i>"
        return (f"{ic('trophy')} <b>{title}</b>\n{ic('div')}\n\n{body}\n\n{ic('people')} Participants: {n}  |  {ic('heart')} Votes: {v}")

    def giveaway_callback(self, uid, action, gid, sub, chat_id, mid, ans):
        g = self.owned(gid, uid)
        if not g:
            return ans("Giveaway not found / access denied", True)
        back = [[B(f"{ic('back')} BACK", f"g:{gid}", style="primary")]]
        if action == "g":
            self.db.clear_state(uid)
            return self.send_panel(uid, gid, chat_id, mid)
        if action == "gl":
            return self.show(chat_id, mid, self.lb_text(g, 15), back)
        if action in ("ga", "gr", "gd"):
            if g["status"] == "ended":
                return ans("Giveaway ended", True)
            state = {"ga": "mg_add", "gr": "mg_rm", "gd": "mg_dq"}[action]
            self.db.set_state(uid, state, {"gid": gid})
            if action == "gd":
                text = f"{ic('ban')} <b>DISQUALIFY</b>\n{ic('div')}\n\nParticipant ka <b>User ID</b> bhejo:"
            else:
                what = "ADD" if action == "ga" else "REMOVE"
                text = (f"{ic('plus') if action == 'ga' else ic('minus')} <b>{what} VOTES</b>\n\n"
                        f"Participant ID aur votes bhejo:\n<code>USER_ID AMOUNT</code>\nExample: <code>8845565421 10</code>")
            return self.show(chat_id, mid, text, [[B(f"{ic('cross')} CANCEL", f"g:{gid}", style="danger")]])
        if action == "gp":
            if g["status"] == "active":
                self.db.set_gstatus(gid, "paused")
                ans("Paused")
            elif g["status"] == "paused":
                self.db.set_gstatus(gid, "active")
                ans("Resumed")
            return self.send_panel(uid, gid, chat_id, mid)
        if action == "gs":
            if g["status"] == "ended":
                return ans("Giveaway ended", True)
            if sub == "self":
                self.db.set_gopt(gid, "allow_self_vote", 0 if g["allow_self_vote"] else 1)
            elif sub == "lim":
                order = [1, 2, 3, 5, 10]
                cur = g["votes_per_user"]
                nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else 1
                self.db.set_gopt(gid, "votes_per_user", nxt)
            g = self.db.giveaway(gid)
            onoff = lambda x: f"{ic('green')} ON" if x else f"{ic('red')} OFF"
            text = (f"{ic('gear')} <b>GIVEAWAY SETTINGS</b>\n{ic('div')}\n\n"
                    f"Self vote: {onoff(g['allow_self_vote'])}\nChannel join required: {ic('lock')} ALWAYS ON\n"
                    f"Votes per user: <b>{g['votes_per_user']}</b>")
            kb = [[B(f"Self vote: {'ON' if g['allow_self_vote'] else 'OFF'}", f"gs:{gid}:self", style="primary")],
                  [B(f"Votes per user: {g['votes_per_user']} (change)", f"gs:{gid}:lim", style="primary")],
                  [B(f"{ic('back')} BACK", f"g:{gid}")]]
            return self.show(chat_id, mid, text, kb)
        if action == "gx":
            rows = self.db.all_participants(gid)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["rank", "user_id", "name", "username", "votes", "disqualified", "joined_at"])
            for i, p in enumerate(rows, 1):
                w.writerow([i, p["user_id"], p["name"], p["username"] or "", p["votes"], p["disq"], fmt_ts(p["joined_at"])])
            data = ("\ufeff" + buf.getvalue()).encode("utf-8")
            ans("Exporting...")
            try:
                return self.tg.send_doc(uid, f"giveaway_{gid}_participants.csv", data, f"{ic('file')} Giveaway #{gid} export")
            except TgError as e:
                return self.tg.send(uid, f"{ic('cross')} Export failed: {esc(e.desc)}")
        if action == "gpost":
            title = "FINAL RESULTS" if g["status"] == "ended" else "LEADERBOARD"
            try:
                self.tg.send(g["chat_id"], self.lb_text(g, 10, title))
                return ans(f"{ic('check')} Posted in channel", True)
            except TgError as e:
                return ans(f"Post failed: {e.desc[:150]}", True)
        if action == "ge":
            if g["status"] == "ended":
                return ans("Already ended", True)
            kb = [[B(f"{ic('check')} YES, END NOW", f"gey:{gid}", style="danger")],
                  [B(f"{ic('cross')} NO", f"g:{gid}", style="success")]]
            return self.show(chat_id, mid, f"{ic('warn')} <b>END GIVEAWAY?</b>\n\nVoting band ho jayegi aur winners channel me announce honge.", kb)
        if action == "gey":
            if not self.end_giveaway(gid):
                return ans("Already ended", True)
            ans("Giveaway ended")
            return self.send_panel(uid, gid, chat_id, mid)

    def end_giveaway(self, gid):
        if not self.db.end_giveaway(gid):
            return False
        g = self.db.giveaway(gid)
        top = self.db.leaderboard(gid, 3)
        n, v = self.db.gstats(gid)
        medals = [ic("gold"), ic("silver"), ic("bronze")]
        lines = []
        for i, p in enumerate(top):
            lines.append(f"{medals[i]} <a href=\"tg://user?id={p['user_id']}\">{esc(p['name'])}</a> - <b>{p['votes']}</b> votes")
        win = "\n".join(lines) if lines else "<i>No participants</i>"
        text = (f"{ic('party')} <b>GIVEAWAY ENDED!</b>\n{ic('div')}\n\n{esc(g['title'])}\n\n"
                f"{ic('trophy')} <b>WINNERS</b>\n{win}\n\n{ic('people')} Participants: {n}  |  {ic('heart')} Votes: {v}\n\n"
                f"{ic('heart')} Thanks to everyone for participating!")
        try:
            self.tg.send(g["chat_id"], text)
        except TgError as e:
            log.warning("result post fail: %s", e)
        if g["announce_msg_id"]:
            try:
                self.tg.edit(g["chat_id"], g["announce_msg_id"],
                             f"{ic('lock')} <b>GIVEAWAY ENDED</b>\n{ic('div')}\n\n{esc(g['title'])}", [])
            except TgError:
                pass
        try:
            self.tg.send(g["owner_id"], f"{ic('lock')} Giveaway #{gid} ended.\n\n" + self.lb_text(g, 10, "FINAL LEADERBOARD"))
        except TgError:
            pass
        for i, p in enumerate(top):
            try:
                self.tg.send(p["user_id"], f"{ic('party')} Congratulations! You finished <b>#{i + 1}</b> in the giveaway of "
                                           f"<b>{esc(self.chan_name(g))}</b> with {p['votes']} votes!")
            except TgError:
                pass
        return True

    # =====================================================================
    # ADMIN PANEL
    # =====================================================================
    def admin_panel(self, uid, mid_pair):
        mt = self.db.get_setting("maintenance") == "1"
        text = f"{ic('admin')} <b>ADMIN PANEL</b>\n{ic('div')}\n\nBot owner controls."
        kb = [[B(f"{ic('stats')} STATS", "ad:stats", style="primary"), B(f"{ic('broadcast')} BROADCAST", "ad:bc", style="primary")],
              [B(f"{ic('ban')} BAN USER", "ad:ban", style="danger"), B(f"{ic('check')} UNBAN USER", "ad:unban", style="success")],
              [B(f"{ic('diamond')} VOTE SELLING", "ad:sell", style="success"), B(f"{ic('gift')} ALL GIVEAWAYS", "ad:gv")],
              [B(f"{ic('pin')} WELCOME TEXT", "ad:wt"), B(f"{ic('star')} WELCOME IMAGE", "ad:wp")],
              [B(f"{ic('refresh')} RESET WELCOME", "ad:wr")],
              [B(f"{ic('gear')} MAINTENANCE: {'ON' if mt else 'OFF'}", "ad:mt", style="danger" if mt else "success")],
              [B(f"{ic('backup')} BACKUP DB", "ad:bk", style="primary"), B(f"{ic('refresh')} RESTORE DB", "ad:rs")],
              [B(f"{ic('home')} HOME", "home")]]
        if mid_pair:
            self.show(mid_pair[0], mid_pair[1], text, kb)
        else:
            self.tg.send(uid, text, kb)

    def admin_callback(self, uid, data, chat_id, mid, ans):
        back = [[B(f"{ic('back')} BACK", "ad", style="primary")]]
        if data == "ad":
            self.db.clear_state(uid)
            return self.admin_panel(uid, (chat_id, mid))
        act = data[3:]
        if act == "stats":
            s = self.db.stats()
            text = (f"{ic('stats')} <b>BOT STATS</b>\n{ic('div')}\n\n{ic('people')} Users: <b>{s['users']}</b> (banned {s['banned']})\n"
                    f"{ic('gift')} Giveaways: <b>{s['giveaways']}</b> (active {s['active']})\n"
                    f"{ic('user')} Participants: <b>{s['participants']}</b>\n{ic('heart')} Total votes: <b>{s['votes']}</b>\n"
                    f"{ic('megaphone')} Connected channels: <b>{s['channels']}</b>\n"
                    f"{ic('diamond')} Vote sales: <b>{s['sales']}</b> | Revenue: <b>Rs {s['revenue']}</b> | Pending: <b>{s['pending']}</b>")
            return self.show(chat_id, mid, text, back)
        if act == "sell":
            return self.sell_panel(uid, (chat_id, mid))
        if act.startswith("sell:"):
            return self.sell_action(uid, act[5:], chat_id, mid, ans)
        if act == "bc":
            self.db.set_state(uid, "adm_bc")
            return self.show(chat_id, mid, f"{ic('broadcast')} <b>BROADCAST</b>\n{ic('div')}\n\nJo message sabko bhejna hai wo ab bhejo (text/photo/video sab chalega).",
                             self.cancel_kb())
        if act == "bcgo":
            state, d = self.db.get_state(uid)
            if state != "adm_bc_confirm":
                return ans("Session expired", True)
            self.db.clear_state(uid)
            ans("Broadcast started")
            self.show(chat_id, mid, f"{ic('send')} Broadcast started in background...", None)
            threading.Thread(target=self.do_broadcast, args=(uid, d["chat"], d["mid"]), daemon=True).start()
            return
        if act in ("ban", "unban"):
            self.db.set_state(uid, f"adm_{act}")
            return self.show(chat_id, mid, f"{ic('ban')} <b>{act.upper()} USER</b>\n\nUser ka Telegram ID bhejo:", self.cancel_kb())
        if act == "gv":
            gs = self.db.all_giveaways(12)
            dot = {"active": ic("green"), "paused": ic("yellow"), "ended": ic("red")}
            kb = [[B(f"{dot.get(g['status'], '')} #{g['id']} {(g['chat_title'] or g['chat_username'] or '')[:22]}", f"ad:gx:{g['id']}", style="primary")] for g in gs]
            kb.append([B(f"{ic('back')} BACK", "ad")])
            return self.show(chat_id, mid, f"{ic('gift')} <b>ALL GIVEAWAYS</b> (latest 12)", kb)
        if act.startswith("gx:"):
            gid = int(act[3:])
            g = self.db.giveaway(gid)
            if not g:
                return ans("Not found", True)
            text, kb = self.panel_content(g)
            text += f"\n<b>Owner:</b> <code>{g['owner_id']}</code>"
            kb[-1] = [B(f"{ic('back')} BACK", "ad:gv")]
            return self.show(chat_id, mid, text, kb)
        if act == "wt":
            self.db.set_state(uid, "adm_wt")
            return self.show(chat_id, mid, f"{ic('pin')} <b>WELCOME TEXT</b>\n{ic('div')}\n\nNaya welcome text bhejo. HTML allowed: &lt;b&gt;bold&lt;/b&gt;, &lt;i&gt;italic&lt;/i&gt;. "
                                           f"Icons apne emoji se daal sakte ho.", self.cancel_kb())
        if act == "wp":
            self.db.set_state(uid, "adm_wp")
            return self.show(chat_id, mid, f"{ic('star')} <b>WELCOME IMAGE</b>\n{ic('div')}\n\nPhoto bhejo (as photo).", self.cancel_kb())
        if act == "wr":
            self.db.set_setting("welcome_text", None)
            self.db.set_setting("welcome_photo", None)
            ans("Welcome reset to default", True)
            return
        if act == "mt":
            cur = self.db.get_setting("maintenance") == "1"
            self.db.set_setting("maintenance", "0" if cur else "1")
            return self.admin_panel(uid, (chat_id, mid))
        if act == "bk":
            ans("Creating backup...")
            return self.send_backup(uid)
        if act == "rs":
            self.db.set_state(uid, "adm_rs")
            return self.show(chat_id, mid, f"{ic('refresh')} <b>RESTORE DATABASE</b>\n{ic('div')}\n\n{ic('warn')} Current data replace ho jayega.\n"
                                           f"Backup <code>.db</code> file bhejo.", self.cancel_kb())

    def do_broadcast(self, admin_id, from_chat, msg_id):
        ok = fail = 0
        ids = self.db.all_user_ids()
        for u in ids:
            try:
                self.tg.call("copyMessage", {"chat_id": u, "from_chat_id": from_chat, "message_id": msg_id}, retries=1)
                ok += 1
            except TgError:
                fail += 1
            time.sleep(0.05)
        try:
            self.tg.send(admin_id, f"{ic('check')} <b>Broadcast finished</b>\n\nSent: {ok}\nFailed/blocked: {fail}")
        except TgError:
            pass

    def send_backup(self, chat_id):
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            self.db.backup_to(tmp)
            with open(tmp, "rb") as f:
                blob = f.read()
            name = "votebot_backup_" + datetime.now().strftime("%Y%m%d_%H%M") + ".db"
            self.tg.send_doc(chat_id, name, blob, f"{ic('backup')} Database backup. Restore ke liye ADMIN PANEL -> RESTORE DB.")
        except Exception as e:
            log.warning("backup fail: %s", e)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    # =====================================================================
    # VOTE SELLING
    # =====================================================================
    def sell_on(self):
        return self.db.get_setting("sell_on") == "1" and bool(self.db.get_setting("upi"))

    @staticmethod
    def parse_packs(raw):
        out = []
        for part in re.split(r"[,\n]+", raw or ""):
            m = re.match(r"^\s*(\d+)\s*[=:@-]\s*(\d+)\s*$", part)
            if m and int(m.group(1)) > 0 and int(m.group(2)) > 0:
                out.append((int(m.group(1)), int(m.group(2))))
        return out[:8]

    def packs(self):
        return self.parse_packs(self.db.get_setting("packs") or "10=10,50=45,100=80,500=350")

    def sell_panel(self, uid, pair):
        on = self.db.get_setting("sell_on") == "1"
        upi = self.db.get_setting("upi")
        s = self.db.stats()
        packs = "\n".join(f"{ic('tri')} {v} votes = Rs {p}" for v, p in self.packs())
        text = (f"{ic('diamond')} <b>VOTE SELLING</b>\n{ic('div')}\n\n"
                f"<b>Status:</b> {ic('green') + ' ON' if on else ic('red') + ' OFF'}\n"
                f"<b>UPI:</b> {('<code>' + esc(upi) + '</code>') if upi else 'not set'}\n"
                f"<b>QR:</b> {'set' if self.db.get_setting('sell_qr') else 'not set'}\n\n<b>Packs</b>\n{packs}\n\n"
                f"Sales: <b>{s['sales']}</b> | Revenue: <b>Rs {s['revenue']}</b> | Pending: <b>{s['pending']}</b>")
        kb = [[B(f"{ic('refresh')} TURN {'OFF' if on else 'ON'}", "ad:sell:tog", style="danger" if on else "success")],
              [B(f"{ic('key')} SET UPI ID", "ad:sell:upi", style="primary"), B(f"{ic('star')} SET QR", "ad:sell:qr", style="primary")],
              [B(f"{ic('plan')} SET PACKS", "ad:sell:packs", style="primary")],
              [B(f"{ic('clock')} PENDING ORDERS ({s['pending']})", "ad:sell:orders")],
              [B(f"{ic('back')} BACK", "ad")]]
        if pair:
            self.show(pair[0], pair[1], text, kb)
        else:
            self.tg.send(uid, text, kb)

    def sell_action(self, uid, act, chat_id, mid, ans):
        if act == "tog":
            if self.db.get_setting("sell_on") != "1" and not self.db.get_setting("upi"):
                return ans("Pehle UPI ID set karo", True)
            self.db.set_setting("sell_on", "0" if self.db.get_setting("sell_on") == "1" else "1")
            return self.sell_panel(uid, (chat_id, mid))
        cancel = [[B(f"{ic('cross')} CANCEL", "ad:sell", style="danger")]]
        if act == "upi":
            self.db.set_state(uid, "adm_upi")
            return self.show(chat_id, mid, f"{ic('key')} <b>SET UPI ID</b>\n\nApni UPI ID bhejo (example: <code>name@okhdfc</code>)", cancel)
        if act == "qr":
            self.db.set_state(uid, "adm_qr")
            return self.show(chat_id, mid, f"{ic('star')} <b>SET QR</b>\n\nPayment QR ki photo bhejo.", cancel)
        if act == "packs":
            self.db.set_state(uid, "adm_packs")
            return self.show(chat_id, mid, f"{ic('plan')} <b>SET PACKS</b>\n\nHar line me <code>votes=price</code> bhejo (max 8):\n"
                                           f"<code>10=10\n50=45\n100=80\n500=350</code>", cancel)
        if act == "orders":
            rows = self.db.pending_orders(10)
            if not rows:
                return ans("Koi pending order nahi", True)
            ans(f"{len(rows)} pending orders bheje")
            for o in rows:
                self.send_order_to(uid, o)

    def order_caption(self, o):
        g = self.db.giveaway(o["giveaway_id"])
        p = self.db.participant(o["giveaway_id"], o["user_id"])
        return (f"{ic('diamond')} <b>VOTE ORDER #{o['id']}</b>\n{ic('div')}\n\n"
                f"<b>User:</b> {esc(p['name'] if p else '?')} (<code>{o['user_id']}</code>)\n"
                f"<b>Giveaway:</b> #{o['giveaway_id']} {esc(self.chan_name(g) if g else '')}\n"
                f"<b>Votes:</b> {o['votes']}\n<b>Amount:</b> Rs {o['amount']}\n<b>UTR:</b> {esc(o['utr'] or 'not given')}")

    def send_order_to(self, admin_id, o):
        kb = [[B(f"{ic('ok')} APPROVE", f"oa:{o['id']}", style="success"), B(f"{ic('no')} REJECT", f"or:{o['id']}", style="danger")]]
        try:
            if o["proof"]:
                self.tg.send_photo(admin_id, o["proof"], self.order_caption(o), kb)
            else:
                self.tg.send(admin_id, self.order_caption(o), kb)
        except TgError as e:
            log.warning("order notify fail: %s", e)

    def valid_buy_target(self, uid, gid):
        g = self.db.giveaway(gid)
        p = self.db.participant(gid, uid) if g else None
        if not g or g["status"] != "active" or not p or p["disq"]:
            return None
        return g

    def buy_start(self, uid, chat_id, mid, ans):
        if not self.sell_on():
            return ans("Vote selling abhi band hai", True)
        gs = self.db.user_participations(uid)
        if not gs:
            return self.show(chat_id, mid, f"{ic('warn')} Votes kharidne ke liye pehle kisi giveaway me register karo.", self.back_home_kb())
        if len(gs) == 1:
            return self.buy_packs(uid, gs[0]["id"], chat_id, mid, ans)
        kb = [[B(f"{(g['chat_title'] or g['chat_username'] or 'Giveaway')[:28]} #{g['id']}", f"bg:{g['id']}", style="primary")] for g in gs]
        kb.append([B(f"{ic('home')} HOME", "home")])
        self.show(chat_id, mid, f"{ic('diamond')} <b>BUY VOTES</b>\n{ic('div')}\n\nKis giveaway ke liye?", kb)

    def buy_packs(self, uid, gid, chat_id, mid, ans):
        if not self.sell_on():
            return ans("Vote selling abhi band hai", True)
        g = self.valid_buy_target(uid, gid)
        if not g:
            return ans("Is giveaway me tum active participant nahi ho", True)
        kb = [[B(f"{v} VOTES - Rs {p}", f"bp:{gid}:{i}", style="success")] for i, (v, p) in enumerate(self.packs())]
        kb.append([B(f"{ic('home')} HOME", "home")])
        self.show(chat_id, mid, f"{ic('diamond')} <b>BUY VOTES</b>\n{ic('div')}\n\n<b>Giveaway:</b> {esc(self.chan_name(g))}\n\nPack chuno:", kb)

    def buy_pick(self, uid, gid, idx, chat_id, mid, ans):
        if not self.sell_on():
            return ans("Vote selling abhi band hai", True)
        g = self.valid_buy_target(uid, gid)
        packs = self.packs()
        if not g or idx < 0 or idx >= len(packs):
            return ans("Not available", True)
        if self.db.user_pending_count(uid) >= 3:
            return ans("Tumhare 3 orders pehle se pending hain. Pehle unhe complete karo.", True)
        votes, price = packs[idx]
        oid = self.db.create_order(uid, gid, votes, price)
        upi = self.db.get_setting("upi")
        text = (f"{ic('diamond')} <b>ORDER #{oid}</b>\n{ic('div')}\n\n"
                f"<b>Votes:</b> {votes}\n<b>Amount:</b> Rs {price}\n\n"
                f"{ic('key')} <b>Pay to UPI:</b>\n<code>{esc(upi)}</code>\n\n"
                f"{ic('warn')} Exact amount <b>Rs {price}</b> bhejo. Payment ke baad <b>I HAVE PAID</b> dabao aur screenshot bhejo (UTR/Txn ID caption me likho).")
        kb = [[B(f"{ic('ok')} I HAVE PAID", f"bpaid:{oid}", style="success")],
              [B(f"{ic('no')} CANCEL ORDER", f"bx:{oid}", style="danger")]]
        qr = self.db.get_setting("sell_qr")
        if qr:
            try:
                self.tg.send_photo(uid, qr, f"{ic('star')} Scan &amp; pay Rs {price}")
            except TgError:
                pass
        self.tg.send(uid, text, kb)

    def buy_paid(self, uid, oid, chat_id, mid, ans):
        o = self.db.order(oid)
        if not o or o["user_id"] != uid or o["status"] != "created":
            return ans("Order not available", True)
        self.db.set_state(uid, "await_proof", {"oid": oid})
        self.tg.send(uid, f"{ic('upload')} <b>Payment screenshot bhejo</b> (photo ki tarah).\n"
                          f"Caption me <b>UTR / Transaction ID</b> likh do.", [[B(f"{ic('cross')} CANCEL", "cancel", style="danger")]])

    def receive_proof(self, uid, user, m, text, data):
        o = self.db.order(data.get("oid") or 0)
        if not o or o["user_id"] != uid or o["status"] != "created":
            self.db.clear_state(uid)
            return self.tg.send(uid, f"{ic('cross')} Order available nahi hai. /start se dobara try karo.")
        photos = m.get("photo")
        if not photos:
            return self.tg.send(uid, f"{ic('cross')} Screenshot photo bhejo (file nahi, photo).", [[B(f"{ic('cross')} CANCEL", "cancel", style="danger")]])
        cap = m.get("caption") or text or ""
        mt = re.search(r"\b\d{9,18}\b", cap)
        utr = mt.group(0) if mt else None
        if utr and self.db.utr_used(utr):
            return self.tg.send(uid, f"{ic('cross')} Ye UTR pehle use ho chuka hai. Sahi screenshot bhejo.")
        if not self.db.attach_proof(o["id"], photos[-1]["file_id"], utr):
            self.db.clear_state(uid)
            return self.tg.send(uid, f"{ic('cross')} Order already processed.")
        self.db.clear_state(uid)
        o = self.db.order(o["id"])
        for a in C.ADMIN_IDS:
            self.send_order_to(a, o)
        self.tg.send(uid, f"{ic('ok')} <b>Request sent!</b>\n\nOrder #{o['id']} admin approval ke liye gaya hai. Approve hote hi votes add ho jayenge.",
                     self.back_home_kb())

    def order_decide(self, admin_uid, oid, approve, chat_id, mid, ans):
        o = self.db.order(oid)
        if not o or o["status"] != "pending":
            return ans("Already processed / not found", True)
        who = esc(self.db.one("SELECT first_name FROM users WHERE id=?", (admin_uid,))["first_name"] if self.db.one("SELECT 1 FROM users WHERE id=?", (admin_uid,)) else admin_uid)
        if approve:
            g = self.db.giveaway(o["giveaway_id"])
            p = self.db.participant(o["giveaway_id"], o["user_id"])
            if not g or g["status"] == "ended" or not p or p["disq"]:
                return ans("Giveaway ended ya participant disqualified - REJECT karo", True)
            if not self.db.set_order(oid, "pending", "approved"):
                return ans("Already processed", True)
            new = self.db.adjust_votes(o["giveaway_id"], o["user_id"], o["votes"])
            self.refresh_card(g, o["user_id"], p["msg_id"], new)
            try:
                self.tg.send(o["user_id"], f"{ic('ok')} <b>Payment approved!</b>\n\n{o['votes']} votes add ho gaye. Total votes: <b>{new}</b>")
            except TgError:
                pass
            status = f"{ic('ok')} APPROVED by {who}"
        else:
            if not self.db.set_order(oid, "pending", "rejected"):
                return ans("Already processed", True)
            try:
                self.tg.send(o["user_id"], f"{ic('no')} <b>Order #{oid} rejected.</b>\n\nPayment verify nahi hui. Galti lage to @{C.OWNER_USERNAME} se contact karo.")
            except TgError:
                pass
            status = f"{ic('no')} REJECTED by {who}"
        ans("Done")
        try:
            self.tg.call("editMessageCaption", {"chat_id": chat_id, "message_id": mid, "caption": self.order_caption(self.db.order(oid)) + f"\n\n<b>{status}</b>",
                                                "parse_mode": "HTML", "reply_markup": {"inline_keyboard": []}}, retries=0)
        except TgError:
            try:
                self.tg.edit(chat_id, mid, self.order_caption(self.db.order(oid)) + f"\n\n<b>{status}</b>", [])
            except TgError:
                pass

    # =====================================================================
    # background jobs
    # =====================================================================
    def scheduler_loop(self):
        last_backup = time.time()
        while not self.stop:
            try:
                now = int(time.time())
                for r in self.db.expired_giveaways(now):
                    log.info("auto-ending giveaway %s", r["id"])
                    self.end_giveaway(r["id"])
                if C.BACKUP_HOURS > 0 and C.ADMIN_IDS and time.time() - last_backup >= C.BACKUP_HOURS * 3600:
                    last_backup = time.time()
                    self.send_backup(C.ADMIN_IDS[0])
            except Exception:
                log.exception("scheduler error")
            time.sleep(20)
