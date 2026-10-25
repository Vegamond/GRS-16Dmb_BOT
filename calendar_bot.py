#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import html
import json
import argparse
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

import requests

KYIV_TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = "state.json"
TG_MAX_LEN = 4096

# Банка «На каву адміну ☕» — блок додається лише в недільний тижневий пост
DONATE_URL = "https://send.monobank.ua/jar/2rWrXwkk7H"
DONATE_LINE = (
    "<blockquote>"
    "☕ <b>Розклад тобі допомагає?</b>\n"
    f'💛 <b><u><a href="{DONATE_URL}">ДОНАТ НА КАВУ АДМІНУ</a></u></b> 👈'
    "</blockquote>"
)


def tg_visible_len(html_text: str) -> int:
    """Довжина так, як її рахує Telegram: без HTML-тегів, сутності як 1 символ,
    у UTF-16 code units (емодзі = 2)."""
    import html as _html
    plain = _html.unescape(re.sub(r"<[^>]+>", "", html_text))
    return len(plain.encode("utf-16-le")) // 2


URL_RE = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE
)

PAIR_BY_START = {
    "09:00": 1,
    "10:40": 2,
    "12:30": 3,
    "14:10": 4,
    "15:40": 5,
}


def pair_no(t: dt.datetime) -> Optional[int]:
    s = t.astimezone(KYIV_TZ).strftime("%H:%M")
    return PAIR_BY_START.get(s)


MOODLE_LINKS = {
    # Наповнити після отримання PDF розкладу ГРС-16Дмб —
    # формат: "Точна назва дисципліни": "https://distance.kuk.edu.ua/..."
    # Посилання на Moodle з опису події має пріоритет над цим словником.
}


def normalize_discipline(name: str) -> str:
    return " ".join((name or "").replace("'", "'").split()).casefold()


MOODLE_LINKS_NORM = {
    normalize_discipline(k): v
    for k, v in MOODLE_LINKS.items()
    if v
}


def ics_unescape(s: str) -> str:
    if not s:
        return ""
    return (s
            .replace(r"\n", "\n")
            .replace(r"\N", "\n")
            .replace(r"\,", ",")
            .replace(r"\;", ";")
            .replace(r"\\", "\\"))


