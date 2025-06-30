
# Luno.py

import os
import requests
import time
import threading
import base64
import logging
import signal
import sys
from threading import Thread
import json
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from dotenv import load_dotenv
from keep_alive import keep_alive

# Telegram integration
def send_telegram_message(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[Telegram] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": chat_id, "text": message})
    except Exception as e:
        print(f"[Telegram] Failed to send message: {e}")

keep_alive()
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.FileHandler("bot.log")]
)

running_bots = []
shutdown_event = threading.Event()
summary_logger = logging.getLogger("summary")
summary_logger.setLevel(logging.INFO)
summary_logger.propagate = False
summary_logger.handlers.clear()
summary_logger.addHandler(logging.StreamHandler(sys.stdout))

class GridBot:
    def __init__(self, api_key, api_secret, market_pair, trade_quantity, grid_buy_percentage, grid_sell_percentage, check_interval=10):
        self.api_base = "https://api.luno.com/api/1/"
        self.api_key = api_key
        self.api_secret = api_secret
        self.market_pair = market_pair
        self.currency = market_pair[:-3]
        self.fiat_currency = market_pair[-3:]
        self.logger = logging.getLogger(self.market_pair)
        self.trade_quantity = trade_quantity
        self.grid_buy_percentage = grid_buy_percentage
        self.grid_sell_percentage = grid_sell_percentage
        self.decimal_places = self.fetch_price_scale()
        self.check_interval = check_interval
        self.running = True
        self.order_price_map = {}
        self.layer = None

    def fetch_price_scale(self):
        try:
            r = requests.get("https://api.luno.com/api/exchange/1/markets")
            r.raise_for_status()
            for market in r.json().get("markets", []):
                if market.get("market_id") == self.market_pair:
                    return market.get("price_scale", 4)
        except Exception as e:
            self.logger.error(f"Failed to fetch price scale: {e}")
        return 4

    def luno_auth_headers(self):
        auth = f"{self.api_key}:{self.api_secret}"
        b64_auth = base64.b64encode(auth.encode()).decode()
        return {"Authorization": f"Basic {b64_auth}"}

    def get_current_price(self):
        try:
            r = requests.get(f"{self.api_base}ticker?pair={self.market_pair}", headers=self.luno_auth_headers())
            r.raise_for_status()
            data = r.json()
            return (float(data['bid']) + float(data['ask'])) / 2
        except Exception as e:
            self.logger.error(f"Failed to fetch price: {e}")
            return None

    def get_balance(self, currency):
        try:
            r = requests.get(f"{self.api_base}balance", headers=self.luno_auth_headers())
            r.raise_for_status()
            total = 0.0
            for item in r.json().get("balance", []):
                if item["asset"] == currency:
                    bal = float(item["balance"])
                    res = float(item["reserved"])
                    total += (bal - res)
            return total
        except Exception as e:
            self.logger.error(f"Failed to fetch balance: {e}")
            return 0.0

    def place_limit_order(self, order_type, price):
        price_str = f"{price:.{self.decimal_places}f}"
        data = {
            "pair": self.market_pair,
            "type": order_type,
            "volume": str(self.trade_quantity),
            "price": price_str,
            "post_only": "true"
        }
        try:
            r = requests.post(f"{self.api_base}postorder", headers=self.luno_auth_headers(), data=data)
            r.raise_for_status()
            order_id = r.json().get("order_id")
            if order_id:
                self.order_price_map[order_id] = (price, "BUY" if order_type == "BID" else "SELL")
            return order_id
        except Exception as e:
            self.logger.error(f"Failed to place {order_type} order: {e}")
            return None

    def cancel_order(self, order_id):
        try:
            requests.post(f"{self.api_base}stoporder", headers=self.luno_auth_headers(), data={"order_id": order_id})
        except Exception as e:
            self.logger.error(f"Failed to cancel order {order_id}: {e}")

    def cancel_all_open_orders(self):
        try:
            r = requests.get(f"{self.api_base}listorders", headers=self.luno_auth_headers())
            r.raise_for_status()
            for order in r.json().get("orders", []):
                if order["state"] == "PENDING" and order["pair"] == self.market_pair:
                    self.cancel_order(order["order_id"])
        except Exception as e:
            self.logger.error(f"Error cancelling orders: {e}")

    def get_completed_orders(self):
        with shared_lock:
            return [o for o in shared_order_cache.get(self.market_pair, []) if o.get("state") == "COMPLETE"]

    def rebuild_grid(self, base_price):
        algo_balance = self.get_balance(self.currency)
        fiat_balance = self.get_balance(self.fiat_currency)
        self.cancel_all_open_orders()
        buy_price = float(Decimal(base_price * (1 - self.grid_buy_percentage)).quantize(Decimal('1') / (10 ** self.decimal_places), rounding=ROUND_DOWN))
        sell_price = float(Decimal(base_price * (1 + self.grid_sell_percentage)).quantize(Decimal('1') / (10 ** self.decimal_places), rounding=ROUND_UP))
        buy_id = None
        sell_id = None

        needed_fiat = buy_price * self.trade_quantity
        if fiat_balance >= needed_fiat:
            buy_id = self.place_limit_order("BID", buy_price)
        if algo_balance >= self.trade_quantity:
            sell_id = self.place_limit_order("ASK", sell_price)

        self.layer = {
            "buy_order_id": buy_id,
            "sell_order_id": sell_id,
            "buy_price": buy_price,
            "sell_price": sell_price
        }

        message = f"🔄 Rebuilding Grid [{self.market_pair}]
