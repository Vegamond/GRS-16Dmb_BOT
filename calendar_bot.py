#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import subprocess
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

# Банка «На каву адміну ☕» — окреме повідомлення з кнопкою після тижневого посту
DONATE_URL = "https://send.monobank.ua/jar/2rWrXwkk7H"
# Номер картки банки (monobank → банка → «Поповнити з картки іншого банку»).
# Порожній рядок — кнопка копіювання не показується.
DONATE_CARD = "4874 1000 3360 9226"
DONATE_LINE = (
    "☕ <b>Розклад тобі допомагає?</b>\n\n"
    "Бот працює щодня, а підтримує його адмін у вільний час.\n"
    "Якщо розклад стає в пригоді — пригости адміна кавою 💛"
)
if DONATE_CARD:
    DONATE_LINE += (
        "\n\n💳 Немає монобанку? Скопіюй номер картки банки "
        "і переказуй з будь-якого банку."
    )


def donate_button() -> Dict:
    rows = [[{"text": "☕ Банка monobank", "url": DONATE_URL}]]
    if DONATE_CARD:
        # Кнопка копіює номер у буфер обміну одним дотиком
        rows.append([{"text": "📋 Скопіювати номер картки",
                      "copy_text": {"text": DONATE_CARD.replace(" ", "")}}])
    return {"inline_keyboard": rows}


DONATE_BUTTON = donate_button()


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


MOODLE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "moodle.json")


def load_moodle_links() -> Dict[str, str]:
    """Посилання на курси Moodle для цієї групи — у moodle.json поруч з ботом:
    {"Назва дисципліни": "https://distance.kuk.edu.ua/..."}.
    Ключі, що починаються з «_», бот ігнорує (туди можна складати чернетки).
    Посилання на Moodle з опису події має пріоритет над цим файлом."""
    try:
        with open(MOODLE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"WARNING: moodle.json is broken, Moodle links skipped: {e}")
        return {}
    return {k: v for k, v in data.items()
            if isinstance(v, str) and v and not k.startswith("_")}


MOODLE_LINKS = load_moodle_links()

ZOOM_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zoom.json")


def load_zoom_overrides() -> Dict[str, str]:
    """Підміна посилання на пару, якщо в календарі воно неправильне.
    zoom.json поруч з ботом: {"частина назви дисципліни": "https://...zoom.us/j/..."}.
    Збіг — якщо назва дисципліни МІСТИТЬ ключ (без урахування регістру).
    Ключі, що починаються з «_», ігноруються."""
    try:
        with open(ZOOM_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"WARNING: zoom.json is broken, overrides skipped: {e}")
        return {}
    return {normalize_discipline(k): v for k, v in data.items()
            if isinstance(v, str) and v.startswith("http") and not k.startswith("_")}


def normalize_discipline(name: str) -> str:
    return " ".join((name or "").replace("’", "'").replace("ʼ", "'").split()).casefold()


MOODLE_LINKS_NORM = {
    normalize_discipline(k): v
    for k, v in MOODLE_LINKS.items()
    if v
}
ZOOM_OVERRIDES = load_zoom_overrides()


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
    if "гостьова" in t:
        return "Гостьова Лекція"
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


# Заліки/іспити — виділений блок без номера пари
EXAM_TYPES = {"Залік", "Іспит", "Екзамен"}
# Усі типи, що показуються виділеним блоком з рамкою
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
    # «Маркетинг-Лекція» без пробілів (як у старому форматі)
    if "-" in s:
        left, right = s.rsplit("-", 1)
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


AUD_RE = re.compile(r"ауд\.?\s*(\d+)", re.IGNORECASE)


def fmt_aud(m: "re.Match") -> str:
    return f"ауд. {m.group(1)}"


