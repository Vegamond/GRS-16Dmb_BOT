# -*- coding: utf-8 -*-
"""Картинки-банери для розкладу: картка дня і сітка тижня.

Малює лише картинку з уже готових даних — нічого не знає про календар
чи Telegram. Шрифти Inter (ліцензія SIL OFL) лежать у папці fonts/ поруч.
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).resolve().parent / "fonts"
_FONT_FILES = {
    "r": "Inter-Regular.otf",
    "m": "Inter-Medium.otf",
    "sb": "Inter-SemiBold.otf",
    "b": "Inter-Bold.otf",
    "xb": "Inter-ExtraBold.otf",
}
_font_cache: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    key = (weight, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(FONT_DIR / _FONT_FILES[weight]), size)
    return _font_cache[key]


BG = (18, 24, 33)
CARD = (29, 39, 51)
CARD2 = (24, 32, 43)
TEXT = (236, 240, 245)
MUTED = (139, 155, 173)
ACCENT = (100, 181, 246)

# тип заняття (як у боті) → (колір, підпис на чипі)
TYPE_STYLE = {
    "Ректорська контрольна робота": ((239, 83, 80), "РКР"),
    "Залік": ((255, 167, 38), "ЗАЛІК"),
    "Іспит": ((255, 167, 38), "ІСПИТ"),
    "Екзамен": ((255, 167, 38), "ЕКЗАМЕН"),
    "Лекція": ((66, 165, 245), "Лекція"),
    "Гостьова Лекція": ((171, 71, 188), "Гостьова"),
    "Практичне": ((102, 187, 106), "Практичне"),
    "Лабораторна": ((38, 198, 218), "Лабораторна"),
    "Семінар": ((92, 107, 192), "Семінар"),
    "Консультація": ((141, 110, 99), "Консультація"),
}
DEFAULT_STYLE = ((120, 144, 156), "Заняття")
FRAMED = {"Ректорська контрольна робота", "Залік", "Іспит", "Екзамен"}


def style(etype: Optional[str]) -> Tuple[Tuple[int, int, int], str]:
    return TYPE_STYLE.get(etype or "", DEFAULT_STYLE)


def _mix(color, base, k):
    return tuple(int(c * k + b * (1 - k)) for c, b in zip(color, base))


def _chip(d, x, y, text, color, f):
    w = d.textlength(text, font=f)
    h = f.size + 14
    d.rounded_rectangle((x, y, x + w + 24, y + h), h // 2, fill=_mix(color, CARD, 0.22))
    d.text((x + 12, y + 6), text, font=f, fill=color)
    return x + w + 24


def _fit(d, text, f, max_w):
    if d.textlength(text, font=f) <= max_w:
        return text
    while text and d.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(d, text, f, max_w, max_lines=2):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    lines = [_fit(d, ln, f, max_w) for ln in lines]
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [_fit(d, " ".join(lines[max_lines - 1:]), f, max_w)]
    return lines


# ───────────────────── іконка погоди (малюємо фігурами) ─────────────────────
RAIN = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
SNOW = {71, 73, 75, 77, 85, 86}


def _cloud(d, cx, cy, s, col):
    d.ellipse((cx - s, cy - s * .2, cx + s * .1, cy + s * .7), fill=col)
    d.ellipse((cx - s * .55, cy - s * .7, cx + s * .55, cy + s * .5), fill=col)
    d.ellipse((cx - s * .05, cy - s * .35, cx + s, cy + s * .7), fill=col)
    d.rectangle((cx - s * .6, cy + s * .2, cx + s * .6, cy + s * .7), fill=col)


def _weather_icon(d, cx, cy, s, code):
    sun = (255, 202, 40)
    cloud = (200, 210, 222)
    if code in (0, 1):
        d.ellipse((cx - s * .6, cy - s * .6, cx + s * .6, cy + s * .6), fill=sun)
        return
    if code == 2:
        d.ellipse((cx - s * .1, cy - s * .9, cx + s * .9, cy + s * .1), fill=sun)
    _cloud(d, cx, cy, s, cloud)
    if code in RAIN:
        for i in (-1, 0, 1):
            x = cx + i * s * .45
            d.line((x, cy + s * .95, x - s * .15, cy + s * 1.35), fill=ACCENT, width=max(3, s // 8))
    if code in SNOW:
        for i in (-1, 0, 1):
            x = cx + i * s * .45
            d.ellipse((x - s * .1, cy + s * 1.0, x + s * .1, cy + s * 1.2), fill=(240, 244, 248))


# ───────────────────────────── картка дня ─────────────────────────────
def render_day(path: str, label: str, weekday: str, date_txt: str,
               weather: Optional[Dict], pairs: List[Dict]) -> None:
    """pairs: [{no, start, end, name, teacher, type, place}]
    weather: {code, tmorning, tmax, desc} або None."""
    W, pad = 1080, 48
    name_f, sub_f, chip_f = font("b", 46), font("m", 32), font("sb", 30)
    x_name = pad + 190
    max_name = W - pad - 36 - x_name
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    rows = []
    for p in pairs:
        lines = _wrap(tmp, p["name"], name_f, max_name)
        h = 36 + 56 * len(lines) + (44 if p.get("teacher") else 0) + 64 + 28
        h = max(h, 230)                     # щоб влізли номер і час ліворуч
        rows.append((p, lines, h))
    top, gap = 250, 20
    H = top + sum(r[2] for r in rows) + gap * max(0, len(rows) - 1) + 48

    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    for i in range(260):
        d.line((0, i, W, i), fill=_mix(ACCENT, BG, 0.12 * (1 - i / 260)))

    d.text((pad, 40), label.upper(), font=font("sb", 30), fill=ACCENT)
    d.text((pad, 80), weekday, font=font("xb", 84), fill=TEXT)
    d.text((pad, 180), date_txt, font=font("m", 40), fill=MUTED)

    if weather:
        t = f"{weather['tmorning']}° → {weather['tmax']}°"
        tf = font("b", 52)
        tw = d.textlength(t, font=tf)
        d.text((W - pad - tw, 96), t, font=tf, fill=TEXT)
        sf = font("m", 32)
        d.text((W - pad - d.textlength(weather["desc"], font=sf), 166), weather["desc"], font=sf, fill=MUTED)
        _weather_icon(d, int(W - pad - tw - 56), 118, 30, weather["code"])

    y = top
    for p, lines, h in rows:
        color, tag = style(p.get("type"))
        d.rounded_rectangle((pad, y, W - pad, y + h), 26, fill=CARD)
        if p.get("type") in FRAMED:
            d.rounded_rectangle((pad, y, W - pad, y + h), 26, outline=color, width=4)
        d.rounded_rectangle((pad, y, pad + 12, y + h), 6, fill=color)

        cx = pad + 92
        if p.get("no"):
            d.text((cx, y + 36), str(p["no"]), font=font("xb", 80), fill=color, anchor="mt")
            ty = y + 128
        else:
            ty = y + 50
        d.text((cx, ty), p["start"], font=font("b", 34), fill=TEXT, anchor="mt")
        d.text((cx, ty + 40), p["end"], font=font("r", 30), fill=MUTED, anchor="mt")

        yy = y + 32
        for ln in lines:
            d.text((x_name, yy), ln, font=name_f, fill=TEXT)
            yy += 56
        if p.get("teacher"):
            d.text((x_name, yy + 4), _fit(d, p["teacher"], sub_f, max_name), font=sub_f, fill=MUTED)
            yy += 44
        nx = _chip(d, x_name, yy + 14, tag, color, chip_f)
        if p.get("place"):
            pf = font("m", 30)
            d.text((nx + 18, yy + 20), _fit(d, p["place"], pf, W - pad - 36 - nx - 18), font=pf, fill=MUTED)
        y += h + gap
    im.save(path, optimize=True)


# ───────────────────────────── сітка тижня ─────────────────────────────
def render_week(path: str, title: str, days: List[Tuple[str, str, bool]],
                times: List[Tuple[int, str, str]],
                grid: Dict[Tuple[int, int], Tuple[str, Optional[str]]]) -> None:
    """days: [(«Пн», «26.10», підсвітити?)]; times: [(1, «09:00», «10:20»)];
    grid: {(стовпчик, рядок): (назва, тип)}."""
    W, H, pad = 1280, 720, 40
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((pad, 30), "РОЗКЛАД НА ТИЖДЕНЬ", font=font("sb", 22), fill=ACCENT)
    d.text((pad, 58), title, font=font("xb", 44), fill=TEXT)

    # легенда — лише типи, що реально є цього тижня
    present = []
    for name, typ in grid.values():
        tag = style(typ)
        if tag not in present:
            present.append(tag)
    lf = font("m", 20)
    x = W - pad
    for color, tag in present[::-1][:6]:
        w = d.textlength(tag, font=lf)
        x -= w
        d.text((x, 78), tag, font=lf, fill=MUTED)
        x -= 26
        d.ellipse((x, 82, x + 16, 98), fill=color)
        x -= 28

    top, left = 130, pad + 120
    cols, rows = len(days), len(times)
    cw = (W - left - pad - (cols - 1) * 10) / cols
    rh = (H - top - 80 - (rows - 1) * 10) / rows
    name_f = font("sb", 18 if cols >= 6 else 19)
    for c, (dn, dd, hl) in enumerate(days):
        x = left + c * (cw + 10)
        d.text((x + cw / 2, top + 4), dn, font=font("b", 26), fill=ACCENT if hl else TEXT, anchor="mt")
        d.text((x + cw / 2, top + 36), dd, font=font("r", 18), fill=MUTED, anchor="mt")
    gy = top + 70
    for r, (no, t1, t2) in enumerate(times):
        y = gy + r * (rh + 10)
        d.text((pad + 8, y + rh / 2 - 12), str(no), font=font("xb", 30), fill=MUTED, anchor="lm")
        d.text((pad + 40, y + rh / 2 - 12), t1, font=font("sb", 18), fill=TEXT, anchor="lm")
        d.text((pad + 40, y + rh / 2 + 12), t2, font=font("r", 16), fill=MUTED, anchor="lm")
        for c in range(cols):
            x = left + c * (cw + 10)
            box = (x, y, x + cw, y + rh)
            cell = grid.get((c, r))
            if not cell:
                d.rounded_rectangle(box, 14, fill=CARD2)
                continue
            name, typ = cell
            color, tag = style(typ)
            d.rounded_rectangle(box, 14, fill=_mix(color, CARD, 0.20))
            d.rounded_rectangle((x, y, x + 7, y + rh), 4, fill=color)
            if typ in FRAMED:
                d.rounded_rectangle(box, 14, outline=color, width=2)
            for i, line in enumerate(_wrap(d, name, name_f, cw - 28)):
                d.text((x + 16, y + 12 + i * 22), line, font=name_f, fill=TEXT)
            d.text((x + 16, y + rh - 12), tag, font=font("m", 15), fill=color, anchor="lb")
    im.save(path, optimize=True)
