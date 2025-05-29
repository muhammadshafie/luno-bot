"""
Luno grid-bot – Render-ready version
-----------------------------------
• Reads LUNO_API_KEY and LUNO_API_SECRET from the environment (preferred)
  – if missing, will still read api.txt to keep local work simple.
• Starts a minimal Flask server (keep_alive()) so UptimeRobot can ping it.
• Everything else is your original logic, only cosmetically reordered.
"""
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

from dotenv import load_dotenv           # <-- NEW
from keep_alive import keep_alive        # <-- NEW (create keep_alive.py as shown before)


# ────────────────────────────────────────────────────────────────────────────────
# 1. one-time set-up
# ────────────────────────────────────────────────────────────────────────────────
keep_alive()                 # 🚀 spin up the Flask server first
load_dotenv()                # 🔐 read .env into os.environ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log")
    ]
)

running_bots = []
shutdown_event = threading.Event()

summary_logger = logging.getLogger("summary")
summary_logger.setLevel(logging.INFO)
summary_logger.propagate = False
summary_logger.handlers.clear()
summary_logger.addHandler(logging.StreamHandler(sys.stdout))

# ────────────────────────────────────────────────────────────────────────────────
# 2. grid-bot class
# ────────────────────────────────────────────────────────────────────────────────

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
            data = r.json()
            for market in data.get("markets", []):
                if market.get("market_id") == self.market_pair:
                    return market.get("price_scale", 4)
        except Exception as e:
            self.logger.error(f"Failed to fetch price scale for {self.market_pair}: {e}")
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

    def get_last_trade_price(self):
        try:
            r = requests.get(f"{self.api_base}trades?pair={self.market_pair}", headers=self.luno_auth_headers())
            r.raise_for_status()
            trades = r.json().get("trades", [])
            if trades:
                return float(trades[0]["price"])
        except Exception as e:
            self.logger.error(f"Failed to fetch last trade price: {e}")
            return self.get_current_price()

    def get_balance(self, currency):
        try:
            r = requests.get(f"{self.api_base}balance", headers=self.luno_auth_headers())
            r.raise_for_status()
            balances = r.json().get("balance", [])
            total = 0.0
            found = False
            for item in balances:
                if item["asset"] == currency:
                    found = True
                    bal = float(item["balance"])
                    res = float(item["reserved"])
                    net = bal - res
                    total += net
                    self.logger.info(f"[Balance Debug] {currency} entry: {item} => net={net}")
            if not found:
                self.logger.warning(f"[Balance Debug] No balance entry found for {currency}")
            return total
        except Exception as e:
            self.logger.error(f"Failed to fetch balance: {e}")
            return 0.0
            return 0.0
        except Exception as e:
            self.logger.error(f"Failed to fetch balance: {e}")
            return 0.0

    def place_limit_order(self, order_type, price):
        price_str = f"{price:.{self.decimal_places}f}"
        data = {
            "pair": self.market_pair,
            "type": order_type,
            "volume": f"{self.trade_quantity}",
            "price": price_str,
            "post_only": "true"
        }
        try:
            r = requests.post(f"{self.api_base}postorder", headers=self.luno_auth_headers(), data=data)
            if r.status_code != 200:
                self.logger.error(f"Failed to place {order_type} order at {price_str}: HTTP {r.status_code} - {r.text}")
                return None
            response = r.json()
            order_id = response.get("order_id")
            if order_id:
                self.order_price_map[order_id] = (price, "BUY" if order_type == "BID" else "SELL")
            return order_id
        except Exception as e:
            self.logger.error(f"Exception placing {order_type} order at {price_str}: {e}")
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
            self.logger.info("All open orders canceled.")
        except Exception as e:
            self.logger.error(f"Error while cancelling open orders: {e}")

    def get_completed_orders(self):
        with shared_lock:
            return [o for o in shared_order_cache.get(self.market_pair, []) if o.get("state") == "COMPLETE"]


    def rebuild_grid(self, base_price):
        algo_balance = self.get_balance(self.currency)
        fiat_balance = self.get_balance(self.fiat_currency)
        self.cancel_all_open_orders()
        self.logger.info(f"Rebuilding grid around price {base_price:.{self.decimal_places}f}")
        time.sleep(1)

        buy_price = float(Decimal(base_price * (1 - self.grid_buy_percentage)).quantize(Decimal('1') / (10 ** self.decimal_places), rounding=ROUND_DOWN))
        sell_price = float(Decimal(base_price * (1 + self.grid_sell_percentage)).quantize(Decimal('1') / (10 ** self.decimal_places), rounding=ROUND_UP))

        buy_id = None
        sell_id = None

        needed_fiat_for_buy = buy_price * self.trade_quantity

        if fiat_balance >= needed_fiat_for_buy:
            buy_id = self.place_limit_order("BID", buy_price)
        if buy_id:
            fiat_balance -= needed_fiat_for_buy
        else:
            self.logger.warning(f"[Grid] FAILED to place BUY order at {buy_price:.{self.decimal_places}f}")

        if algo_balance >= self.trade_quantity:
            sell_id = self.place_limit_order("ASK", sell_price)
            if not sell_id:
                self.logger.warning(f"[Grid] FAILED to place SELL order at {sell_price:.{self.decimal_places}f}")
        else:
            self.logger.warning(f"[Grid] Skipped SELL at {sell_price:.{self.decimal_places}f} - Not enough {self.currency} balance.")

        self.logger.info(f"[Grid] BUY @ {buy_price:.{self.decimal_places}f} (ID {buy_id}) | SELL @ {sell_price:.{self.decimal_places}f} (ID {sell_id})")
        buy_summary = f"{buy_price:.{self.decimal_places}f}" if buy_id else "skipped"
        sell_summary = f"{sell_price:.{self.decimal_places}f}" if sell_id else "skipped"
        summary_logger.info(f"{self.currency}: buy @ {buy_summary}, sell @ {sell_summary}")

        self.layer = {
            "buy_order_id": buy_id,
            "sell_order_id": sell_id,
            "buy_price": buy_price,
            "sell_price": sell_price
        }

    def run(self):
        algo_balance = self.get_balance(self.currency)
        fiat_balance = self.get_balance(self.fiat_currency)
        self.logger.info(f"[Startup] Initialized grid bot for {self.market_pair} with balances: {self.currency}={algo_balance:.6f}, {self.fiat_currency}={fiat_balance:.2f}")
        base_price = self.get_current_price()
        if base_price:
            self.rebuild_grid(base_price)
        else:
            self.logger.error("Failed to get initial price. Exiting.")
            return

        while self.running:
            try:
                completed_orders = self.get_completed_orders()
                active_order_ids = {self.layer['buy_order_id'], self.layer['sell_order_id']} if self.layer else set()

                for completed in completed_orders:
                    completed_id = completed.get("order_id")
                    if completed_id not in active_order_ids:
                        continue

                    side = self.order_price_map.get(completed_id, (None, None))[1]
                    if side:
                        self.logger.info(f"Order filled: {side} {completed_id}")

                    fill_price = float(completed.get("limit_price")) or self.get_current_price()
                    self.rebuild_grid(fill_price)
                    break

            except Exception as e:
                self.logger.error(f"Unexpected error: {e}")

            if shutdown_event.wait(timeout=self.check_interval):
                break