def classify_place(location: str, description: str,
                   meeting_kind: Optional[str] = None) -> str:
    blob = f"{location}\n{description}".lower()
    aud = AUD_RE.search(blob)
    online = bool(meeting_kind) or "online" in blob or "онлайн" in blob or "zoom" in blob
    if online:
        name = MEETING_NAMES.get(meeting_kind) or ("Zoom" if "zoom" in blob else None)
        label = f"🌐 Online ({name})" if name else "🌐 Online"
        if aud:
            return f"{label} • 🏫 {fmt_aud(aud)}"
        return label
    if aud:
        return f"🏫 {fmt_aud(aud)}"
    loc = URL_RE.sub("", location).strip(" ,;\n")
    if loc:
        return f"📍 {loc}"
    return "📍 (місце не вказано)"


MORNING_HOUR = 8  # о котрій студенти виходять з дому — під цю годину і температура


def get_weather_dnipro(day: dt.date) -> Optional[Dict]:
    lat, lon = 48.45, 34.98
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,wind_speed_10m_max"
        "&hourly=temperature_2m"
        "&wind_speed_unit=ms"
        "&timezone=Europe%2FKyiv"
    )
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        if day.isoformat() not in dates:
            return None
        idx = dates.index(day.isoformat())

        # Температура зранку (о MORNING_HOUR), якщо немає — нічний мінімум
        tmorning = daily["temperature_2m_min"][idx]
        hourly = data.get("hourly", {})
        key = f"{day.isoformat()}T{MORNING_HOUR:02d}:00"
        if key in hourly.get("time", []):
            v = hourly["temperature_2m"][hourly["time"].index(key)]
            if v is not None:
                tmorning = v

        p = daily.get("precipitation_probability_max", [None] * (idx + 1))[idx]
        wind = daily.get("wind_speed_10m_max", [None] * (idx + 1))[idx]
        return {
            "code": daily["weathercode"][idx],
            "tmorning": int(round(tmorning)),
            "tmax": int(round(daily["temperature_2m_max"][idx])),
            "p": int(p) if p is not None else None,
            "wind": float(wind) if wind is not None else None,
        }
    except Exception:
        return None


def weathercode_ua(code: int) -> Tuple[str, str]:
    """(іконка, опис) за кодом WMO."""
    mapping = {
        0: ("☀️", "ясно"), 1: ("🌤️", "переважно ясно"), 2: ("⛅", "мінлива хмарність"),
        3: ("☁️", "хмарно"),
        45: ("🌫️", "туман"), 48: ("🌫️", "туман з памороззю"),
        51: ("🌦️", "мряка"), 53: ("🌦️", "мряка"), 55: ("🌦️", "мряка"),
        56: ("🌧️", "крижана мряка"), 57: ("🌧️", "крижана мряка"),
        61: ("🌧️", "дощ"), 63: ("🌧️", "дощ"), 65: ("🌧️", "сильний дощ"),
        66: ("🌧️", "крижаний дощ"), 67: ("🌧️", "крижаний дощ"),
        71: ("🌨️", "сніг"), 73: ("🌨️", "сніг"), 75: ("❄️", "сильний сніг"),
        77: ("🌨️", "снігова крупа"),
        80: ("🌦️", "зливи"), 81: ("🌧️", "зливи"), 82: ("⛈️", "сильні зливи"),
        85: ("🌨️", "снігопад"), 86: ("❄️", "сильний снігопад"),
        95: ("⛈️", "гроза"), 96: ("⛈️", "гроза з градом"), 99: ("⛈️", "гроза з градом"),
    }
    return mapping.get(code, ("🌡️", "погода"))


SNOW_CODES = {71, 73, 75, 77, 85, 86}
ICE_CODES = {56, 57, 66, 67}


def weather_hints(w: Dict) -> List[str]:
    """Підказки — тільки коли вони справді потрібні (не більше двох)."""
    hints = []
    code = w["code"]
    if code in ICE_CODES:
        hints.append("🧊 Ожеледиця — обережно на дорогах")
    elif code in SNOW_CODES:
        hints.append("❄️ Сніг — обирай взуття, що не ковзає")
    elif w.get("p") is not None and w["p"] >= 40:
        hints.append(f"☔ Візьми парасольку — опади до {w['p']}%")
    if w["tmorning"] <= 0:
        hints.append("🧣 Зранку мороз — одягнися тепліше")
    if w.get("wind") is not None and w["wind"] >= 10:
        hints.append(f"💨 Сильний вітер — до {int(round(w['wind']))} м/с")
    return hints[:2]


