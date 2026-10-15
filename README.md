# Rehan Web Hosting

Panel for hosting web apps + telegram bots. SQLite locally, Supabase on Render.

## Termux local test
    pkg install python
    pip install -r requirements.txt
    uvicorn main:app --host 127.0.0.1 --port 8000 --reload
    # open http://127.0.0.1:8000  (admin / admin123)

## Render deploy
1. Push this folder to a GitHub repo.
2. Render → New → Web Service → connect repo.
3. Environment: Docker.
4. Env vars:
       SECRET_KEY=<random 64 chars>
       ADMIN_USER=admin
       ADMIN_PASSWORD=<your password>
       DB_MODE=supabase
       DATABASE_URL=<supabase postgres uri>
       PUBLIC_URL=https://your-app.onrender.com
       RENDER_API_KEY=<render api key>
5. Deploy. First boot creates tables + admin.

## Supabase
Project → Settings → Database → Connection string → URI. Paste into DATABASE_URL.
Alternatively run schema.sql in Supabase SQL editor.

## Bot code contract
Bot source must define:
    async def handle_update(update):
        ...
Optional: `log("message")` is injected for panel-side logging.

## Downloads
    /download/bot/<id>     → current bot source
    /download/app/<id>     → app config zip
    /download/backup       → full panel JSON snapshot
