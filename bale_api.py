import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests


class BaleAPIError(RuntimeError):
    def __init__(self, method: str, description: str, status_code: Optional[int] = None, payload: Optional[dict] = None):
        super().__init__(f"Bale API error in {method}: {description}")
        self.method = method
        self.description = description
        self.status_code = status_code
        self.payload = payload or {}


class BaleAPI:
    """Small, dependency-light HTTP client for Bale Bot API.

    Bale API endpoint is Telegram-like: https://tapi.bale.ai/bot<TOKEN>/METHOD_NAME
    The client keeps failures explicit but has safe_* helpers for moderation flow.
    """

    def __init__(self, token: str, timeout: int = 35, logger: Optional[logging.Logger] = None):
        if not token or token == "PUT_YOUR_BALE_BOT_TOKEN_HERE":
            raise ValueError("BOT_TOKEN is empty. Create .env from .env.example and put your Bale bot token.")
        self.token = token.strip()
        self.base_url = f"https://tapi.bale.ai/bot{self.token}"
        self.file_base_url = f"https://tapi.bale.ai/file/bot{self.token}"
        self.timeout = timeout
        self.log = logger or logging.getLogger(__name__)
        self.session = requests.Session()

    def call(self, method: str, payload: Optional[Dict[str, Any]] = None, files: Optional[dict] = None, *, timeout: Optional[int] = None) -> Any:
        url = f"{self.base_url}/{method}"
        payload = payload or {}
        try:
            if files:
                resp = self.session.post(url, data=payload, files=files, timeout=timeout or self.timeout)
            else:
                resp = self.session.post(url, json=payload, timeout=timeout or self.timeout)
        except requests.RequestException as exc:
            raise BaleAPIError(method, str(exc)) from exc

        try:
            data = resp.json()
        except Exception as exc:
            raise BaleAPIError(method, f"Non JSON response: HTTP {resp.status_code}", resp.status_code) from exc

        if not data.get("ok"):
            desc = data.get("description") or data.get("error_message") or json.dumps(data, ensure_ascii=False)
            raise BaleAPIError(method, desc, resp.status_code, data)
        return data.get("result")

    def safe_call(self, method: str, payload: Optional[Dict[str, Any]] = None, files: Optional[dict] = None, *, default=None) -> Any:
        try:
            return self.call(method, payload, files)
        except BaleAPIError as exc:
            desc = (exc.description or "").lower()
            # CallbackQuery and PreCheckoutQuery are only answerable for a short time.
            # When old pending updates are flushed/replayed, Bale returns this error.
            # It is harmless and should not flood production logs.
            if method in {"answerCallbackQuery", "answerPreCheckoutQuery"} and (
                "query is too old" in desc
                or "response timeout expired" in desc
                or "query id is invalid" in desc
                or "query ID is invalid" in (exc.description or "")
            ):
                self.log.debug("Ignored expired %s: %s", method, exc.description)
                return default
            self.log.warning("Bale API safe_call failed: %s payload=%s", exc, payload)
            return default
        except Exception as exc:
            self.log.warning("Bale API safe_call failed: %s payload=%s", exc, payload)
            return default

    def get_me(self) -> dict:
        return self.call("getMe")

    def delete_webhook(self) -> Any:
        return self.safe_call("deleteWebhook")

    def get_updates(self, offset: Optional[int] = None, limit: int = 100, timeout: int = 30) -> list:
        payload = {"limit": limit, "timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=timeout + 10) or []

    def send_message(self, chat_id: int | str, text: str, *, reply_to_message_id: Optional[int] = None,
                     reply_markup: Optional[dict] = None, parse_mode: Optional[str] = None) -> Any:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text[:4096]}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self.call("sendMessage", payload)

    def safe_send_message(self, chat_id: int | str, text: str, **kwargs) -> Any:
        return self.safe_call("sendMessage", {k: v for k, v in {
            "chat_id": chat_id,
            "text": text[:4096],
            "reply_to_message_id": kwargs.get("reply_to_message_id"),
            "reply_markup": kwargs.get("reply_markup"),
            "parse_mode": kwargs.get("parse_mode"),
        }.items() if v is not None})

    def edit_message_text(self, chat_id: int | str, message_id: int, text: str, *, reply_markup: Optional[dict] = None) -> Any:
        payload: Dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096]}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("editMessageText", payload)

    def answer_callback_query(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> Any:
        return self.safe_call("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text[:200],
            "show_alert": show_alert,
        })

    def delete_message(self, chat_id: int | str, message_id: int) -> Any:
        return self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def safe_delete_message(self, chat_id: int | str, message_id: int) -> bool:
        return bool(self.safe_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, default=False))

    def ban_chat_member(self, chat_id: int | str, user_id: int | str) -> Any:
        return self.call("banChatMember", {"chat_id": chat_id, "user_id": int(user_id)})

    def unban_chat_member(self, chat_id: int | str, user_id: int | str, only_if_banned: bool = False) -> Any:
        return self.call("unbanChatMember", {"chat_id": chat_id, "user_id": int(user_id), "only_if_banned": only_if_banned})

    def promote_chat_member(self, chat_id: int | str, user_id: int | str, **perms) -> Any:
        payload = {"chat_id": chat_id, "user_id": int(user_id)}
        payload.update(perms)
        return self.call("promoteChatMember", payload)

    def get_chat_member(self, chat_id: int | str, user_id: int | str) -> Optional[dict]:
        return self.safe_call("getChatMember", {"chat_id": chat_id, "user_id": int(user_id)}, default=None)

    def get_chat_administrators(self, chat_id: int | str) -> list:
        return self.safe_call("getChatAdministrators", {"chat_id": chat_id}, default=[]) or []

    def get_chat_members_count(self, chat_id: int | str) -> int:
        return int(self.safe_call("getChatMembersCount", {"chat_id": chat_id}, default=0) or 0)

    def pin_chat_message(self, chat_id: int | str, message_id: int) -> Any:
        return self.call("pinChatMessage", {"chat_id": chat_id, "message_id": message_id})

    def unpin_chat_message(self, chat_id: int | str, message_id: int) -> Any:
        return self.call("unPinChatMessage", {"chat_id": chat_id, "message_id": message_id})

    def export_chat_invite_link(self, chat_id: int | str) -> Optional[str]:
        return self.safe_call("exportChatInviteLink", {"chat_id": chat_id}, default=None)

    def get_file(self, file_id: str) -> Optional[dict]:
        return self.safe_call("getFile", {"file_id": file_id}, default=None)

    def download_file(self, file_path: str, target_path: str | Path) -> bool:
        url = f"{self.file_base_url}/{file_path}"
        try:
            with self.session.get(url, timeout=self.timeout, stream=True) as resp:
                resp.raise_for_status()
                target_path = Path(target_path)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with target_path.open("wb") as fh:
                    for chunk in resp.iter_content(8192):
                        if chunk:
                            fh.write(chunk)
            return True
        except Exception as exc:
            self.log.warning("File download failed: %s", exc)
            return False

    def send_document(self, chat_id: int | str, file_path: str | Path, *, caption: str = "") -> Any:
        path = Path(file_path)
        with path.open("rb") as fh:
            return self.call("sendDocument", {"chat_id": chat_id, "caption": caption[:1024]}, files={"document": (path.name, fh)})

    def send_invoice(self, chat_id: int | str, title: str, description: str, payload: str,
                     provider_token: str, amount_irr: int, label: str = "پرداخت") -> Any:
        return self.call("sendInvoice", {
            "chat_id": chat_id,
            "title": title[:32],
            "description": description[:255],
            "payload": payload[:128],
            "provider_token": provider_token,
            "prices": [{"label": label, "amount": int(amount_irr)}],
        })

    def answer_pre_checkout_query(self, pre_checkout_query_id: str, ok: bool = True, error_message: str = "") -> Any:
        payload: Dict[str, Any] = {"pre_checkout_query_id": pre_checkout_query_id, "ok": ok}
        if not ok:
            payload["error_message"] = error_message[:200]
        return self.safe_call("answerPreCheckoutQuery", payload)


def inline_keyboard(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


def btn(text: str, data: Optional[str] = None, url: Optional[str] = None, copy_text: Optional[str] = None) -> dict:
    b: Dict[str, Any] = {"text": text}
    if data is not None:
        b["callback_data"] = data
    if url is not None:
        b["url"] = url
    if copy_text is not None:
        b["copy_text"] = {"text": copy_text[:256]}
    return b