def fmt_temp(t: int) -> str:
    return f"+{t}°C" if t > 0 else f"{t}°C"


def format_weather_block(day: dt.date, label: str, w: Optional[Dict] = None) -> str:
    if w is None:
        w = get_weather_dnipro(day)
    if not w:
        return ""
    icon, desc = weathercode_ua(w["code"])
    lines = [
        f"{icon} <b>{label.capitalize()} в Дніпрі</b> — {desc}",
        f"🌅 Зранку <b>{fmt_temp(w['tmorning'])}</b> · вдень до <b>{fmt_temp(w['tmax'])}</b>",
    ] + weather_hints(w)
    # Цитата — кольорова смужка й підсвічений фон, погода відділяється від розкладу
    return "<blockquote>" + "\n".join(lines) + "</blockquote>\n\n"


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


# Іконки типів занять
TYPE_ICONS = {
    "Лекція": "📖",
    "Гостьова Лекція": "🎤",
    "Практичне": "✍️",
    "Лабораторна": "🧪",
    "Семінар": "🗣️",
    "Консультація": "💬",
}

# Короткі підписи посилань для рядка «📘 Moodle · 🔗 Zoom»
LINK_SHORT = {"zoom": "▶️", "meet": "▶️", "teams": "▶️", "moodle": "📘", "other": "🔗"}
LINK_TEXT = {
    "zoom": "Підключитися до Zoom",
    "meet": "Підключитися до Meet",
    "teams": "Підключитися до Teams",
    "moodle": "Відкрити курс у Moodle",
    "other": "Відкрити посилання",
}

NO_PLACE = "📍 (місце не вказано)"


def link_html(kind: str, url: str) -> str:
    # Іконка теж усередині посилання, а текст довгий — більша зона натискання
    return f'<a href="{escape_html_attr(url)}"><b>{LINK_SHORT[kind]} {LINK_TEXT[kind]} ›</b></a>'


def short_place(location: str, description: str, meeting_kind: Optional[str]) -> str:
    """Місце для компактного блоку. Якщо є посилання на зустріч, назву сервісу
    не дублюємо (вона й так на посиланні): просто «🌐 Online»."""
    place = classify_place(location, description, meeting_kind)
    if meeting_kind:
        place = re.sub(r"^🌐 Online \([^)]*\)", "🌐 Online", place)
    return place


def nb(html_text: str) -> str:
    """Пробіли поза HTML-тегами → нерозривні: пункт переноситься цілим."""
    return re.sub(r"(<[^>]*>)|( )", lambda m: m.group(1) or " ", html_text)


def event_links(ev: Event):
    """(зустріч, moodle, інші) для події — спільне для тексту й кнопок."""
    discipline, _ = split_summary(ev.summary)
    links = extract_links(ev.description + "\n" + ev.location)
    meeting = next(((k, u) for k, u in links if k in MEETING_KINDS), None)
    # Підміна з zoom.json має перевагу над посиланням з календаря
    disc_norm = normalize_discipline(discipline)
    for key, url in ZOOM_OVERRIDES.items():
        if key and key in disc_norm:
            meeting = (classify_link(url) if classify_link(url) in MEETING_KINDS else "zoom", url)
            break
    moodle_url = (next((u for k, u in links if k == "moodle"), None)
                  or MOODLE_LINKS_NORM.get(disc_norm))
    other_links = [u for k, u in links if k == "other"][:2]
    return meeting, moodle_url, other_links


