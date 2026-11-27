"""Telegram message texts (HTML parse mode). Pure functions."""

from __future__ import annotations

import html
import math
from datetime import datetime, timedelta, timezone

from app.paper.sim import risk_pct_for_score

TEHRAN = timezone(timedelta(hours=3, minutes=30))
STYLE_FA = {"trend_pullback": "پولبک روندی", "breakout": "شکست محدوده", "mean_reversion": "برگشت به میانگین",
            "liquidity_sweep": "شکار نقدینگی", "divergence": "واگرایی RSI", "vwap_volume": "VWAP و حجم"}
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fmt_price(p: float | None, ref: float | None = None) -> str:
    if p is None:
        return "—"
    r = abs(ref if ref else p)
    d = 1 if r >= 1000 else 2 if r >= 10 else min(8, max(3, 3 - math.floor(math.log10(r)))) if r > 0 else 2
    return f"{p:,.{d}f}"


def fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TEHRAN).strftime("%H:%M").translate(FA_DIGITS)


def _levels(c: dict) -> tuple[float, float, float, float, str]:
    if c.get("lbank_entry") is not None:
        return c["lbank_entry"], c["lbank_stop"], c["lbank_tp1"], c["lbank_tp2"], "ال‌بانک"
    return c["entry"], c["stop"], c["tp1"], c["tp2"], "بایننس"


def new_signal(c: dict, site: str) -> str:
    long = c["side"] > 0
    entry, stop, tp1, tp2, venue = _levels(c)
    risk = abs(entry - stop)
    rr2 = abs(tp2 - entry) / risk if risk else 0
    styles = "، ".join(STYLE_FA.get(s["style"], s["style"]) for s in c.get("styles", []) if s.get("core", True))
    sym = html.escape(c["symbol"])
    lines = [
        f"{'🟢' if long else '🔴'} <b>{'خرید' if long else 'فروش'} {sym}</b>   امتیاز {round(c['score'])}",
        "",
        f"ورود: <code>{fmt_price(entry)}</code>",
        f"حد ضرر: <code>{fmt_price(stop, entry)}</code>",
        f"TP1: <code>{fmt_price(tp1, entry)}</code>  (بستن نصف پوزیشن)",
        f"TP2: <code>{fmt_price(tp2, entry)}</code>  (سود به ضرر 1:{rr2:.1f})",
        "",
        f"ریسک پیشنهادی: {risk_pct_for_score(c['score'])}٪ سرمایه",
        f"دلیل: {html.escape(styles)}",
    ]
    if venue != "ال‌بانک":
        lines.append("⚠️ قیمت‌ها از بایننس است؛ قبل از ورود قیمت ال‌بانک را چک کنید.")
    for n in c.get("notes") or []:
        lines.append(f"• {html.escape(n)}")
    lines += ["", f"<a href=\"{site}/#chart\">نمودار و جزئیات</a>   ·   قیمت‌ها: {venue}"]
    return "\n".join(lines)


def update(c: dict, kind: str, payload: dict) -> str | None:
    sym = html.escape(c["symbol"])
    side = "خرید" if c["side"] > 0 else "فروش"
    entry, *_ = _levels(c)
    r = payload.get("r")
    rs = f"{r:+.2f}R" if isinstance(r, (int, float)) else ""
    if kind == "tp1":
        return (f"✅ <b>TP1 رسید</b> | {side} {sym}\n"
                f"نصف پوزیشن را ببندید و حد ضرر را به نقطه ورود (<code>{fmt_price(entry)}</code>) ببرید.")
    if kind == "tp2":
        return f"🎯 <b>هدف نهایی رسید</b> | {side} {sym}\nمعامله کامل بسته شد. نتیجه: <b>{rs}</b>"
    if kind == "stop":
        return f"🛑 <b>حد ضرر خورد</b> | {side} {sym}\nنتیجه: <b>{rs}</b>"
    if kind == "breakeven":
        return f"⚪️ <b>بسته شد در نقطه ورود</b> | {side} {sym}\nبعد از TP1 قیمت به ورود برگشت. نتیجه کل: <b>{rs}</b>"
    if kind == "void":
        return f"⏹ <b>سیگنال باطل شد</b> | {side} {sym}\nقیمت قبل از ورود از حد ضرر یا TP1 عبور کرد؛ وارد نشوید."
    return None


def welcome_linked(username: str) -> str:
    return (f"اتصال انجام شد ✅\nحساب <b>{html.escape(username)}</b> به این تلگرام وصل شد. "
            "سیگنال‌ها و به‌روزرسانی‌ها اینجا می‌آیند.\n\n/off قطع موقت اعلان‌ها   /on وصل دوباره   /status وضعیت")


def help_text(site: str) -> str:
    return ("برای اتصال، وارد سایت شوید و در منوی بالا «تلگرام» را بزنید؛ لینک اتصال آنجاست.\n"
            f"{site}\n\n/status وضعیت   /on   /off")