def html_to_text(s: str) -> str:
    """Google Calendar зберігає опис, набраний у своєму редакторі, як HTML
    (<br>, <a href>, <b> ...). Повертаємо звичайні рядки, щоб «Код:» і «Викл.:»
    знаходились на початку рядка, а посилання бралось зі справжнього href."""
    if not s or "<" not in s:
        return s
    s = re.sub(r'<a\s[^>]*href="([^"]+)"[^>]*>.*?</a>', r' \1 ', s, flags=re.I | re.S)
    s = re.sub(r'<br\s*/?>|</p>|</div>|</li>', '\n', s, flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    s = html.unescape(s).replace('\u00a0', ' ')
    return "\n".join(line.strip() for line in s.splitlines())


@dataclass
class Event:
    start: dt.datetime
    end: dt.datetime
    summary: str
    description: str
    location: str


def now_kyiv() -> dt.datetime:
    return dt.datetime.now(tz=KYIV_TZ)


def iso_date(d: dt.date) -> str:
    return d.isoformat()


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def should_post(state: Dict, key: str, stamp: str) -> bool:
    last = state.get(key)
    return last != stamp


def mark_posted(state: Dict, key: str, stamp: str) -> None:
    state[key] = stamp


def env_required(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def env_optional_int(name: str) -> Optional[int]:
    v = os.getenv(name, "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def is_saturday(day: dt.date) -> bool:
    return day.weekday() == 5


def is_sunday(day: dt.date) -> bool:
    return day.weekday() == 6


def fetch_ics(url: str, timeout_s: int = 30) -> str:
    resp = requests.get(url, timeout=timeout_s)
    resp.raise_for_status()
    return resp.text


def _unfold_ics_lines(ics_text: str) -> List[str]:
    raw = ics_text.splitlines()
    out = []
    for line in raw:
        if not line:
            out.append(line)
            continue
        if line.startswith(" ") or line.startswith("\t"):
            if out:
                out[-1] += line[1:]
            else:
                out.append(line.lstrip())
        else:
            out.append(line)
    return out


def _parse_dt(value: str, tzid: Optional[str]) -> dt.datetime:
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):
        d = dt.datetime.strptime(value, "%Y%m%d").date()
        return dt.datetime(
            d.year, d.month, d.day, 0, 0,
            tzinfo=ZoneInfo(tzid) if tzid else KYIV_TZ
        )
    if value.endswith("Z"):
        base = dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        return base.astimezone(KYIV_TZ)
    naive = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
    tz = ZoneInfo(tzid) if tzid else KYIV_TZ
    return naive.replace(tzinfo=tz).astimezone(KYIV_TZ)


def parse_ics_events(ics_text: str) -> List[Event]:
    lines = _unfold_ics_lines(ics_text)
    events: List[Event] = []
    in_event = False
    cur: Dict[str, Tuple[Optional[str], str]] = {}

    def flush():
        nonlocal cur
        if not cur:
            return
        dtstart_tz, dtstart_val = cur.get("DTSTART", (None, ""))
        dtend_tz, dtend_val = cur.get("DTEND", (None, ""))
        summary_raw = cur.get("SUMMARY", (None, ""))[1]
        description_raw = cur.get("DESCRIPTION", (None, ""))[1]
        location_raw = cur.get("LOCATION", (None, ""))[1]
        if not dtstart_val or not dtend_val:
            cur = {}
            return
        start = _parse_dt(dtstart_val, dtstart_tz)
        end = _parse_dt(dtend_val, dtend_tz)
        summary = ics_unescape(summary_raw).strip()
        description = html_to_text(ics_unescape(description_raw)).strip()
        location = html_to_text(ics_unescape(location_raw)).strip()
        events.append(Event(start=start, end=end, summary=summary,
                            description=description, location=location))
        cur = {}

    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event = True
            cur = {}
            continue
        if line == "END:VEVENT":
            if in_event:
                flush()
            in_event = False
            continue
        if not in_event:
            continue
        if ":" not in line:
            continue
        left, value = line.split(":", 1)
        key = left
        tzid = None
        if ";" in left:
            key, params = left.split(";", 1)
            m = re.search(r"TZID=([^;]+)", params)
            if m:
                tzid = m.group(1)
        key = key.strip().upper()
        value = value.strip()
        if key in {"DTSTART", "DTEND", "SUMMARY", "DESCRIPTION", "LOCATION"}:
            cur[key] = (tzid, value)

    events.sort(key=lambda e: e.start)
    return events


def events_in_range(events: List[Event], start_date: dt.date, end_date: dt.date) -> List[Event]:
    out = []
    for ev in events:
        d = ev.start.astimezone(KYIV_TZ).date()
        if start_date <= d <= end_date:
            out.append(ev)
    return out


UA_DOW = {
    0: "Понеділок", 1: "Вівторок", 2: "Середа",
    3: "Четвер", 4: "Пʼятниця", 5: "Субота", 6: "Неділя",
}

RKR_TYPE = "Ректорська контрольна робота"


def detect_type(tail: str) -> Optional[str]:
    t = tail.strip().lower()
    # РКР перевіряємо першою — «контрольна» не повинна потрапити в інші типи
    if "ректор" in t or re.search(r"\bркр\b", t):
        return RKR_TYPE
    if "лекц" in t:
        return "Лекція"
    if "практ" in t or t == "пр." or t == "пр":
        return "Практичне"
    if "лаб" in t:
        return "Лабораторна"
    if "семінар" in t:
        return "Семінар"
    if "консультація" in t:
        return "Консультація"
    if "іспит" in t:
        return "Іспит"
    if "залік" in t:
        return "Залік"
    if "екзамен" in t:
        return "Екзамен"
    return None


# Заліки/іспити — виділений блок, лише аудиторія (без Zoom)
EXAM_TYPES = {"Залік", "Іспит", "Екзамен"}
# Усі типи, що показуються виділеним блоком з рамкою і йдуть першими
HIGHLIGHT_TYPES = EXAM_TYPES | {RKR_TYPE}


def split_summary(summary: str) -> Tuple[str, Optional[str]]:
    s = summary.strip()
    if "—" in s:
        left, right = s.rsplit("—", 1)
        etype = detect_type(right)
        if etype:
            return left.strip(), etype
    if " - " in s:
        left, right = s.rsplit(" - ", 1)
        etype = detect_type(right)
        if etype:
            return left.strip(), etype
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", s)
    if m:
        base = m.group(1).strip()
        tail = m.group(2).strip()
        etype = detect_type(tail)
        if etype:
            return base, etype
    return s, None


# ───────────────────────── Посилання ─────────────────────────

MEETING_KINDS = ("zoom", "meet", "teams")
MEETING_NAMES = {"zoom": "Zoom", "meet": "Google Meet", "teams": "Teams"}
LINK_LABELS = {
    "zoom": "🔗 <a href=\"{u}\">Відкрити Zoom</a>",
    "meet": "🔗 <a href=\"{u}\">Відкрити Google Meet</a>",
    "teams": "🔗 <a href=\"{u}\">Відкрити Teams</a>",
    "moodle": "📘 <a href=\"{u}\">Відкрити Moodle</a>",
    "other": "🔗 <a href=\"{u}\">Посилання</a>",
}


def _unwrap_google_redirect(url: str) -> str:
    """Google Calendar іноді обгортає посилання в google.com/url?q=..."""
    try:
        p = urlparse(url)
        if p.netloc.endswith("google.com") and p.path == "/url":
            qs = parse_qs(p.query)
            target = (qs.get("q") or qs.get("url") or [None])[0]
            if target and target.startswith("http"):
                return target
    except Exception:
        pass
    return url


def _clean_url(url: str) -> str:
    url = url.rstrip(".,;:!?)]}»\"'")
    return _unwrap_google_redirect(url)


def classify_link(url: str) -> str:
    u = url.lower()
    if "zoom.us" in u:
        return "zoom"
    if "meet.google.com" in u:
        return "meet"
    if "teams.microsoft" in u or "teams.live" in u:
        return "teams"
    if "moodle" in u or "distance.kuk.edu.ua" in u:
        return "moodle"
    return "other"


def extract_links(text: str) -> List[Tuple[str, str]]:
    """Усі посилання з тексту як [(тип, url)], без дублів, у порядку появи."""
    if not text:
        return []
    t = text.replace("\\n", "\n").replace("\u200b", "")
    out: List[Tuple[str, str]] = []
    seen = set()
    for raw in URL_RE.findall(t):
        u = _clean_url(raw)
        if u in seen:
            continue
        seen.add(u)
        out.append((classify_link(u), u))
    return out


# ───────────────────────── Викладач ─────────────────────────

# Префікси: довші форми стоять першими, а lookahead не дає «викл»
# зачепити початок слова «викладач».
TEACHER_PREFIX_RE = re.compile(
    r"^(?:ст\.?\s*викл\.?|старший\s+викладач|викладач|викл\.?|доцент|доц\.?|"
    r"професор|проф\.?|асистент|асист\.?|лектор|піб)"
    r"(?![а-яіїєґʼ'’])\s*[:\-–—]?\s*(.+)$",
    re.IGNORECASE,
)

_NAME_WORD = r"[А-ЯІЇЄҐ][а-яіїєґʼ'’\-]+"
_INITIALS = r"[А-ЯІЇЄҐ]\.\s?[А-ЯІЇЄҐ]\.?"
# Рядок, що складається лише з ПІБ:
#   «Іваненко І.П.», «І.П. Іваненко», «Іваненко Ірина Петрівна»
PIB_RE = re.compile(
    rf"^(?:{_NAME_WORD}\s+{_INITIALS}|{_INITIALS}\s+{_NAME_WORD}|"
    rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{2}})$"
)