def format_event(ev: Event, with_links: bool = True) -> List[str]:
    """Компактний блок однієї пари (4 рядки)."""
    discipline, etype = split_summary(ev.summary)
    text_blob = ev.description + "\n" + ev.location
    teacher = extract_teacher(ev.description)
    passcode = extract_passcode(text_blob + "\n" + ev.summary)
    meeting, moodle_url, other_links = event_links(ev)
    # Zoom підмінено з zoom.json — код з календаря належить старому посиланню
    if meeting and meeting[1] in ZOOM_OVERRIDES.values():
        passcode = None

    is_exam = etype in EXAM_TYPES
    is_rkr = etype == RKR_TYPE
    highlighted = is_exam or is_rkr

    lines: List[str] = []
    pno = pair_no(ev.start)
    time_txt = f"{hhmm(ev.start)}–{hhmm(ev.end)}"

    # 1) Час
    if highlighted:
        lines.append(separator())
        lines.append(f"{'🛑' if is_rkr else '📝'} <b>{etype.upper()}!</b>")
    if pno and not is_exam:
        lines.append(nb(f"🕒 <b>{pno} пара · {time_txt}</b>"))
    else:
        lines.append(nb(f"🕒 <b>{time_txt}</b>"))

    # 2) Дисципліна, під нею — її курс у Moodle (подалі від Zoom)
    lines.append(f"📚 <b>{escape_html(discipline)}</b>")
    if with_links and moodle_url:
        lines.append(nb(link_html("moodle", moodle_url)))

    # 3) Тип · місце
    place = short_place(ev.location, ev.description, meeting[0] if meeting else None)
    kind_where = []
    if etype and not highlighted:
        kind_where.append(nb(f"{TYPE_ICONS.get(etype, '🎓')} {etype}"))
    if not (place == NO_PLACE and (moodle_url or meeting)):
        kind_where.extend(nb(escape_html(p.strip())) for p in place.split("•"))
    if kind_where:
        joined = " · ".join(kind_where)
        # На телефоні в рядок влазить ~26 символів. Якщо тип + місце довші —
        # місце окремим рядком, щоб «·» не повисала в кінці рядка.
        if len(kind_where) > 1 and tg_visible_len(joined) > 26 and etype and not highlighted:
            lines.append(kind_where[0])
            lines.append(" · ".join(kind_where[1:]))
        else:
            lines.append(joined)

    # 4) Викладач
    if teacher:
        lines.append(nb(f"👩‍🏫 {escape_html(teacher)}"))

    # 5) Zoom/Meet і інші посилання — внизу, над кодом
    tools = []
    if not with_links:
        moodle_url_txt, meeting_txt, other_txt = None, None, []
    else:
        moodle_url_txt, meeting_txt, other_txt = moodle_url, meeting, other_links
    if meeting_txt:
        tools.append(nb(link_html(meeting_txt[0], meeting_txt[1])))
    for u in other_txt:
        tools.append(nb(link_html("other", u)))
    # кожне посилання окремим рядком — щоб на телефоні не влучити не туди
    lines.extend(tools)

    # 6) Код — одразу під своїм Zoom, з підписом
    if passcode:
        lines.append(f"🔑 Код: <code>{escape_html(passcode)}</code>")

    if highlighted:
        lines.append(separator())
    return lines


def sort_events(events: List[Event]) -> List[Event]:
    """Заліки/іспити — першими; решта (і РКР) — за часом."""
    top = [ev for ev in events if split_summary(ev.summary)[1] in EXAM_TYPES]
    rest = [ev for ev in events if split_summary(ev.summary)[1] not in EXAM_TYPES]
    return top + rest


def format_events(events: List[Event], with_links: bool = True) -> str:
    return "\n\n".join("\n".join(format_event(ev, with_links)) for ev in sort_events(events))


def format_day(events: List[Event], day: dt.date, with_links: bool = True) -> str:
    """День для ранкового/вечірнього посту — повністю розгорнутий."""
    out = day_header(day) + "\n\n"
    if not events:
        return out + "— (пар немає)"
    return out + format_events(events, with_links)


def plural_pairs(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} пара"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} пари"
    return f"{n} пар"