BUY @ {buy_price} ({'✅' if buy_id else '❌'})
SELL @ {sell_price} ({'✅' if sell_id else '❌'})"
        send_telegram_message(message)

    def run(self):
        base_price = self.get_current_price()
        if not base_price:
            return
        self.rebuild_grid(base_price)

        while self.running:
            try:
                completed_orders = self.get_completed_orders()
                for completed in completed_orders:
                    order_id = completed.get("order_id")
                    if order_id not in {self.layer["buy_order_id"], self.layer["sell_order_id"]}:
                        continue
                    price, side = self.order_price_map.get(order_id, (None, None))
                    fill_price = float(completed.get("limit_price") or self.get_current_price())
                    profit = 0
                    if side == "SELL":
                        profit = (fill_price - self.layer["buy_price"]) * self.trade_quantity
                        msg = f"💰 Order Filled: SELL @ {fill_price} | Profit: {profit:.2f} {self.fiat_currency}"
                        send_telegram_message(msg)
                    elif side == "BUY":
                        msg = f"🛒 Order Filled: BUY @ {fill_price}"
                        send_telegram_message(msg)
                    self.rebuild_grid(fill_price)
                    break
            except Exception as e:
                self.logger.error(f"Error in bot loop: {e}")
            if shutdown_event.wait(timeout=self.check_interval):
                break

def signal_handler(sig, frame):
    summary_logger.info("CTRL+C received. Shutting down...")
    shutdown_event.set()
    for bot in running_bots:
        bot.running = False
        bot.cancel_all_open_orders()
    send_telegram_message("🛑 Grid bot shutting down.")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
shared_order_cache = {}
shared_lock = threading.Lock()

def poll_orders(pair, api_key, api_secret):
    auth = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    while True:
        try:
            r = requests.get(f"https://api.luno.com/api/1/listorders?pair={pair}", headers=headers)
            r.raise_for_status()
            with shared_lock:
                shared_order_cache[pair] = r.json().get("orders", [])
        except:
            pass
        time.sleep(5)

if __name__ == "__main__":
    with open("api.txt") as f:
        config = json.load(f)
    api_key = os.getenv("LUNO_API_KEY")
    api_secret = os.getenv("LUNO_API_SECRET")
    if not api_key or not api_secret:
        sys.exit("Missing LUNO_API_KEY or LUNO_API_SECRET")

    for cfg in config["bots"]:
        bot = GridBot(
            api_key,
            api_secret,
            cfg["market_pair"],
            cfg["trade_quantity"],
            cfg.get("grid_buy_percentage", 0.01),
            cfg.get("grid_sell_percentage", 0.02)
        )
        running_bots.append(bot)
        Thread(target=bot.run).start()
        Thread(target=poll_orders, args=(bot.market_pair, api_key, api_secret), daemon=True).start()
        time.sleep(2)

    while any(bot.running for bot in running_bots):
        time.sleep(1)
