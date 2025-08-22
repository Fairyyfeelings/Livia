from flask import Flask
from threading import Thread
import os

app = Flask(__name__)

@app.get("/")
def home():
    return "Livia Bot is alive."

def run():
    # Use the port Render assigns; default to 10000 for local runs
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

def start():
    t = Thread(target=run, daemon=True)
    t.start()