def day_summary(events: List[Event]) -> str:
    """«3 пари · 09:00–13:50 · 🛑 РКР» — підсумок дня в тижневому пості."""
    if not events:
        return "вільний день 🎉"
    evs = sorted(events, key=lambda e: e.start)
    parts = [plural_pairs(len(evs)), f"{hhmm(evs[0].start)}–{hhmm(max(e.end for e in evs))}"]
    types = {split_summary(e.summary)[1] for e in evs}
    if RKR_TYPE in types:
        parts.append("🛑 РКР")
    for t in ("Залік", "Іспит", "Екзамен"):
        if t in types:
            parts.append(f"📝 {t.lower()}")
    return " · ".join(parts)


def format_week_day(events: List[Event], day: dt.date) -> str:
    """День у тижневому пості: заголовок з підсумком + деталі в розгортній цитаті."""
    head = f"📅 <b>{UA_DOW[day.weekday()]} · {fmt_date_short(day)}</b> — {day_summary(events)}"
    if not events:
        return head
    # У цитаті менше місця — короткий роздільник, щоб не переносився на телефоні
    body = format_events(events).replace(separator(), "━" * 12)
    return f"{head}\n<blockquote expandable>{body}</blockquote>"


def format_week_messages(events: List[Event], start_day: dt.date, end_day: dt.date) -> List[str]:
    """Тижневий розклад як список повідомлень. Кожен день — заголовок з підсумком
    і розгортна цитата з деталями. Якщо все не влазить у ліміт Telegram (4096),
    ділимо по днях — день ніколи не розривається посередині."""
    rng = f"{fmt_date_short(start_day)} – {fmt_date_short(end_day)}"
    title = f"🗓️ <b>Розклад на тиждень · {rng}</b>\n<i>Торкнись блоку під днем, щоб розгорнути пари 👇</i>\n\n"
    cont_title = f"🗓️ <b>Розклад на тиждень · {rng}</b> (продовження)\n\n"
    footer = f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"

    by_day: Dict[dt.date, List[Event]] = {
        start_day + dt.timedelta(days=i): []
        for i in range((end_day - start_day).days + 1)
    }
    for ev in events:
        by_day[ev.start.astimezone(KYIV_TZ).date()].append(ev)
    day_blocks = [format_week_day(by_day[d], d) for d in by_day]

    messages: List[str] = []
    cur = title
    has_day = False
    for block in day_blocks:
        candidate = cur + ("\n\n" if has_day else "") + block
        if has_day and tg_visible_len(candidate + footer) > TG_MAX_LEN:
            messages.append(cur)
            cur = cont_title + block
        else:
            cur = candidate
        has_day = True
    messages.append(cur + footer)
    return messages


def tg_send_message(
    token: str,
    chat_id: str,
    text: str,
    message_thread_id: Optional[int] = None,
    reply_markup: Optional[Dict] = None,
    preview_url: Optional[str] = None,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if preview_url:
        # Велика картинка НАД текстом — у тому ж повідомленні.
        # Невидиме посилання на картинку на самому початку тексту: без нього
        # Telegram іноді взагалі не будує прев'ю з link_preview_options.url.
        payload["text"] = f'<a href="{escape_html_attr(preview_url)}">\u2060</a>' + text
        payload["link_preview_options"] = {
            "url": preview_url,
            "prefer_large_media": True,
            "show_above_text": True,
        }
    else:
        payload["link_preview_options"] = {"is_disabled": True}
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")


def tg_send_photo(token: str, chat_id: str, path: str,
                  message_thread_id: Optional[int] = None) -> None:
    data = {"chat_id": chat_id}
    if message_thread_id is not None:
        data["message_thread_id"] = message_thread_id
    with open(path, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                          data=data, files={"photo": f}, timeout=60)
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError(f"Telegram error: {r.text}")


# ───────────────────────── Картинки-банери ─────────────────────────

CARDS_DIR = "cards"
CARDS_KEEP_DAYS = 14
UA_MONTHS_GEN = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня",
                 "серпня", "вересня", "жовтня", "листопада", "грудня"]
