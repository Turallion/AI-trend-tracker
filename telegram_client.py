from typing import Optional
import requests


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send_message(self, text: str) -> None:
        requests.post(
            f"{self.base_url}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": False,
            },
            timeout=20,
        ).raise_for_status()

    def send_photo_with_caption(self, photo_url: str, caption: str) -> None:
        requests.post(
            f"{self.base_url}/sendPhoto",
            json={
                "chat_id": self.chat_id,
                "photo": photo_url,
                "caption": caption[:1024],
            },
            timeout=20,
        ).raise_for_status()
