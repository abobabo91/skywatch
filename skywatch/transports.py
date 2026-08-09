"""Getting a message onto a phone. Telegram for subscribers, ntfy as a side channel."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, render

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# ntfy silently converts a body over ~4096 bytes into a file attachment, which
# destroys the notification. Budget below that and count bytes, not characters --
# accented text is multi-byte.
NTFY_BYTE_CEILING = 3900


def telegram_call(token: str, method: str, payload: dict, timeout: int = 30) -> dict | None:
    url = TELEGRAM_API.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            # 429 carries a retry_after; anything else is not worth hammering.
            if exc.code == 429:
                try:
                    wait = json.loads(body)["parameters"]["retry_after"]
                except Exception:
                    wait = 3
                time.sleep(min(wait, 30))
                continue
            log.error("telegram %s failed: HTTP %s %s", method, exc.code, body[:300])
            return None
        except (urllib.error.URLError, TimeoutError) as exc:
            log.warning("telegram %s network error (%s), attempt %d", method, exc, attempt + 1)
            time.sleep(2 * (attempt + 1))
    return None


def send_telegram(token: str, chat_id: str | int, html: str,
                  reply_markup: dict | None = None,
                  disable_notification: bool = False) -> bool:
    payload = {
        "chat_id": chat_id,
        "text": html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": disable_notification,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    result = telegram_call(token, "sendMessage", payload)
    return bool(result and result.get("ok"))


def send_ntfy(topic: str, body: str, title: str = "Skywatch",
              priority: str = "default", click: str | None = None) -> bool:
    """Post to ntfy.sh. Body is plain text; markup is stripped by the caller."""
    encoded = body.encode("utf-8")
    if len(encoded) > NTFY_BYTE_CEILING:
        encoded = encoded[:NTFY_BYTE_CEILING].decode("utf-8", "ignore").encode("utf-8")
        encoded += b"\n[...truncated]"

    # urllib encodes headers as latin-1, so a stray em-dash in the title raises
    # UnicodeEncodeError mid-POST and the notification never fires.
    try:
        title.encode("latin-1")
    except UnicodeEncodeError:
        title = title.encode("ascii", "replace").decode("ascii")

    headers = {"Title": title, "Priority": priority,
               "Content-Type": "text/plain; charset=utf-8"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=encoded,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, TimeoutError) as exc:
        log.error("ntfy post to %s failed: %s", topic, exc)
        return False


def deliver(subscriber: dict, html: str, title: str = "Skywatch",
            priority: str = "default") -> bool:
    """Send one rendered message to whichever channels this subscriber uses."""
    ok = False
    token = config.telegram_token()
    if subscriber["transport"] == "telegram" and token:
        ok = send_telegram(token, subscriber["target"], html)
    elif subscriber["transport"] == "ntfy":
        ok = send_ntfy(subscriber["target"], render.to_plaintext(html), title, priority)

    # A Telegram subscriber can additionally mirror to an ntfy topic.
    mirror = subscriber["settings"].get("ntfy_topic")
    if mirror and subscriber["transport"] != "ntfy":
        send_ntfy(mirror, render.to_plaintext(html), title, priority)
    return ok