UA_DOW_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def plain_place(ev: Event, meeting_kind: Optional[str]) -> str:
    """Місце без емодзі для картинки: «Online · ауд. 207»."""
    blob = f"{ev.location}\n{ev.description}".lower()
    parts = []
    if meeting_kind or "online" in blob or "онлайн" in blob or "zoom" in blob:
        parts.append("Online")
    aud = AUD_RE.search(blob)
    if aud:
        parts.append(fmt_aud(aud))
    if not parts:
        loc = URL_RE.sub("", ev.location).strip(" ,;\n")
        if loc:
            parts.append(loc)
    return " · ".join(parts)


def nearest_pair(t: dt.datetime) -> int:
    """Номер пари для сітки: точний збіг або найближчий час початку."""
    no = pair_no(t)
    if no:
        return no
    mins = t.hour * 60 + t.minute
    def dist(item):
        h, m = map(int, item[0].split(":"))
        return abs(h * 60 + m - mins)
    return min(PAIR_BY_START.items(), key=dist)[1]


def card_weather(w: Optional[Dict]) -> Optional[Dict]:
    if not w:
        return None
    _, desc = weathercode_ua(w["code"])
    sign = lambda t: f"+{t}" if t > 0 else str(t)
    return {"code": w["code"], "tmorning": sign(w["tmorning"]), "tmax": sign(w["tmax"]), "desc": desc}


def make_day_card(events: List[Event], day: dt.date, label: str,
                  w: Optional[Dict], tag: str) -> Optional[str]:
    if not events:
        return None
    try:
        import cards
        pairs = []
        for ev in sort_events(events):
            discipline, etype = split_summary(ev.summary)
            meeting, _, _ = event_links(ev)
            pairs.append({
                "no": pair_no(ev.start), "start": hhmm(ev.start), "end": hhmm(ev.end),
                "name": discipline, "teacher": extract_teacher(ev.description),
                "type": etype, "place": plain_place(ev, meeting[0] if meeting else None),
            })
        os.makedirs(CARDS_DIR, exist_ok=True)
        path = os.path.join(CARDS_DIR, f"day-{day.isoformat()}-{tag}.png")
        cards.render_day(path, label, UA_DOW[day.weekday()],
                         f"{day.day} {UA_MONTHS_GEN[day.month - 1]}", card_weather(w), pairs)
        return path
    except Exception as e:
        print(f"WARNING: day card failed: {e}")
        return None


def make_week_card(events: List[Event], start: dt.date, end: dt.date, tag: str) -> Optional[str]:
    if not events:
        return None
    try:
        import cards
        n_days = 5
        for d in (5, 6):   # субота/неділя — лише якщо в них є пари
            if any(ev.start.astimezone(KYIV_TZ).date() == start + dt.timedelta(days=d) for ev in events):
                n_days = d + 1
        days = [(UA_DOW_SHORT[(start + dt.timedelta(days=i)).weekday()],
                 fmt_date_short(start + dt.timedelta(days=i)), False) for i in range(n_days)]
        starts = sorted(PAIR_BY_START.items(), key=lambda x: x[1])
        times = []
        for t, no in starts:
            h, m = map(int, t.split(":"))
            e = dt.datetime(2000, 1, 1, h, m) + dt.timedelta(minutes=80)
            times.append((no, t, e.strftime("%H:%M")))
        grid = {}
        for ev in sort_events(events):
            c = (ev.start.astimezone(KYIV_TZ).date() - start).days
            r = nearest_pair(ev.start) - 1
            if 0 <= c < n_days and 0 <= r < len(times) and (c, r) not in grid:
                discipline, etype = split_summary(ev.summary)
                grid[(c, r)] = (discipline, etype)
        os.makedirs(CARDS_DIR, exist_ok=True)
        path = os.path.join(CARDS_DIR, f"week-{start.isoformat()}-{tag}.png")
        cards.render_week(path, f"{fmt_date_short(start)} – {fmt_date_short(end)}", days, times, grid)
        return path
    except Exception as e:
        print(f"WARNING: week card failed: {e}")
        return None


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=90)


