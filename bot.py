"""
MAX-бот: зеркало постов из группы A в группу B с подменой строк.
Админка: /admin (ADMIN_USER_IDS из .env).
Верстка: конвертация спанов в HTML (format=html). Цитаты: !! в начале строки.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import config_store
import httpx
import replies as rep
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

API_BASE = "https://platform-api.max.ru"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

WEBHOOK_SECRET_RE = re.compile(r"^[a-zA-Z0-9_-]{5,256}$")


class MoscowFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, MOSCOW_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


handler = logging.StreamHandler()
handler.setFormatter(MoscowFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
root_logger = logging.getLogger()
root_logger.handlers.clear()
root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)
logger = logging.getLogger("MirrorBot")

# ---------- Функции для работы с версткой (конвертация в HTML) ----------

def normalize_text_format(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        inner = raw.get("type") or raw.get("name") or raw.get("value") or raw.get("format")
        if inner is not None:
            return normalize_text_format(inner)
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return None
    s = str(raw).strip().lower().replace("-", "_")
    if s in ("markdown", "md", "mrkdwn"):
        return "markdown"
    if s in ("html", "text_html"):
        return "html"
    return None

def extract_text_format_from_body(body: Dict[str, Any]) -> Optional[str]:
    for key in ("format", "text_format", "textFormat", "parse_mode", "parseMode", "text_style", "textStyle"):
        if key in body:
            fmt = normalize_text_format(body.get(key))
            if fmt:
                return fmt
    return None

def copy_markup_from_body(body: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    raw = body.get("markup")
    if not isinstance(raw, list) or not raw:
        return None
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(dict(item))
    return out or None

def message_body_text_format_markup(
    body: Dict[str, Any],
) -> Tuple[str, Optional[str], Optional[List[Dict[str, Any]]]]:
    text = body.get("text") or ""
    if not isinstance(text, str):
        text = str(text)
    return text, extract_text_format_from_body(body), copy_markup_from_body(body)

def _span_url_from_dict(s: Dict[str, Any]) -> Optional[str]:
    for key in ("url", "link", "href", "uri", "target"):
        v = s.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def _heading_level_from_span(span: Dict[str, Any]) -> int:
    typ = span.get("type", "").lower()
    m = re.search(r"(?:heading[_\s-]?|h)(\d)", typ)
    if m:
        return max(1, min(6, int(m.group(1))))
    for key in ("level", "depth", "size", "header_level"):
        v = span.get(key)
        if v is not None:
            try:
                return max(1, min(6, int(v)))
            except (TypeError, ValueError):
                continue
    if typ in ("heading", "header", "title"):
        return 1
    return 1

def _html_tags_for_span(span: Dict[str, Any]) -> Tuple[str, str]:
    typ = span.get("type", "").lower()
    url = _span_url_from_dict(span)
    if url:
        return (f'<a href="{url}">', "</a>")
    if typ.startswith("heading") or typ in ("header", "title"):
        level = _heading_level_from_span(span)
        return (f"<h{level}>", f"</h{level}>")
    mapping = {
        "emphasized": ("<i>", "</i>"),
        "emphasis": ("<i>", "</i>"),
        "em": ("<i>", "</i>"),
        "italic": ("<i>", "</i>"),
        "strong": ("<b>", "</b>"),
        "bold": ("<b>", "</b>"),
        "strikethrough": ("<s>", "</s>"),
        "underline": ("<u>", "</u>"),
        "code": ("<code>", "</code>"),
        "monospace": ("<code>", "</code>"),
    }
    return mapping.get(typ, ("", ""))

def spans_to_html(text: str, spans: List[Dict[str, Any]]) -> str:
    if not text or not spans:
        return text
    # Сортируем спаны по убыванию from (чтобы обрабатывать от конца к началу)
    sorted_spans = sorted(spans, key=lambda s: s.get("from", 0), reverse=True)
    result = text
    for span in sorted_spans:
        start = span.get("from")
        length = span.get("length")
        if start is None or length is None:
            continue
        if start < 0 or start + length > len(result):
            continue
        open_tag, close_tag = _html_tags_for_span(span)
        if not open_tag:
            continue
        result = result[:start] + open_tag + result[start:start+length] + close_tag + result[start+length:]
    return result

def normalize_outbound_message(
    text: str,
    text_format: Optional[str],
    markup: Optional[List[Dict[str, Any]]],
) -> Tuple[str, Optional[str], Optional[List[Dict[str, Any]]]]:
    if text_format in ("markdown", "html"):
        return text, text_format, markup if markup else None
    if markup:
        html_text = spans_to_html(text, markup)
        if html_text != text:
            return html_text, "html", None
        logger.warning("Конвертация спанов в HTML не изменила текст, отправляем как есть")
        return text, None, markup
    return text, None, None

# ---------- Конец функций для верстки ----------

def normalize_webhook_url(url: str) -> Tuple[str, str]:
    raw = url.strip()
    p = urlparse(raw)
    if p.scheme != "https":
        raise ValueError("WEBHOOK_URL должен начинаться с https://")
    path = (p.path or "").strip()
    if not path or path == "/":
        netloc = p.netloc
        if not netloc:
            raise ValueError("WEBHOOK_URL: не указан хост")
        return f"https://{netloc}/webhook", "/webhook"
    if not path.startswith("/"):
        path = "/" + path
    return f"https://{p.netloc}{path}", path

def parse_listen_host_port(raw: str) -> Tuple[str, int]:
    s = raw.strip()
    if ":" not in s:
        return s, 8000
    host, _, port_s = s.rpartition(":")
    if not host:
        host = "0.0.0.0"
    try:
        port = int(port_s)
    except ValueError:
        raise ValueError(f"Некорректный порт в WEBHOOK_LISTEN: {raw!r}") from None
    return host, port

def parse_admin_ids(raw: Any) -> List[int]:
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else str(raw).split(",")
    result: List[int] = []
    for item in values:
        part = str(item).strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            logger.warning("Пропуск неверного admin id: %s", part)
    return sorted(set(result))

def message_mid_from_callback_update(update: Dict[str, Any]) -> Optional[str]:
    cb = update.get("callback")
    if isinstance(cb, dict):
        for key in ("message", "message_update"):
            mid = _mid_from_message_dict(cb.get(key))
            if mid:
                return mid
    return _mid_from_message_dict(update.get("message"))

def _mid_from_message_dict(msg: Any) -> Optional[str]:
    if not isinstance(msg, dict):
        return None
    body = msg.get("body")
    if isinstance(body, dict):
        for key in ("mid", "message_id", "messageId"):
            raw = body.get(key)
            if raw is not None:
                return str(raw)
    for key in ("mid", "message_id", "messageId"):
        raw = msg.get(key)
        if raw is not None:
            return str(raw)
    return None

async def max_subscribe_webhook(client: httpx.AsyncClient, url: str, secret: Optional[str]) -> None:
    payload: Dict[str, Any] = {
        "url": url,
        "update_types": ["message_created", "message_callback", "bot_started"],
    }
    if secret:
        payload["secret"] = secret
    r = await client.post("/subscriptions", json=payload)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(data.get("message") or "POST /subscriptions вернул success=false")

async def max_unsubscribe_webhook(client: httpx.AsyncClient, url: str) -> None:
    r = await client.delete("/subscriptions", params={"url": url})
    r.raise_for_status()

class AdminState(Enum):
    NONE = "none"
    AWAITING_SOURCE = "awaiting_source"
    AWAITING_TARGET = "awaiting_target"
    AWAITING_REPLACE_SEARCH = "awaiting_replace_search"
    AWAITING_REPLACE_REPLACE = "awaiting_replace_replace"

def clean_media_attachments(attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    drop = ("callback_id", "size", "width", "height", "duration")
    clean: List[Dict[str, Any]] = []
    for item in attachments:
        if item.get("type") == "inline_keyboard":
            continue
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        safe_payload = {k: v for k, v in payload.items() if k not in drop}
        clean.append({"type": item.get("type"), "payload": safe_payload})
    return clean

def apply_replacements(text: str, rules: List[Tuple[str, str]]) -> str:
    out = text or ""
    for search, replace in rules:
        if search:
            out = out.replace(search, replace)
    return out

def apply_replacements_deep(obj: Any, rules: List[Tuple[str, str]]) -> Any:
    if isinstance(obj, str):
        return apply_replacements(obj, rules)
    if isinstance(obj, dict):
        return {k: apply_replacements_deep(v, rules) for k, v in obj.items()}
    if isinstance(obj, list):
        return [apply_replacements_deep(x, rules) for x in obj]
    return obj

def normalize_max_url(url: str) -> str:
    u = (url or "").strip()
    if not u.startswith("http"):
        u = "https://" + u
    return u.rstrip("/")

def extract_join_token(url: str) -> str:
    m = re.search(r"/join/([^/?#]+)", url, re.IGNORECASE)
    return m.group(1) if m else ""

def links_match(a: str, b: str) -> bool:
    return normalize_max_url(a).lower() == normalize_max_url(b).lower()

def try_parse_chat_id_from_text(text: str) -> Optional[int]:
    raw = text.strip()
    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except ValueError:
            return None
    m = re.search(r"/c/(-?\d+)", raw)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None

def _chat_title_from_api(d: Optional[Dict[str, Any]]) -> str:
    if not d or not isinstance(d, dict):
        return ""
    inner = d.get("chat")
    u = inner if isinstance(inner, dict) else d
    return str(u.get("title") or u.get("name") or "").strip()

def _prompt_cancel_keyboard(payload: str) -> List[Dict]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [[{"type": "callback", "text": rep.BTN_CANCEL, "payload": payload}]]
            },
        }
    ]

class MirrorConfig:
    def __init__(self, db_path: str | None = None):
        raw = (os.environ.get("SQLITE_PATH") or "").strip()
        self.db_path = db_path or (raw or os.path.join("data", "app.db"))
        self.reload()

    def reload(self) -> None:
        state = config_store.load_state(self.db_path)
        self.source_chat_id: Optional[int] = state.get("source_chat_id")
        self.target_chat_id: Optional[int] = state.get("target_chat_id")
        self.replacements: List[Dict[str, Any]] = list(state.get("replacements") or [])

    def enabled_rules(self) -> List[Tuple[str, str]]:
        return [
            (r["search_text"], r["replace_text"])
            for r in self.replacements
            if r.get("enabled") and r.get("search_text")
        ]

    def set_source(self, chat_id: int) -> None:
        config_store.save_chat_ids(self.db_path, source_chat_id=chat_id)
        self.reload()

    def set_target(self, chat_id: int) -> None:
        config_store.save_chat_ids(self.db_path, target_chat_id=chat_id)
        self.reload()

    def add_replacement(self, search: str, replace: str) -> None:
        config_store.add_replacement(self.db_path, search, replace)
        self.reload()

    def toggle_replacement(self, repl_id: int) -> bool:
        ok = config_store.toggle_replacement(self.db_path, repl_id)
        self.reload()
        return ok

    def delete_replacement(self, repl_id: int) -> bool:
        ok = config_store.delete_replacement(self.db_path, repl_id)
        self.reload()
        return ok

class MirrorBot:
    def __init__(self, token: str, config: MirrorConfig):
        self.token = token
        self.config = config
        self.root_admin_ids = parse_admin_ids(os.environ.get("ADMIN_USER_IDS", ""))
        self.headers = {"Authorization": self.token}
        self.client = httpx.AsyncClient(base_url=API_BASE, headers=self.headers, timeout=60.0)
        self.bot_id: int | None = None
        self.admin_states: Dict[int, AdminState] = {}
        self._pending_replace_search: Dict[int, str] = {}

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.root_admin_ids

    async def get_me(self) -> None:
        r = await self.client.get("/me")
        r.raise_for_status()
        data = r.json()
        self.bot_id = data.get("user_id")
        logger.info("Logged in as bot ID %s (@%s)", self.bot_id, data.get("username"))

    async def send_message(
        self,
        chat_id: int,
        text: str,
        attachments: Optional[List[Dict]] = None,
        text_format: Optional[str] = None,
        markup: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict]:
        try:
            text, text_format, markup = normalize_outbound_message(text, text_format, markup)
            payload: Dict[str, Any] = {"text": text}
            if text_format in ("markdown", "html"):
                payload["format"] = text_format
            if markup:
                payload["markup"] = markup
            if attachments:
                payload["attachments"] = attachments
            params = {"user_id": chat_id} if chat_id > 0 else {"chat_id": chat_id}
            r = await self.client.post("/messages", params=params, json=payload)
            r.raise_for_status()
            return r.json().get("message")
        except Exception as e:
            logger.error("POST /messages to %s failed: %s", chat_id, e)
            return None

    async def edit_message(
        self,
        message_id: str,
        text: str,
        attachments: Optional[List[Dict]] = None,
        text_format: Optional[str] = None,
        markup: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        try:
            text, text_format, markup = normalize_outbound_message(text, text_format, markup)
            payload: Dict[str, Any] = {"text": text}
            if text_format in ("markdown", "html"):
                payload["format"] = text_format
            if markup:
                payload["markup"] = markup
            if attachments is not None:
                payload["attachments"] = attachments
            r = await self.client.put("/messages", params={"message_id": message_id}, json=payload)
            r.raise_for_status()
            return True
        except Exception as e:
            logger.error("PUT /messages %s failed: %s", message_id, e)
            return False

    async def show_menu_or_edit(
        self,
        user_id: int,
        text: str,
        attachments: Optional[List[Dict]] = None,
        *,
        edit_message_id: Optional[str] = None,
    ) -> None:
        if edit_message_id and await self.edit_message(edit_message_id, text, attachments):
            return
        await self.send_message(user_id, text, attachments)

    async def fetch_chat_by_id(self, chat_id: int) -> Optional[Dict[str, Any]]:
        try:
            r = await self.client.get(f"/chats/{chat_id}")
            if r.status_code != 200:
                return None
            data = r.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.error("GET /chats/%s: %s", chat_id, e)
            return None

    async def find_chat_by_invite_url(self, url: str) -> tuple[Optional[int], Optional[Dict[str, Any]], str]:
        norm = normalize_max_url(url)
        token = extract_join_token(norm)
        marker: int | None = None
        while True:
            params: Dict[str, Any] = {"count": 100}
            if marker is not None:
                params["marker"] = marker
            try:
                r = await self.client.get("/chats", params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                return None, None, rep.chat_list_fetch_error(str(e))
            chats = data.get("chats") or []
            if not isinstance(chats, list):
                chats = []
            for c in chats:
                if not isinstance(c, dict):
                    continue
                cid = c.get("chat_id")
                clink = (c.get("link") or "").strip()
                if clink and links_match(clink, norm):
                    return int(cid) if cid is not None else None, c, ""
                if token and clink and extract_join_token(clink) == token:
                    return int(cid) if cid is not None else None, c, ""
            next_m = data.get("marker")
            if next_m is None or not chats:
                break
            try:
                marker = int(next_m)
            except (TypeError, ValueError):
                break
        return None, None, rep.CHAT_NOT_IN_BOT_LIST

    async def resolve_chat_from_input(self, text: str) -> tuple[Optional[int], Optional[Dict[str, Any]], str]:
        raw = text.strip()
        if not raw:
            return None, None, rep.EMPTY_INPUT
        maybe_id = try_parse_chat_id_from_text(raw)
        if maybe_id is not None:
            info = await self.fetch_chat_by_id(maybe_id)
            if info:
                return maybe_id, info, ""
            return None, None, rep.chat_not_found_by_id(maybe_id)
        if not raw.startswith("http"):
            raw = "https://" + raw.lstrip("/")
        return await self.find_chat_by_invite_url(raw)

    async def chat_label_for_id(self, chat_id: Optional[int]) -> str:
        if chat_id is None:
            return rep.ADMIN_NOT_SET
        info = await self.fetch_chat_by_id(chat_id)
        return rep.chat_label(chat_id, _chat_title_from_api(info))

    def _reset_admin_fsm(self, user_id: int) -> None:
        self.admin_states[user_id] = AdminState.NONE
        self._pending_replace_search.pop(user_id, None)

    async def send_admin_menu(
        self,
        user_id: int,
        *,
        edit_message_id: Optional[str] = None,
        prepend: Optional[str] = None,
    ) -> None:
        src_label = await self.chat_label_for_id(self.config.source_chat_id)
        tgt_label = await self.chat_label_for_id(self.config.target_chat_id)
        lines = [
            rep.ADMIN_MENU_TITLE,
            "",
            rep.ADMIN_STATUS_HEADER,
            rep.ADMIN_SOURCE_LINE.format(label=src_label),
            rep.ADMIN_TARGET_LINE.format(label=tgt_label),
            rep.ADMIN_REPLACEMENTS_COUNT.format(n=len(self.config.replacements)),
        ]
        if prepend:
            lines = [prepend, ""] + lines
        buttons = [
            [{"type": "callback", "text": rep.BTN_SET_SOURCE, "payload": "adm_set_source"}],
            [{"type": "callback", "text": rep.BTN_SET_TARGET, "payload": "adm_set_target"}],
            [{"type": "callback", "text": rep.BTN_REPLACEMENTS, "payload": "adm_replacements"}],
        ]
        await self.show_menu_or_edit(
            user_id,
            "\n".join(lines),
            [{"type": "inline_keyboard", "payload": {"buttons": buttons}}],
            edit_message_id=edit_message_id,
        )

    async def send_replacements_menu(
        self,
        user_id: int,
        *,
        edit_message_id: Optional[str] = None,
        prepend: Optional[str] = None,
    ) -> None:
        lines = [rep.REPLACEMENTS_HEADER]
        if not self.config.replacements:
            lines.append(rep.REPLACEMENTS_EMPTY)
        else:
            for i, r in enumerate(self.config.replacements, start=1):
                st = rep.replacement_state_word(bool(r.get("enabled")))
                lines.append(
                    rep.REPLACEMENT_LINE.format(
                        n=i,
                        state=st,
                        search=r["search_text"],
                        replace=r["replace_text"],
                    )
                )
        if prepend:
            lines = [prepend, ""] + lines
        buttons: List[List[Dict]] = [
            [{"type": "callback", "text": rep.BTN_ADD_REPLACEMENT, "payload": "adm_add_repl"}],
        ]
        for r in self.config.replacements:
            rid = int(r["id"])
            on = bool(r.get("enabled"))
            toggle = "Выкл" if on else "Вкл"
            label = f"{toggle}: {r['search_text'][:20]}"
            buttons.append(
                [
                    {"type": "callback", "text": label, "payload": f"adm_toggle:{rid}"},
                    {"type": "callback", "text": "Удалить", "payload": f"adm_del:{rid}"},
                ]
            )
        buttons.append([{"type": "callback", "text": rep.BTN_BACK, "payload": "adm_menu"}])
        await self.show_menu_or_edit(
            user_id,
            "\n".join(lines),
            [{"type": "inline_keyboard", "payload": {"buttons": buttons}}],
            edit_message_id=edit_message_id,
        )

    async def mirror_post(self, msg: Dict[str, Any]) -> None:
        source = self.config.source_chat_id
        target = self.config.target_chat_id
        if source is None or target is None:
            logger.warning("Зеркало не настроено: source=%s target=%s", source, target)
            return

        msg_body = msg.get("body") or {}
        if not isinstance(msg_body, dict):
            msg_body = {}
        text, text_fmt, markup = message_body_text_format_markup(msg_body)
        attachments = msg_body.get("attachments") or []
        if not isinstance(attachments, list):
            attachments = []

        logger.info("mirror_post: text_fmt=%s, text_len=%d, attachments=%d, markup=%s",
                    text_fmt, len(text), len(attachments), bool(markup))

        # ===== Эвристика для цитат через !! =====
        lines = text.split('\n')
        new_lines = []
        modified = False
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith('!!'):
                content = line[line.index('!!')+2:].lstrip()
                new_lines.append(f'<blockquote>{content}</blockquote>')
                modified = True
            else:
                new_lines.append(line)
        if modified:
            text = '\n'.join(new_lines)
            text_fmt = "html"
            markup = None   # очищаем markup, чтобы не было конфликта
        # ===== Конец эвристики =====

        if not text.strip() and not attachments:
            logger.info("Пустое сообщение, пропускаем")
            return

        rules = self.config.enabled_rules()
        text = apply_replacements(text, rules)
        if markup:
            markup = apply_replacements_deep(markup, rules)
        clean_attachments = clean_media_attachments(attachments)

        result = await self.send_message(
            target,
            text,
            clean_attachments if clean_attachments else None,
            text_format=text_fmt,
            markup=markup if markup else None,
        )
        if result:
            logger.info("Зеркало: %s → %s, mid=%s", source, target, _mid_from_message_dict(result))
        else:
            logger.error("Зеркало не удалось: %s → %s", source, target)

    async def handle_update(self, update: Dict[str, Any]) -> None:
        ut = update.get("update_type")
        if ut == "message_created":
            await self.on_message_created(update.get("message", {}))
        elif ut == "message_callback":
            await self.on_callback(update)
        elif ut == "bot_started":
            user = update.get("user") or {}
            uid = user.get("user_id")
            if uid:
                await self.send_message(int(uid), rep.START_HINT)

    async def on_message_created(self, msg: Dict[str, Any]) -> None:
        sender = msg.get("sender", {})
        sender_id = int(sender.get("user_id")) if sender.get("user_id") else None
        recipient = msg.get("recipient", {})
        raw_chat_id = recipient.get("chat_id") or recipient.get("chat", {}).get("chat_id")
        chat_id = int(raw_chat_id) if raw_chat_id is not None else None

        if sender_id and self.bot_id and sender_id == self.bot_id:
            return

        if chat_id is not None and chat_id == self.config.source_chat_id:
            await self.mirror_post(msg)
            return

        if sender_id is None:
            return

        body = msg.get("body") or {}
        text = (body.get("text") or "").strip() if isinstance(body, dict) else ""

        if text.startswith("/start"):
            await self.send_message(sender_id, rep.START_HINT)
            return

        if text.startswith("/admin"):
            if not self.is_admin(sender_id):
                await self.send_message(sender_id, rep.MASTER_ONLY_ADMIN_CMD)
                return
            self._reset_admin_fsm(sender_id)
            await self.send_admin_menu(sender_id)
            return

        if self.is_admin(sender_id):
            await self.process_admin_text(sender_id, text)

    async def process_admin_text(self, sender_id: int, text: str) -> None:
        state = self.admin_states.get(sender_id, AdminState.NONE)
        if state == AdminState.AWAITING_SOURCE:
            cid, _, err = await self.resolve_chat_from_input(text)
            if err or cid is None:
                await self.send_message(sender_id, err or rep.EMPTY_INPUT)
                return
            self.config.set_source(cid)
            self._reset_admin_fsm(sender_id)
            await self.send_admin_menu(sender_id, prepend=rep.SOURCE_SAVED.format(cid=cid))
            return

        if state == AdminState.AWAITING_TARGET:
            cid, _, err = await self.resolve_chat_from_input(text)
            if err or cid is None:
                await self.send_message(sender_id, err or rep.EMPTY_INPUT)
                return
            self.config.set_target(cid)
            self._reset_admin_fsm(sender_id)
            await self.send_admin_menu(sender_id, prepend=rep.TARGET_SAVED.format(cid=cid))
            return

        if state == AdminState.AWAITING_REPLACE_SEARCH:
            search = text.strip()
            if not search:
                await self.send_message(sender_id, rep.EMPTY_INPUT)
                return
            self._pending_replace_search[sender_id] = search
            self.admin_states[sender_id] = AdminState.AWAITING_REPLACE_REPLACE
            await self.send_message(
                sender_id,
                rep.PROMPT_REPLACE_REPLACE,
                _prompt_cancel_keyboard("adm_cancel"),
            )
            return

        if state == AdminState.AWAITING_REPLACE_REPLACE:
            replace = text
            search = self._pending_replace_search.get(sender_id, "")
            if not search:
                self._reset_admin_fsm(sender_id)
                await self.send_replacements_menu(sender_id, prepend=rep.MSG_PROMPT_CANCELLED)
                return
            self.config.add_replacement(search, replace)
            self._reset_admin_fsm(sender_id)
            await self.send_replacements_menu(
                sender_id,
                prepend=rep.REPLACEMENT_ADDED.format(search=search, replace=replace),
            )

    async def on_callback(self, update: Dict[str, Any]) -> None:
        cb = update.get("callback", {})
        payload = cb.get("payload")
        user_data = cb.get("user", {})
        sender_id = int(user_data.get("user_id")) if user_data.get("user_id") else None
        if sender_id is None or not payload or not self.is_admin(sender_id):
            return
        callback_mid = message_mid_from_callback_update(update)

        if payload == "adm_menu":
            self._reset_admin_fsm(sender_id)
            await self.send_admin_menu(sender_id, edit_message_id=callback_mid)
        elif payload == "adm_set_source":
            self.admin_states[sender_id] = AdminState.AWAITING_SOURCE
            await self.show_menu_or_edit(
                sender_id,
                rep.PROMPT_SOURCE_CHAT,
                _prompt_cancel_keyboard("adm_cancel"),
                edit_message_id=callback_mid,
            )
        elif payload == "adm_set_target":
            self.admin_states[sender_id] = AdminState.AWAITING_TARGET
            await self.show_menu_or_edit(
                sender_id,
                rep.PROMPT_TARGET_CHAT,
                _prompt_cancel_keyboard("adm_cancel"),
                edit_message_id=callback_mid,
            )
        elif payload == "adm_replacements":
            self._reset_admin_fsm(sender_id)
            await self.send_replacements_menu(sender_id, edit_message_id=callback_mid)
        elif payload == "adm_add_repl":
            self.admin_states[sender_id] = AdminState.AWAITING_REPLACE_SEARCH
            await self.show_menu_or_edit(
                sender_id,
                rep.PROMPT_REPLACE_SEARCH,
                _prompt_cancel_keyboard("adm_cancel"),
                edit_message_id=callback_mid,
            )
        elif payload == "adm_cancel":
            self._reset_admin_fsm(sender_id)
            await self.send_admin_menu(sender_id, edit_message_id=callback_mid, prepend=rep.MSG_PROMPT_CANCELLED)
        elif isinstance(payload, str) and payload.startswith("adm_toggle:"):
            try:
                rid = int(payload.split(":", 1)[1])
            except ValueError:
                return
            if self.config.toggle_replacement(rid):
                r = next((x for x in self.config.replacements if int(x["id"]) == rid), None)
                st = rep.replacement_state_word(bool(r.get("enabled"))) if r else "изменена"
                await self.send_replacements_menu(
                    sender_id,
                    edit_message_id=callback_mid,
                    prepend=rep.REPLACEMENT_TOGGLED.format(state=st),
                )
        elif isinstance(payload, str) and payload.startswith("adm_del:"):
            try:
                rid = int(payload.split(":", 1)[1])
            except ValueError:
                return
            if self.config.delete_replacement(rid):
                await self.send_replacements_menu(
                    sender_id,
                    edit_message_id=callback_mid,
                    prepend=rep.REPLACEMENT_REMOVED,
                )

async def run_webhook_server(
    bot: MirrorBot,
    webhook_url: str,
    webhook_secret: Optional[str],
    listen_host: str,
    listen_port: int,
) -> None:
    await bot.get_me()
    full_url, path = normalize_webhook_url(webhook_url)

    async def webhook_get(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "webhook": True, "detail": rep.WEBHOOK_GET_DETAIL})

    async def on_webhook(request: Request) -> Response:
        if webhook_secret:
            if request.headers.get("X-Max-Bot-Api-Secret") != webhook_secret:
                return Response(status_code=403)
        try:
            body = await request.json()
        except Exception:
            return Response(status_code=400)
        if not isinstance(body, dict):
            return Response(status_code=400)
        logger.info("Webhook POST: update_type=%r", body.get("update_type"))
        try:
            await bot.handle_update(body)
        except Exception:
            logger.exception("handle_update failed")
        return Response(status_code=200)

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    routes = [
        Route(path, webhook_get, methods=["GET"]),
        Route(path, on_webhook, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ]

    class AccessLogMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            if not (request.url.path == "/health" and request.method == "GET"):
                peer = request.client.host if request.client else "?"
                logger.info("HTTP %s %s from %s", request.method, request.url.path, peer)
            return await call_next(request)

    app = Starlette(routes=routes)
    if os.environ.get("WEBHOOK_ACCESS_LOG", "1").strip() not in ("0", "false", "no"):
        app.add_middleware(AccessLogMiddleware)
    uvicorn_log = os.environ.get("LOG_LEVEL", "info").lower()
    server = uvicorn.Server(uvicorn.Config(app, host=listen_host, port=listen_port, log_level=uvicorn_log))
    serve_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    subscribed_ok = False
    try:
        await max_subscribe_webhook(bot.client, full_url, webhook_secret)
        subscribed_ok = True
        logger.info("Webhook: URL=%s, listen %s:%s path=%s", full_url, listen_host, listen_port, path)
        await serve_task
    finally:
        if subscribed_ok:
            try:
                await max_unsubscribe_webhook(bot.client, full_url)
            except Exception as e:
                logger.warning("Отписка webhook: %s", e)

def _sqlite_paths_from_env() -> tuple[str, str]:
    raw_db = (os.environ.get("SQLITE_PATH") or "").strip()
    db_path = raw_db or os.path.join("data", "app.db")
    raw_b = (os.environ.get("SQLITE_BACKUP_DIR") or "").strip()
    backup_dir = raw_b or os.path.join("data", "backups")
    return db_path, backup_dir

async def daily_sqlite_backup_loop() -> None:
    db_path, backup_dir = _sqlite_paths_from_env()
    while True:
        now = datetime.now(MOSCOW_TZ)
        next_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        await asyncio.sleep((next_midnight - now).total_seconds())
        try:
            config_store.backup_now(db_path, backup_dir)
        except Exception:
            logger.exception("Ежедневный дамп SQLite не удался")

async def main() -> None:
    token = os.environ.get("MAX_BOT_TOKEN")
    if not token:
        logger.error("MAX_BOT_TOKEN not found")
        return
    webhook_url = (os.environ.get("WEBHOOK_URL") or "").strip()
    if not webhook_url:
        logger.error("Укажите WEBHOOK_URL")
        return
    webhook_secret_raw = (os.environ.get("WEBHOOK_SECRET") or "").strip()
    webhook_secret: Optional[str] = None
    if webhook_secret_raw:
        if not WEBHOOK_SECRET_RE.match(webhook_secret_raw):
            logger.error("WEBHOOK_SECRET: 5–256 символов [a-zA-Z0-9_-]")
            return
        webhook_secret = webhook_secret_raw
    try:
        listen_host, listen_port = parse_listen_host_port(os.environ.get("WEBHOOK_LISTEN", "0.0.0.0:8000"))
    except ValueError as e:
        logger.error("%s", e)
        return

    bot = MirrorBot(token, MirrorConfig())
    backup_task = asyncio.create_task(daily_sqlite_backup_loop())
    try:
        await run_webhook_server(bot, webhook_url, webhook_secret, listen_host, listen_port)
    finally:
        backup_task.cancel()
        try:
            await backup_task
        except asyncio.CancelledError:
            pass
        await bot.client.aclose()

if __name__ == "__main__":
    asyncio.run(main())