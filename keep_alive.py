# keep_alive.py
from flask import Flask
from threading import Thread
import os

app = Flask(__name__)

@app.route("/")
def home():
    return "Luno Grid-Bot is alive!"

def _run():
    port = int(os.environ.get("PORT", 8080))  # ← use Render-assigned port
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    Thread(target=_run, daemon=True).start()