def extract_teacher(description: str) -> Optional[str]:
    if not description:
        return None
    lines = [l.strip() for l in description.splitlines() if l.strip()]

    # 1) Явний префікс: «Викладач: ...», «доц. ...», «ПІБ: ...»
    for line in lines:
        m = TEACHER_PREFIX_RE.match(line)
        if m:
            val = URL_RE.sub("", m.group(1)).strip(" ,;:-–—")
            if val:
                return val

    # 2) Рядок, який сам по собі є ПІБ (без префікса)
    for line in lines:
        if URL_RE.search(line) or re.search(r"\d", line):
            continue
        cand = line.strip(" ,;")
        if PIB_RE.match(cand):
            return cand
    return None


def extract_passcode(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.replace("\\n", "\n")
    patterns = [
        r"(?:Код\s*доступу|Код\s*доступа|Passcode|Пароль)\s*[:=\-]\s*([^\s,;]+)",
        r"(?:^|\n)\s*Код\s*[:=\-]\s*([^\s,;]+)",
    ]
    for pat in patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


AUD_RE = re.compile(r"(ауд\.?\s*\d+)", re.IGNORECASE)


def classify_place(location: str, description: str,
                   meeting_kind: Optional[str] = None) -> str:
    blob = f"{location}\n{description}".lower()
    aud = AUD_RE.search(blob)
    online = bool(meeting_kind) or "online" in blob or "онлайн" in blob or "zoom" in blob
    if online:
        name = MEETING_NAMES.get(meeting_kind) or ("Zoom" if "zoom" in blob else None)
        label = f"🌐 Online ({name})" if name else "🌐 Online"
        if aud:
            return f"{label} • 🏫 {aud.group(1).strip()}"
        return label
    if aud:
        return f"🏫 {aud.group(1).strip()}"
    loc = URL_RE.sub("", location).strip(" ,;\n")
    if loc:
        return f"📍 {loc}"
    return "📍 (місце не вказано)"


def get_weather_dnipro(day: dt.date) -> Optional[Dict]:
    lat, lon = 48.45, 34.98
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Europe%2FKyiv"
    )
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        dates = data.get("daily", {}).get("time", [])
        if not dates:
            return None
        if day.isoformat() not in dates:
            return None
        idx = dates.index(day.isoformat())
        wcode = data["daily"]["weathercode"][idx]
        tmax = data["daily"]["temperature_2m_max"][idx]
        tmin = data["daily"]["temperature_2m_min"][idx]
        p = data["daily"]["precipitation_probability_max"][idx]
        return {
            "desc": weathercode_ua(wcode),
            "tmin": int(round(tmin)),
            "tmax": int(round(tmax)),
            "p": int(p) if p is not None else None,
        }
    except Exception:
        return None


