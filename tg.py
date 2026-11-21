import json
import logging
import time

import requests

log = logging.getLogger("tg")


class TgError(Exception):
    def __init__(self, code, desc, params=None):
        super().__init__(f"[{code}] {desc}")
        self.code = code
        self.desc = desc or ""
        self.params = params or {}


def B(text, data=None, url=None, style=None):
    """Inline button. style = primary (blue) | success (green) | danger (red)"""
    b = {"text": text}
    if url:
        b["url"] = url
    else:
        b["callback_data"] = (data or "noop")[:64]
    if style:
        b["style"] = style
    return b


class Tg:
    def __init__(self, token):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"
        self.s = requests.Session()

    # ---------- core ----------
    def call(self, method, params=None, files=None, timeout=30, retries=2):
        params = dict(params or {})
        for k, v in list(params.items()):
            if v is None:
                del params[k]
            elif isinstance(v, (dict, list)):
                params[k] = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, bool):
                params[k] = "true" if v else "false"
        attempt = 0
        stripped = False
        while True:
            try:
                r = self.s.post(self.base + method, data=params, files=files, timeout=timeout)
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                attempt += 1
                if attempt > retries:
                    raise TgError(0, f"network: {e}")
                time.sleep(1.5 * attempt)
                continue
            if data.get("ok"):
                return data["result"]
            code = data.get("error_code", 0)
            desc = data.get("description", "")
            ra = (data.get("parameters") or {}).get("retry_after")
            if code == 429 and attempt < retries:
                attempt += 1
                time.sleep(min(int(ra or 2) + 1, 15))
                continue
            low = desc.lower()
            if (code == 400 and not stripped and "reply_markup" in params
                    and '"style"' in params["reply_markup"]
                    and ("style" in low or "button" in low or "unknown" in low or "field" in low)):
                # purani Bot API version: colored button support nahi -> style hata ke dobara try
                stripped = True
                params["reply_markup"] = params["reply_markup"].replace('"style": "primary", ', '') \
                    .replace('"style": "success", ', '').replace('"style": "danger", ', '') \
                    .replace(', "style": "primary"', '').replace(', "style": "success"', '') \
                    .replace(', "style": "danger"', '')
                continue
            raise TgError(code, desc, data.get("parameters"))

    # ---------- helpers ----------
    def send(self, chat_id, text, kb=None, **extra):
        p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
             "link_preview_options": {"is_disabled": True}}
        if kb is not None:
            p["reply_markup"] = {"inline_keyboard": kb}
        p.update(extra)
        return self.call("sendMessage", p)

    def edit(self, chat_id, mid, text, kb=None):
        p = {"chat_id": chat_id, "message_id": mid, "text": text, "parse_mode": "HTML",
             "link_preview_options": {"is_disabled": True}}
        if kb is not None:
            p["reply_markup"] = {"inline_keyboard": kb}
        try:
            return self.call("editMessageText", p)
        except TgError as e:
            if "not modified" in e.desc.lower():
                return None
            raise

    def edit_markup(self, chat_id, mid, kb):
        p = {"chat_id": chat_id, "message_id": mid, "reply_markup": {"inline_keyboard": kb}}
        try:
            return self.call("editMessageReplyMarkup", p)
        except TgError as e:
            if "not modified" in e.desc.lower():
                return None
            raise

    def answer(self, cq_id, text=None, alert=False):
        try:
            self.call("answerCallbackQuery", {"callback_query_id": cq_id, "text": text,
                                              "show_alert": alert if text else None}, retries=0)
        except TgError:
            pass

    def send_photo(self, chat_id, photo, caption, kb=None):
        p = {"chat_id": chat_id, "photo": photo, "caption": caption, "parse_mode": "HTML"}
        if kb is not None:
            p["reply_markup"] = {"inline_keyboard": kb}
        return self.call("sendPhoto", p)

    def send_doc(self, chat_id, filename, data, caption=None):
        p = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        return self.call("sendDocument", p, files={"document": (filename, data)}, timeout=120)

    def get_member(self, chat_id, user_id):
        try:
            return self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id}, retries=1)
        except TgError:
            return None

    def is_member(self, chat_id, user_id):
        """True / False / None (None = verify nahi ho paya, bot admin nahi ya API error)"""
        m = self.get_member(chat_id, user_id)
        if not m:
            return None
        st = m.get("status")
        if st in ("creator", "administrator", "member"):
            return True
        if st == "restricted":
            return bool(m.get("is_member"))
        return False

    def download(self, file_id, max_bytes=20 * 1024 * 1024):
        info = self.call("getFile", {"file_id": file_id})
        path = info.get("file_path")
        if not path:
            raise TgError(0, "file path missing")
        r = self.s.get(self.file_base + path, timeout=60)
        if r.status_code != 200 or len(r.content) > max_bytes:
            raise TgError(r.status_code, "download failed")
        return r.content
