# Livia Bot (Postgres)

Render (Web Service - Free) + Neon/Supabase Postgres.

Build:  pip install -r requirements.txt
Start:  python bot.py

ENV VARS:
- DISCORD_TOKEN = <your bot token>
- DATABASE_URL  = postgres://user:pass@host:port/db?sslmode=require

Commands: /create, /sheet, /skill_add, /roll, /damage, /heal, /wallet, /shop, /buy, /inventory, /gm_give, /gm_additem, /gm_backup, /gm_restore