def cleanup_old_cards(today: dt.date) -> None:
    """Видаляємо картинки, старші за CARDS_KEEP_DAYS, щоб репозиторій не ріс."""
    if not os.path.isdir(CARDS_DIR):
        return
    border = today - dt.timedelta(days=CARDS_KEEP_DAYS)
    for name in os.listdir(CARDS_DIR):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
        if m and dt.date.fromisoformat(m.group(1)) < border:
            os.remove(os.path.join(CARDS_DIR, name))


def publish_card(path: str, today: dt.date) -> Optional[str]:
    """Комітить картинку в репозиторій і повертає її пряме посилання,
    якщо Telegram зможе її відкрити (публічний репозиторій). Інакше None."""
    repo = os.getenv("GITHUB_REPOSITORY")
    if not repo or os.getenv("GITHUB_ACTIONS") != "true":
        return None
    try:
        cleanup_old_cards(today)
        if not _git("config", "user.email").stdout.strip():
            _git("config", "user.name", "github-actions")
            _git("config", "user.email", "github-actions@github.com")
        _git("add", "-A", CARDS_DIR)
        _git("commit", "-m", f"Schedule card {os.path.basename(path)}")
        branch = os.getenv("GITHUB_REF_NAME") or "main"
        for _ in range(3):
            _git("pull", "--rebase", "--autostash", "origin", branch)
            if _git("push", "origin", f"HEAD:{branch}").returncode == 0:
                break
        else:
            print("WARNING: could not push card")
            return None
        sha = _git("rev-parse", "HEAD").stdout.strip()
        url = f"https://raw.githubusercontent.com/{repo}/{sha}/{path.replace(os.sep, '/')}"
        for _ in range(5):
            try:
                if requests.head(url, timeout=10).status_code == 200:
                    time.sleep(3)
                    return url
            except requests.RequestException:
                pass
            time.sleep(2)
        print("INFO: card URL not public — sending the picture as a separate photo")
        return None
    except Exception as e:
        print(f"WARNING: publish card failed: {e}")
        return None


