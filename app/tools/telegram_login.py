import asyncio
import argparse
import getpass
from pathlib import Path
import sys
import qrcode
from telethon import TelegramClient, errors
from app.config.settings import get_settings


async def main():
    parser = argparse.ArgumentParser(description="Authorize the Telegram source account")
    parser.add_argument("--qr", action="store_true", help="Use Telegram QR login instead of a phone code")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH first")
    Path(settings.telegram_session_path).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(settings.telegram_session_path, settings.telegram_api_id, settings.telegram_api_hash)
    try:
        if args.qr:
            await client.connect()
            if not await client.is_user_authorized():
                login = await client.qr_login()
                while True:
                    print("\033[2J\033[H", end="")
                    print("Telegram → Настройки → Устройства → Подключить устройство → отсканируйте QR")
                    code = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
                    code.add_data(login.url)
                    image_path = Path(settings.telegram_session_path).parent / "telegram-login.png"
                    code.make_image(fill_color="black", back_color="white").save(image_path)
                    code.print_ascii(out=sys.stdout, tty=True)
                    try:
                        await login.wait()
                        break
                    except asyncio.TimeoutError:
                        await login.recreate()
                    except errors.SessionPasswordNeededError:
                        await client.sign_in(password=getpass.getpass("Облачный пароль Telegram: "))
                        break
        else:
            await client.start()
        me = await client.get_me()
        print(f"Telegram session authorized for user {me.id}")
    finally:
        await client.disconnect()
        if args.qr:
            (Path(settings.telegram_session_path).parent / "telegram-login.png").unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