# ────────────────────────────────────────────────────────────────────────────────
# 3. helpers & signal-handler
# ────────────────────────────────────────────────────────────────────────────────

def signal_handler(sig, frame):
    summary_logger.info("TRL+C received. Shutting down bots...")
    shutdown_event.set()
    logging.info("Shutdown requested... cleaning up all bots.")
    for bot in running_bots:
        bot.running = False
        bot.cancel_all_open_orders()
    logging.info("All bots have been shut down. Exiting.")
    summary_logger.info("Shutdown successful.")
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
            if r.status_code == 429:
                logging.warning(f"[RateLimit] Too many requests for {pair}. Backing off.")
                time.sleep(5)
                continue
            r.raise_for_status()
            with shared_lock:
                shared_order_cache[pair] = r.json().get("orders", [])
        except Exception as e:
            logging.error(f"[Poller] Failed to poll orders for {pair}: {e}")
        time.sleep(5)


# ────────────────────────────────────────────────────────────────────────────────
# 4. main entry-point
# ────────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with open("api.txt") as f:
        config = json.load(f)
    # 4-A 🔑 fetch credentials ────────────────────────────────────────────────
    api_key = os.getenv("LUNO_API_KEY")
    api_secret = os.getenv("LUNO_API_SECRET")

    if not api_key or not api_secret:
        logging.error("❌  API credentials not found in Secrets or api.txt – aborting.")
        sys.exit(1)

    if "bots" not in config or not config["bots"]:
        logging.error("No bot configurations found in api.txt. Exiting.")
        summary_logger.info("Shutdown failed.")
        sys.exit(1)

    bot_configs = config["bots"]

    for bot_cfg in bot_configs:
        bot = GridBot(
            api_key=api_key,
            api_secret=api_secret,
            market_pair=bot_cfg["market_pair"],
            trade_quantity=bot_cfg["trade_quantity"],
            grid_buy_percentage=bot_cfg.get("grid_buy_percentage", 0.01),
            grid_sell_percentage=bot_cfg.get("grid_sell_percentage", 0.01)
        )
        running_bots.append(bot)
        t = Thread(target=bot.run)
        t.start()
        Thread(target=poll_orders, args=(bot.market_pair, api_key, api_secret), daemon=True).start()
        time.sleep(2)

    # 4-C ⏳ keep main thread alive ───────────────────────────────────────────
    try:
        while any(bot.running for bot in running_bots):
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)
