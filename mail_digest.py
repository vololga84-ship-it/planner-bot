"""Выжимка новой почты: Mail.ru «Входящие» с подпапками + собранные в Mail.ru ящики Gmail.

Бот зовёт make_digest(since, until) по расписанию (9/12/15/18/21 по времени
владельца) и отправляет результат в Telegram. Почта читается только на чтение
(SELECT readonly, BODY.PEEK) — письма не помечаются прочитанными.
"""
import base64, email, imaplib, json, logging, re, time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

import requests

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.mail.ru"
BODY_CHARS = 700        # сколько текста письма отдаём модели
BATCH = 8               # писем на один запрос к Groq (бесплатный тариф ограничен по токенам в минуту)


def _utf7_decode(name):
    """Имена папок IMAP в modified UTF-7 (&BBAEOwQ+BEA- → Алор)."""
    def repl(m):
        s = m.group(1)
        if not s:
            return "&"
        s = s.replace(",", "/")
        return base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", repl, name)


def _folders(box):
    """INBOX, его подпапки и ящики Gmail, собранные в Mail.ru."""
    out = []
    for raw in box.list()[1]:
        line = raw.decode()
        m = re.match(r'\((.*?)\) "(.*?)" (.+)$', line)
        if not m:
            continue
        name = m.group(3).strip('"')
        if name == "INBOX" or name.startswith("INBOX/") or name.lower().endswith("@gmail.com"):
            out.append(name)
    return out


def _hdr(v):
    try:
        return str(make_header(decode_header(v or ""))).strip()
    except Exception:
        return (v or "").strip()


def _text(msg):
    plain, html = "", ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            s = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if part.get_content_type() == "text/plain" and not plain:
            plain = s
        elif part.get_content_type() == "text/html" and not html:
            html = s
    if not plain and html:
        html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        plain = re.sub(r"<[^>]+>", " ", html)
    plain = re.sub(r"[͏​-‏­]", "", plain)      # невидимые символы рассылок
    plain = re.sub(r"https?://\S+", "", plain)
    return re.sub(r"\s+", " ", plain).strip()[:BODY_CHARS]


def _attachments(msg):
    names = []
    for part in msg.walk():
        fn = part.get_filename()
        if fn:
            fn = _hdr(fn)
            if not re.fullmatch(r"image\d+\.(png|jpe?g|gif)", fn, re.I):   # картинки из подписи
                names.append(fn)
    return names


def fetch_letters(user, password, since, until):
    """Письма с датой получения в (since, until]. since/until — aware datetime."""
    box = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=60)
    box.login(user, password)
    letters = []
    try:
        for folder in _folders(box):
            typ, _ = box.select(f'"{folder}"', readonly=True)
            if typ != "OK":
                continue
            day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
            typ, data = box.search(None, "SINCE", day)
            ids = data[0].split() if typ == "OK" and data and data[0] else []
            for mid in ids:
                # сначала только дата получения — тело (с вложениями) качаем лишь для писем из окна
                typ, d = box.fetch(mid, "(INTERNALDATE)")
                got = imaplib.Internaldate2tuple(d[0]) if typ == "OK" and d and d[0] else None
                if not got:
                    continue
                received = datetime.fromtimestamp(time.mktime(got), tz=timezone.utc)
                if not (since < received <= until):
                    continue
                typ, d = box.fetch(mid, "(BODY.PEEK[])")
                if typ != "OK" or not d or not isinstance(d[0], tuple):
                    continue
                msg = email.message_from_bytes(d[0][1])
                name, addr = parseaddr(_hdr(msg.get("From")))
                letters.append({
                    "folder": _utf7_decode(folder).replace("INBOX/", "").replace("INBOX", "Входящие"),
                    "from": name or addr,
                    "addr": addr,
                    "subject": _hdr(msg.get("Subject")) or "(без темы)",
                    "text": _text(msg),
                    "files": _attachments(msg),
                    "received": received,
                })
    finally:
        try:
            box.logout()
        except Exception:
            pass
    letters.sort(key=lambda x: x["received"])
    return letters


PROMPT = """Ты помогаешь Ольге разбирать почту. Для каждого письма ниже напиши очень краткую выжимку по-русски.
Верни JSON: {"items": [{"i": номер, "kind": "person" | "service" | "newsletter", "important": true/false, "summary": "..."}]}
- kind: person — письмо от живого человека или с работы; service — счёт, чек, код, уведомление сервиса, банк, госуслуги, доставка; newsletter — рассылка, реклама, новости, акции.
- important: true только если нужно действие или есть срок/деньги/документ (оплатить, подтвердить, забрать, ответить, данные готовы, ссылка истекает). Рекламные «успейте купить» — не важные.
- summary: одна строка до 120 символов, только суть: что случилось и что сделать, с суммами и датами если есть. Без «В письме говорится», без приветствий. Если есть вложения — назови их. Для рассылки — главная новость или предложение.

Письма:
"""


def _summarize(batch, groq_key, model):
    lines = []
    for n, x in enumerate(batch):
        files = f" Вложения: {', '.join(x['files'])}." if x["files"] else ""
        lines.append(f"[{n}] От: {x['from']} <{x['addr']}> | Папка: {x['folder']} | Тема: {x['subject']}.{files}\nТекст: {x['text']}")
    for attempt in range(4):
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": PROMPT + "\n\n".join(lines)}],
                "max_tokens": 1500,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "reasoning_effort": "none",
                "reasoning_format": "hidden",
            },
            timeout=60,
        )
        if resp.status_code != 429 or attempt == 3:
            break
        m = re.search(r"try again in ([\d.]+)s", resp.text)
        time.sleep(min(float(resp.headers.get("retry-after") or (m.group(1) if m else 20)) + 1, 60))
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    raw = raw[raw.find("{"): raw.rfind("}") + 1]
    return {int(it["i"]): it for it in json.loads(raw).get("items", []) if str(it.get("i", "")).isdigit()}


def make_digest(user, password, groq_key, model, since, until, tz):
    """Готовый текст выжимки или None, если новых писем нет."""
    letters = fetch_letters(user, password, since, until)
    if not letters:
        return None
    for start in range(0, len(letters), BATCH):
        batch = letters[start:start + BATCH]
        if start:
            time.sleep(5)   # не упираться в поминутный лимит Groq
        try:
            res = _summarize(batch, groq_key, model)
        except Exception:
            logger.exception("Почта: Groq не ответил, отдаю письма без выжимки")
            res = {}
        for n, x in enumerate(batch):
            it = res.get(n) or {}
            x["kind"] = it.get("kind") if it.get("kind") in ("person", "service", "newsletter") else "service"
            x["important"] = bool(it.get("important"))
            x["summary"] = (it.get("summary") or x["subject"]).strip()

    def line(x):
        box = f" ({x['folder']})" if "@gmail.com" in x["folder"] else ""
        return f"• {x['from']}{box} — {x['summary']}"

    t0, t1 = since.astimezone(tz), until.astimezone(tz)
    parts = [f"📬 Почта {t0:%H:%M}–{t1:%H:%M} · писем: {len(letters)}"]
    sections = [
        ("❗ Важное", [x for x in letters if x["important"]]),
        ("✉️ Письма", [x for x in letters if not x["important"] and x["kind"] == "person"]),
        ("🔔 Сервисы", [x for x in letters if not x["important"] and x["kind"] == "service"]),
        ("📰 Рассылки", [x for x in letters if not x["important"] and x["kind"] == "newsletter"]),
    ]
    for title, items in sections:
        if items:
            parts.append(title + "\n" + "\n".join(line(x) for x in items))
    return "\n\n".join(parts)
