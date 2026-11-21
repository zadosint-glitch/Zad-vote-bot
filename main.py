import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import config as C
from bot import Bot
from tg import TgError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


class Health(BaseHTTPRequestHandler):
    def _ok(self, body=True):
        data = b"ZAD VOTE BOT is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok(False)

    def log_message(self, *a):
        pass


def start_http():
    srv = ThreadingHTTPServer(("0.0.0.0", C.PORT), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("health server on port %s", C.PORT)


def keepalive():
    if not C.KEEPALIVE_URL:
        return
    while True:
        time.sleep(600)
        try:
            requests.get(C.KEEPALIVE_URL, timeout=20)
        except requests.RequestException:
            pass


def main():
    if not C.BOT_TOKEN:
        print("ERROR: BOT_TOKEN environment variable set nahi hai.")
        sys.exit(1)
    start_http()  # Render ko port chahiye, pehle hi open kar do
    bot = Bot()
    try:
        me = bot.init_me()
    except TgError as e:
        print(f"ERROR: BOT_TOKEN galat hai ya Telegram se connect nahi hua: {e}")
        sys.exit(1)
    log.info("Bot started as @%s | admins=%s", me.get("username"), C.ADMIN_IDS)
    threading.Thread(target=bot.scheduler_loop, daemon=True).start()
    threading.Thread(target=keepalive, daemon=True).start()
    bot.run()


if __name__ == "__main__":
    main()
