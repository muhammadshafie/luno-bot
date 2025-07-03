import os
import requests
import time
import threading
import base64
import logging
import signal
import sys
from threading import Thread, Timer
import json
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timedelta

# ────────────────────────────────────────────────────────────────────────────────
# 1. one-time set-up
# ────────────────────────────────────────────────────────────────────────────────
from dotenv import load_dotenv           # <-- NEW
from keep_alive import keep_alive        # <-- NEW (create keep_alive.py as shown before)

keep_alive()                 # 🚀 spin up the Flask server first
load_dotenv()                # 🔐 read .env into os.environ

# Configure logging
logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
	handlers=[
		logging.FileHandler("bot.log"),
		logging.StreamHandler(sys.stdout)
	]
)

running_bots = []
shutdown_event = threading.Event()

class TelegramNotifier:
	def __init__(self, bot_token, chat_id):
		self.bot_token = bot_token
		self.chat_id = chat_id
		self.base_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
		
	def send_message(self, text):
		try:
			payload = {
				"chat_id": self.chat_id,
				"text": text,
				"parse_mode": "HTML"
			}
			response = requests.post(self.base_url, json=payload, timeout=10)
			response.raise_for_status()
			return True
		except Exception as e:
			logging.error(f"Failed to send Telegram message: {e}")
			return False