def weathercode_ua(code: int) -> str:
    mapping = {
        0: "ясно", 1: "переважно ясно", 2: "мінлива хмарність", 3: "хмарно",
        45: "туман", 48: "паморозь / туман",
        51: "мряка", 53: "мряка", 55: "мряка",
        61: "дощ", 63: "дощ", 65: "сильний дощ",
        66: "крижаний дощ", 67: "крижаний дощ",
        71: "сніг", 73: "сніг", 75: "сильний сніг", 77: "снігова крупа",
        80: "зливи", 81: "зливи", 82: "сильні зливи",
        85: "снігопад", 86: "сильний снігопад",
        95: "гроза", 96: "гроза з градом", 99: "гроза з градом",
    }
    return mapping.get(code, f"погода (код: {code})")


def format_weather_block(day: dt.date, label: str) -> str:
    w = get_weather_dnipro(day)
    if not w:
        return ""
    lines = [
        f"⛅ Погода в Дніпрі на {label}:",
        f"• {w['desc']}",
        f"• 🌡️ Мін/Макс: {w['tmin']}°C / {w['tmax']}°C",
    ]
    if w.get("p") is not None:
        lines.append(f"• ☔ Ймовірність опадів: {w['p']}%")
    return "\n".join(lines) + "\n\n"


def hhmm(t: dt.datetime) -> str:
    return t.astimezone(KYIV_TZ).strftime("%H:%M")


def fmt_date_short(d: dt.date) -> str:
    return d.strftime("%d.%m")


def day_header(d: dt.date) -> str:
    dow = UA_DOW[d.weekday()]
    return f"📅 <b>{dow}</b> • <b>{fmt_date_short(d)}</b>"


