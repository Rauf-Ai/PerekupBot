from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import text
from app.config.settings import get_settings
from app.db.session import SessionLocal

app = FastAPI(title="PerekupBot", version="0.1.0")


@app.get("/health")
def health():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}


@app.get("/telegram-login", response_class=HTMLResponse)
def telegram_login_page():
    return """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход Telegram</title><style>body{font:18px system-ui;text-align:center;margin:24px}img{width:min(88vw,560px);image-rendering:pixelated}</style>
<h2>Telegram → Настройки → Устройства → Подключить устройство</h2><p>Отсканируйте QR в официальном приложении Telegram.</p>
<img id="qr" alt="Ожидание QR для входа"><script>const q=document.querySelector('#qr');function refresh(){q.src='/telegram-login.png?t='+Date.now()}refresh();setInterval(refresh,2000)</script></html>"""


@app.get("/telegram-login.png")
def telegram_login_qr():
    path = Path(get_settings().telegram_session_path).parent / "telegram-login.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Telegram QR login is not running")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})