class GridBot:
	def __init__(self, api_key, api_secret, market_pair, trade_quantity, 
				 grid_percentage=0.017, quantity_multiplier=1, notifier=None):
		self.api_base = "https://api.luno.com/api/1/"
		self.api_key = api_key
		self.api_secret = api_secret
		self.market_pair = market_pair
		self.currency = market_pair[:-3]
		self.fiat_currency = market_pair[-3:]
		self.logger = logging.getLogger(self.market_pair)
		self.trade_quantity = trade_quantity
		self.grid_percentage = grid_percentage
		self.quantity_multiplier = quantity_multiplier
		self.notifier = notifier
		self.decimal_places = self.fetch_price_scale()
		self.running = True
		self.active_buy_orders = {}
		self.active_sell_orders = {}
		self.realized_profit = 0.0
		self.last_reset_time = datetime.now()

	def fetch_price_scale(self):
		"""Fetch the number of decimal places allowed for prices"""
		try:
			r = requests.get("https://api.luno.com/api/exchange/1/markets")
			r.raise_for_status()
			data = r.json()
			for market in data.get("markets", []):
				if market.get("market_id") == self.market_pair:
					return market.get("price_scale", 4)
			return 4
		except Exception as e:
			self.logger.error(f"Failed to fetch price scale for {self.market_pair}: {e}")
			return 4

	def luno_auth_headers(self):
		"""Generate authentication headers for Luno API"""
		auth = f"{self.api_key}:{self.api_secret}"
		b64_auth = base64.b64encode(auth.encode()).decode()
		return {"Authorization": f"Basic {b64_auth}"}

	def get_current_price(self):
		"""Get current market price (average of bid/ask)"""
		try:
			r = requests.get(f"{self.api_base}ticker?pair={self.market_pair}", 
						   headers=self.luno_auth_headers())
			r.raise_for_status()
			data = r.json()
			return (float(data['bid']) + float(data['ask'])) / 2
		except Exception as e:
			self.logger.error(f"Failed to fetch price: {e}")
			return None

	def get_balance(self, currency):
		"""Get available balance for a currency"""
		try:
			r = requests.get(f"{self.api_base}balance", headers=self.luno_auth_headers())
			r.raise_for_status()
			balances = r.json().get("balance", [])
			for item in balances:
				if item["asset"] == currency:
					return float(item["balance"]) - float(item["reserved"])
			return 0.0
		except Exception as e:
			self.logger.error(f"Failed to fetch {currency} balance: {e}")
			return 0.0

	def place_limit_order(self, order_type, price, quantity=None):
		"""Place a limit order with retry logic"""
		if quantity is None:
			quantity = self.trade_quantity
			
		price_str = f"{price:.{self.decimal_places}f}"
		data = {
			"pair": self.market_pair,
			"type": order_type,
			"volume": f"{quantity}",
			"price": price_str,
			"post_only": "true"
		}
		
		for attempt in range(3):
			try:
				r = requests.post(f"{self.api_base}postorder", 
								headers=self.luno_auth_headers(), 
								data=data)
				if r.status_code == 429:  # Rate limited
					wait_time = 2 ** (attempt + 1)
					self.logger.warning(f"Rate limited. Waiting {wait_time}s before retry...")
					time.sleep(wait_time)
					continue
					
				r.raise_for_status()
				response = r.json()
				order_id = response.get("order_id")
				if order_id:
					if order_type == "BID":
						self.active_buy_orders[order_id] = (price, quantity)
					else:
						self.active_sell_orders[order_id] = (price, quantity)
					return order_id
			except Exception as e:
				self.logger.error(f"Attempt {attempt + 1} failed for {order_type} order at {price_str}: {e}")
				if attempt == 2:  # Last attempt failed
					return None
				time.sleep(1)
		return None

	def cancel_order(self, order_id):
		"""Cancel a specific order"""
		try:
			r = requests.post(f"{self.api_base}stoporder", 
							headers=self.luno_auth_headers(), 
							data={"order_id": order_id})
			r.raise_for_status()
			
			# Remove from active orders if found
			if order_id in self.active_buy_orders:
				del self.active_buy_orders[order_id]
			elif order_id in self.active_sell_orders:
				del self.active_sell_orders[order_id]
				
			return True
		except Exception as e:
			self.logger.error(f"Failed to cancel order {order_id}: {e}")
			return False

	def cancel_all_buy_orders(self):
		"""Cancel all active buy orders"""
		try:
			r = requests.get(f"{self.api_base}listorders?state=PENDING", 
							headers=self.luno_auth_headers())
			r.raise_for_status()
			
			for order in r.json().get("orders", []):
				if order["pair"] == self.market_pair and order["type"] == "BID":
					self.cancel_order(order["order_id"])
					
			self.logger.info(f"Cancelled all buy orders for {self.market_pair}")
			return True
		except Exception as e:
			self.logger.error(f"Error while cancelling buy orders: {e}")
			return False

	def get_order_status(self, order_id):
		"""Check the status of a specific order"""
		try:
			r = requests.get(f"{self.api_base}orders/{order_id}", 
						   headers=self.luno_auth_headers())
			r.raise_for_status()
			return r.json()
		except Exception as e:
			self.logger.error(f"Failed to get status for order {order_id}: {e}")
			return None

	def check_filled_orders(self):
		"""Check if any active orders have been filled"""
		filled_orders = []
		
		# Check buy orders
		for order_id in list(self.active_buy_orders.keys()):
			order_info = self.get_order_status(order_id)
			if order_info and order_info.get("state") == "COMPLETE":
				price, quantity = self.active_buy_orders[order_id]
				filled_orders.append(("BUY", order_id, price, quantity))
				del self.active_buy_orders[order_id]
				
		# Check sell orders
		for order_id in list(self.active_sell_orders.keys()):
			order_info = self.get_order_status(order_id)
			if order_info and order_info.get("state") == "COMPLETE":
				price, quantity = self.active_sell_orders[order_id]
				profit = (price * quantity) - (self.active_buy_orders.get(order_id, (0, 0))[0] * quantity)
				self.realized_profit += profit
				filled_orders.append(("SELL", order_id, price, quantity))
				del self.active_sell_orders[order_id]
				
		return filled_orders

	def setup_grid(self, base_price=None):
		"""Setup the initial grid of buy orders"""
		if base_price is None:
			base_price = self.get_current_price()
			if base_price is None:
				self.logger.error("Failed to get current price for grid setup")
				return False
				
		self.cancel_all_buy_orders()
		
		# Calculate buy price (1% below current)
		buy_price = (Decimal(str(base_price)) * (Decimal('1') - Decimal(str(self.grid_percentage))))
		buy_price = float(buy_price.quantize(Decimal('1') / (10 ** self.decimal_places), rounding=ROUND_DOWN))
		
		# Place the buy order
		buy_id = self.place_limit_order("BID", buy_price)
		if buy_id:
			self.logger.info(f"Placed buy order at {buy_price:.{self.decimal_places}f} (ID: {buy_id})")
			return True
		else:
			self.logger.error("Failed to place initial buy order")
			return False

	def handle_filled_buy_order(self, price, quantity):
		"""When a buy order is filled, place a sell order"""
		sell_price = (Decimal(str(price)) * (Decimal('1') + Decimal(str(self.grid_percentage))))
		sell_price = float(sell_price.quantize(Decimal('1') / (10 ** self.decimal_places), rounding=ROUND_UP))
		
		sell_id = self.place_limit_order("ASK", sell_price, quantity)
		if sell_id:
			self.logger.info(f"Placed sell order at {sell_price:.{self.decimal_places}f} (ID: {sell_id})")
			
			# Place a new buy order to maintain the grid
			new_buy_price = (Decimal(str(price)) * (Decimal('1') - Decimal(str(self.grid_percentage))))
			new_buy_price = float(new_buy_price.quantize(Decimal('1') / (10 ** self.decimal_places), rounding=ROUND_DOWN))
			new_buy_id = self.place_limit_order("BID", new_buy_price)
			
			if new_buy_id:
				self.logger.info(f"Replaced buy order at {new_buy_price:.{self.decimal_places}f} (ID: {new_buy_id})")
			else:
				self.logger.error("Failed to replace buy order")
				
			return True
		return False

	def generate_status_report(self):
		"""Generate a status report for Telegram"""
		crypto_balance = self.get_balance(self.currency)
		fiat_balance = self.get_balance(self.fiat_currency)
		current_price = self.get_current_price()
		
		if current_price is None:
			current_price = 0
			
		total_value = fiat_balance + (crypto_balance * current_price)
		
		report = (
			f"<b>{self.market_pair} Status</b>\n"
			f"────────────────\n"
			f"• Current Price: {current_price:.{self.decimal_places}f} {self.fiat_currency}\n"
			f"• {self.currency} Balance: {crypto_balance:.6f}\n"
			f"• {self.fiat_currency} Balance: {fiat_balance:.2f}\n"
			f"• Total Value: {total_value:.2f} {self.fiat_currency}\n"
			f"• Realized Profit: {self.realized_profit:.2f} {self.fiat_currency}\n\n"
			f"<b>Active Buy Orders</b>:\n"
		)
		
		if not self.active_buy_orders:
			report += "None\n"
		else:
			for order_id, (price, qty) in self.active_buy_orders.items():
				report += f"• {price:.{self.decimal_places}f} {self.fiat_currency} ({qty} {self.currency})\n"
				
		report += "\n<b>Active Sell Orders</b>:\n"
		if not self.active_sell_orders:
			report += "None\n"
		else:
			for order_id, (price, qty) in self.active_sell_orders.items():
				report += f"• {price:.{self.decimal_places}f} {self.fiat_currency} ({qty} {self.currency})\n"
				
		return report

	def hourly_reset(self):
		"""Reset the grid every hour"""
		while self.running:
			now = datetime.now()
			next_hour = (now.replace(minute=0, second=0, microsecond=0) + 
						timedelta(hours=1))
			wait_seconds = (next_hour - now).total_seconds()
			
			time.sleep(wait_seconds)
			
			if not self.running:
				break
				
			self.logger.info("Performing hourly reset...")
			current_price = self.get_current_price()
			if current_price:
				self.setup_grid(current_price)
				
			# Send status report
			if self.notifier:
				self.notifier.send_message(self.generate_status_report())

	def run(self):
		"""Main bot execution loop"""
		self.logger.info(f"Starting grid bot for {self.market_pair}")
		
		# Initial setup
		if not self.setup_grid():
			self.logger.error("Initial setup failed. Exiting bot.")
			return
			
		# Start hourly reset thread
		reset_thread = Thread(target=self.hourly_reset, daemon=True)
		reset_thread.start()
		
		# Main trading loop
		while self.running:
			try:
				# Check for filled orders
				filled_orders = self.check_filled_orders()
				
				for side, order_id, price, quantity in filled_orders:
					self.logger.info(f"{side} order filled: {order_id} at {price}")
					
					if side == "BUY":
						self.handle_filled_buy_order(price, quantity)
						
					# Send update to Telegram
					if self.notifier:
						msg = (f"{self.market_pair}: {side} order filled\n"
							  f"Price: {price:.{self.decimal_places}f}\n"
							  f"Quantity: {quantity:.6f} {self.currency}")
						self.notifier.send_message(msg)
				
				# Sleep before next check
				time.sleep(10)
				
			except Exception as e:
				self.logger.error(f"Error in main loop: {e}")
				time.sleep(30)