def separator() -> str:
    return "━━━━━━━━━━━━━━━━━━━━"


def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_html_attr(s: str) -> str:
    return escape_html(s).replace('"', "&quot;")


def link_line(kind: str, url: str) -> str:
    return LINK_LABELS[kind].format(u=escape_html_attr(url))


def format_day(events: List[Event], day: dt.date) -> str:
    lines = []
    lines.append(day_header(day))
    lines.append("")

    if not events:
        lines.append("— (пар немає)")
        return "\n".join(lines)

    # Заліки/іспити/РКР завжди першими, потім решта пар за часом
    top = [ev for ev in events if split_summary(ev.summary)[1] in HIGHLIGHT_TYPES]
    rest = [ev for ev in events if split_summary(ev.summary)[1] not in HIGHLIGHT_TYPES]
    sorted_events = top + rest

    for ev in sorted_events:
        discipline, etype = split_summary(ev.summary)
        text_blob = ev.description + "\n" + ev.location
        teacher = extract_teacher(ev.description)
        passcode = extract_passcode(text_blob + "\n" + ev.summary)
        links = extract_links(text_blob)

        meeting = next(((k, u) for k, u in links if k in MEETING_KINDS), None)
        moodle_url = (next((u for k, u in links if k == "moodle"), None)
                      or MOODLE_LINKS_NORM.get(normalize_discipline(discipline)))
        other_links = [u for k, u in links if k == "other"][:2]

        is_exam = etype in EXAM_TYPES
        is_rkr = etype == RKR_TYPE
        highlighted = is_exam or is_rkr

        if highlighted:
            lines.append(separator())
            icon = "🛑" if is_rkr else "📝"
            lines.append(f"{icon} <b>{etype.upper()}!</b>")
            if is_rkr:
                pno = pair_no(ev.start)
                pfx = f"{pno} пара " if pno else ""
                lines.append(f"🕒 <b>{pfx}{hhmm(ev.start)}–{hhmm(ev.end)}</b>")
            else:
                lines.append(f"🕒 <b>{hhmm(ev.start)}–{hhmm(ev.end)}</b>")
        else:
            pno = pair_no(ev.start)
            pfx = f"{pno} пара " if pno else ""
            lines.append(f"🕒 <b>{pfx}{hhmm(ev.start)}–{hhmm(ev.end)}</b>")

        lines.append(f"📚 <b>{escape_html(discipline)}</b>")

        if moodle_url:
            lines.append(link_line("moodle", moodle_url))

        if etype and not highlighted:
            lines.append(f"🎓 {etype}")

        if teacher:
            lines.append(f"👩\u200d🏫 {escape_html(teacher)}")

        if is_exam:
            # Для заліків/іспитів — тільки аудиторія, без Zoom
            aud = AUD_RE.search(f"{ev.location}\n{ev.description}".lower())
            if aud:
                lines.append(f"🏫 {aud.group(1).strip()}")
        else:
            place = classify_place(ev.location, ev.description,
                                   meeting[0] if meeting else None)
            # Якщо є лише Moodle і більше нічого — «місце не вказано» не пишемо
            if not (moodle_url and place == "📍 (місце не вказано)"):
                lines.append(escape_html(place))
            if meeting:
                lines.append(link_line(meeting[0], meeting[1]))
            for u in other_links:
                lines.append(link_line("other", u))
            if passcode:
                lines.append("🔑 Код доступу:")
                lines.append(f"📎 <code>{escape_html(passcode)}</code>")

        if highlighted:
            lines.append(separator())

        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


def format_week_message(events: List[Event], start_day: dt.date, end_day: dt.date) -> str:
    header = (
        f"🗓️ <b>Розклад на тиждень</b>\n"
        f"<b>{fmt_date_short(start_day)} – {fmt_date_short(end_day)}</b>\n\n"
    )
    by_day: Dict[dt.date, List[Event]] = {
        start_day + dt.timedelta(days=i): []
        for i in range((end_day - start_day).days + 1)
    }
    for ev in events:
        by_day[ev.start.astimezone(KYIV_TZ).date()].append(ev)
    blocks = []
    for d in by_day.keys():
        blocks.append(separator())
        blocks.append(format_day(by_day[d], d))
    blocks.append(separator())
    return header + "\n".join(blocks) + f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"