TEST_BANNER = "🧪 <b>ТЕСТ</b> — це бачиш тільки ти, у групу не пішло\n\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["today", "tomorrow", "week"], help="Posting mode")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Надіслати пост в особисті (TEST_CHAT_ID), а не в групу. "
             "Ігнорує дні тижня, не перевіряє і не змінює state.json."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="week: опублікувати розклад ПОТОЧНОГО тижня в будь-який день "
             "(напр. якщо недільний пост не вийшов)."
    )
    args = parser.parse_args()
    test = args.test

    token = env_required("TG_BOT_TOKEN")
    ics_url = env_required("GCAL_ICS_URL")
    if test:
        chat_id = env_required("TEST_CHAT_ID")
        schedule_thread_id = None
        print("TEST MODE: posting to TEST_CHAT_ID, state.json is not touched.")
    else:
        chat_id = env_required("TG_CHAT_ID")
        schedule_thread_id = env_optional_int("TG_SCHEDULE_THREAD_ID")

    def send(text: str, thread: Optional[int] = None,
             reply_markup: Optional[Dict] = None, banner: bool = True,
             preview_url: Optional[str] = None) -> None:
        if test and banner:
            text = TEST_BANNER + text
        tg_send_message(token, chat_id, text, message_thread_id=thread,
                        reply_markup=reply_markup, preview_url=preview_url)

    def send_with_card(text: str, card: Optional[str], thread: Optional[int] = None,
                       banner: bool = True) -> None:
        """Картинка над текстом в одному повідомленні; якщо посилання на неї
        недоступне — окремим фото перед текстом. Збій картинки пост не ламає."""
        url = publish_card(card, today) if card else None
        if url:
            send(text, thread, banner=banner, preview_url=url)
            return
        if card:
            try:
                tg_send_photo(token, chat_id, card, message_thread_id=thread)
            except Exception as e:
                print(f"WARNING: photo failed: {e}")
        send(text, thread, banner=banner)

    def already_posted(key: str, stamp: str) -> bool:
        return not test and not should_post(state, key, stamp)

    def remember(key: str, stamp: str) -> None:
        if not test:
            mark_posted(state, key, stamp)
            save_state(state)

    state = load_state()
    ics = fetch_ics(ics_url)
    all_events = parse_ics_events(ics)
    today = now_kyiv().date()
    # унікальна назва картинки: Telegram кешує прев'ю за посиланням
    card_tag = now_kyiv().strftime("%H%M%S") + ("-test" if test else "")

    if args.mode == "today":
        if not test and (is_saturday(today) or is_sunday(today)):
            print("Skipping 'today': no today-posts on Saturday or Sunday.")
            return
        target = today
        stamp = f"today:{iso_date(target)}"
        if already_posted("last_today", stamp):
            print("Already posted today schedule for this date. Exiting.")
            return
        day_events = events_in_range(all_events, target, target)
        w = get_weather_dnipro(target)
        weather = format_weather_block(target, "сьогодні", w)
        msg = "<b>Доброго ранку шановні студенти!</b> ☀️\n\n" + weather
        msg += f"🗓️ <b>Розклад на сьогодні ({fmt_date_short(target)})</b>\n\n"
        msg += format_day(day_events, target)
        msg += f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"
        card = make_day_card(day_events, target, "Розклад на сьогодні", w, card_tag)
        send_with_card(msg, card)
        remember("last_today", stamp)
        print("Posted today schedule.")

    elif args.mode == "tomorrow":
        if not test and is_saturday(today):
            print("Skipping 'tomorrow': no posts on Saturday.")
            return
        target = today + dt.timedelta(days=1)
        stamp = f"tomorrow:{iso_date(target)}"
        if already_posted("last_tomorrow", stamp):
            print("Already posted tomorrow schedule for this date. Exiting.")
            return
        day_events = events_in_range(all_events, target, target)
        w = get_weather_dnipro(target)
        weather = format_weather_block(target, "завтра", w)
        msg = "<b>Добрий вечір шановні студенти!</b> 🌙\n\n" + weather
        msg += f"🗓️ <b>Розклад на завтра ({fmt_date_short(target)})</b>\n\n"
        msg += format_day(day_events, target)
        msg += f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"
        card = make_day_card(day_events, target, "Розклад на завтра", w, card_tag)
        send_with_card(msg, card)
        remember("last_tomorrow", stamp)
        print("Posted tomorrow schedule.")

    elif args.mode == "week":
        if not test and not args.force and not is_sunday(today):
            print("Skipping 'week': weekly post should run on Sunday only.")
            return
        this_monday = today - dt.timedelta(days=today.weekday())
        # --force у будній день — поточний тиждень; у неділю й за розкладом — наступний
        if args.force and not is_sunday(today):
            next_monday = this_monday
        else:
            next_monday = this_monday + dt.timedelta(days=7)
        next_sunday = next_monday + dt.timedelta(days=6)
        stamp = f"week:{iso_date(next_monday)}:{iso_date(next_sunday)}"
        if already_posted("last_week", stamp):
            print("Already posted weekly schedule for this week-range. Exiting.")
            return
        week_events = events_in_range(all_events, next_monday, next_sunday)
        messages = format_week_messages(week_events, next_monday, next_sunday)
        if not test and schedule_thread_id is None:
            print("WARNING: TG_SCHEDULE_THREAD_ID not set. Weekly post will go to general chat.")
        card = make_week_card(week_events, next_monday, next_sunday, card_tag)
        for i, msg in enumerate(messages):
            if i == 0:
                send_with_card(msg, card, thread=schedule_thread_id)
            else:
                send(msg, thread=schedule_thread_id, banner=False)
        # Тиждень уже відправлено — фіксуємо одразу, щоб при збої банки
        # наступний запуск не задублював розклад.
        remember("last_week", stamp)
        # Банка — окремим повідомленням з кнопкою, тож доходить завжди.
        try:
            send(DONATE_LINE, thread=schedule_thread_id,
                 reply_markup=DONATE_BUTTON, banner=False)
        except Exception as e:
            print(f"WARNING: donate message failed: {e}")
        print(f"Posted weekly schedule (next week) in {len(messages)} message(s) + donate.")


if __name__ == "__main__":
    main()