def signal_handler(sig, frame):
	"""Handle shutdown signal (CTRL+C)"""
	logging.info("Shutdown requested. Cleaning up...")
	shutdown_event.set()
	
	for bot in running_bots:
		bot.running = False
		bot.cancel_all_buy_orders()
		
	logging.info("All bots stopped. Exiting.")
	sys.exit(0)

if __name__ == "__main__":
    # Load configuration
    with open("api.txt") as f:
        config = json.load(f)

    # 4-A 🔑 fetch credentials ────────────────────────────────────────────────
    api_key = os.getenv("LUNO_API_KEY")
    api_secret = os.getenv("LUNO_API_SECRET")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not api_key or not api_secret:
        logging.error("❌  API credentials not found in Secrets or api.txt – aborting.")
        sys.exit(1)

    if "bots" not in config or not config["bots"]:
        logging.error("No bot configurations found in api.txt. Exiting.")
        logging.info("Shutdown failed.")
        sys.exit(1)
        
    # Initialize Telegram notifier
    notifier = TelegramNotifier(bot_token, chat_id)
    
    # Register signal handler
    signal.signal(signal.SIGINT, signal_handler)
    
    bot_configs = config["bots"]

    # Initialize bots for each market pair
    for bot_config in bot_configs:
        bot = GridBot(
            api_key=api_key,
            api_secret=api_secret,
            market_pair=bot_config["market_pair"],
            trade_quantity=bot_config["trade_quantity"],
            grid_percentage=config.get("grid_percentage", 0.017),
            quantity_multiplier=bot_config.get("quantity_multiplier", 1),
            notifier=notifier
        )
        running_bots.append(bot)
        
        # Start bot in a separate thread
        bot_thread = Thread(target=bot.run)
        bot_thread.start()
        
        # Send startup notification
        initial_balance = bot.get_balance(bot.fiat_currency)
        notifier.send_message(
            f"🚀 Starting Grid Bot for {bot.market_pair}\n"
            f"Initial {bot.fiat_currency} balance: {initial_balance:.2f}"
        )
        
        # Small delay between bot startups
        time.sleep(1)
    
    # Keep main thread alive
    try:
        while any(bot.running for bot in running_bots):
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)