def tg_send_message(
    token: str,
    chat_id: str,
    text: str,
    message_thread_id: Optional[int] = None,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["today", "tomorrow", "week"], help="Posting mode")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force post even if weekday restrictions would normally skip it"
    )
    args = parser.parse_args()

    token = env_required("TG_BOT_TOKEN")
    chat_id = env_required("TG_CHAT_ID")
    ics_url = env_required("GCAL_ICS_URL")
    schedule_thread_id = env_optional_int("TG_SCHEDULE_THREAD_ID")

    state = load_state()
    ics = fetch_ics(ics_url)
    all_events = parse_ics_events(ics)
    today = now_kyiv().date()

    if args.mode == "today":
        if is_saturday(today) or is_sunday(today):
            print("Skipping 'today': no today-posts on Saturday or Sunday.")
            return
        target = today
        stamp = f"today:{iso_date(target)}"
        if not should_post(state, "last_today", stamp):
            print("Already posted today schedule for this date. Exiting.")
            return
        day_events = events_in_range(all_events, target, target)
        weather = format_weather_block(target, "сьогодні")
        msg = "<b>Доброго ранку шановні студенти!</b> ☀️\n\n" + weather
        msg += f"🗓️ <b>Розклад на сьогодні ({fmt_date_short(target)})</b>\n\n"
        msg += format_day(day_events, target)
        msg += f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"
        tg_send_message(token, chat_id, msg, message_thread_id=None)
        mark_posted(state, "last_today", stamp)
        save_state(state)
        print("Posted today schedule.")

    elif args.mode == "tomorrow":
        if is_saturday(today):
            print("Skipping 'tomorrow': no posts on Saturday.")
            return
        target = today + dt.timedelta(days=1)
        stamp = f"tomorrow:{iso_date(target)}"
        if not should_post(state, "last_tomorrow", stamp):
            print("Already posted tomorrow schedule for this date. Exiting.")
            return
        day_events = events_in_range(all_events, target, target)
        weather = format_weather_block(target, "завтра")
        msg = "<b>Добрий вечір шановні студенти!</b> 🌙\n\n" + weather
        msg += f"🗓️ <b>Розклад на завтра ({fmt_date_short(target)})</b>\n\n"
        msg += format_day(day_events, target)
        msg += f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"
        tg_send_message(token, chat_id, msg, message_thread_id=None)
        mark_posted(state, "last_tomorrow", stamp)
        save_state(state)
        print("Posted tomorrow schedule.")

    elif args.mode == "week":
        if not args.force and not is_sunday(today):
            print("Skipping 'week': weekly post should run on Sunday only.")
            return
        this_monday = today - dt.timedelta(days=today.weekday())
        if args.force:
            week_start = this_monday
            week_end = week_start + dt.timedelta(days=6)
            print("Force mode enabled: posting current week.")
        else:
            week_start = this_monday + dt.timedelta(days=7)
            week_end = week_start + dt.timedelta(days=6)
            print("Regular Sunday mode: posting next week.")
        stamp = f"week:{iso_date(week_start)}:{iso_date(week_end)}"
        if not should_post(state, "last_week", stamp):
            print("Already posted weekly schedule for this week-range. Exiting.")
            return
        week_events = events_in_range(all_events, week_start, week_end)
        msg = format_week_message(week_events, week_start, week_end)
        if schedule_thread_id is None:
            print("WARNING: TG_SCHEDULE_THREAD_ID not set. Weekly post will go to general chat.")
        tg_send_message(token, chat_id, msg, message_thread_id=schedule_thread_id)
        # Банку постимо окремим повідомленням — так вона завжди доходить,
        # незалежно від того, наскільки довгий вийшов розклад на тиждень.
        tg_send_message(token, chat_id, DONATE_LINE, message_thread_id=schedule_thread_id)
        mark_posted(state, "last_week", stamp)
        save_state(state)
        print("Posted weekly schedule + donate message.")


if __name__ == "__main__":
    main()
