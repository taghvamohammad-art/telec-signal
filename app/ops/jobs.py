"""Server operations the admin may start from the web panel.

Security model: the web process can only QUEUE a job kind with parameters.
The root runner (app/ops/runner.py) rebuilds the command itself from this
whitelist; no command text ever travels from the browser or the database.
Every parameter is an enum, a bounded integer, a date or a symbol pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

PY = ".venv/bin/python"
SERVICES = ["telec-data", "telec-analysis", "telec-web", "telec-bot", "telec-whales", "telec-portfolio"]
RUNNER = "telec-ops"
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvalidParams(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    kind: str                      # "choice" | "int" | "date" | "symbols"
    default: object = None
    choices: tuple = ()            # (value, label) pairs for "choice"
    lo: int = 0
    hi: int = 0
    optional: bool = False

    def clean(self, raw):
        if raw is None or raw == "" or raw == []:
            if self.optional:
                return None
            raw = self.default
        if self.kind == "choice":
            allowed = [c[0] for c in self.choices]
            if raw not in allowed:
                raise InvalidParams(f"{self.label}: مقدار مجاز نیست")
            return raw
        if self.kind == "int":
            try:
                v = int(raw)
            except (TypeError, ValueError):
                raise InvalidParams(f"{self.label}: عدد صحیح وارد کنید") from None
            if not self.lo <= v <= self.hi:
                raise InvalidParams(f"{self.label}: باید بین {self.lo} و {self.hi} باشد")
            return v
        if self.kind == "date":
            s = str(raw).strip()
            if not DATE_RE.match(s):
                raise InvalidParams(f"{self.label}: تاریخ را به شکل 2022-01-01 وارد کنید")
            try:
                datetime.strptime(s, "%Y-%m-%d")
            except ValueError:
                raise InvalidParams(f"{self.label}: تاریخ نامعتبر است") from None
            return s
        if self.kind == "symbols":
            items = raw if isinstance(raw, list) else str(raw).replace(",", " ").split()
            out = []
            for it in items:
                s = str(it).strip().upper()
                if not SYMBOL_RE.match(s):
                    raise InvalidParams(f"{self.label}: «{s[:24]}» نماد معتبر نیست (مثل BTCUSDT)")
                out.append(s)
            if len(out) > 12:
                raise InvalidParams(f"{self.label}: حداکثر ۱۲ نماد")
            return out or None
        raise InvalidParams("نوع پارامتر ناشناخته")


@dataclass(frozen=True)
class Step:
    argv: tuple
    as_root: bool = False


@dataclass(frozen=True)
class JobKind:
    key: str
    label: str
    help: str
    group: str
    params: tuple = field(default_factory=tuple)
    timeout_s: int = 3600
    restarts_runner: bool = False
    dangerous: bool = False

    def clean(self, raw: dict | None) -> dict:
        raw = raw or {}
        if not isinstance(raw, dict):
            raise InvalidParams("پارامترها نامعتبرند")
        unknown = set(raw) - {p.name for p in self.params}
        if unknown:
            raise InvalidParams("پارامتر ناشناخته")
        return {p.name: p.clean(raw.get(p.name)) for p in self.params}


SERVICE_CHOICES = (("all", "همه"),) + tuple((s, s.replace("telec-", "")) for s in SERVICES + [RUNNER])
TF_CHOICES = (("1d", "روزانه"), ("4h", "۴ ساعته"), ("1h", "۱ ساعته"), ("15m", "۱۵ دقیقه"))

KINDS: dict[str, JobKind] = {k.key: k for k in [
    JobKind("update", "به‌روزرسانی برنامه", "آخرین نسخه را از گیت‌هاب می‌گیرد، کتابخانه‌ها و سرویس‌ها را نصب و همه را ری‌استارت می‌کند.",
            "سرور", timeout_s=900, restarts_runner=True, dangerous=True),
    JobKind("restart", "ری‌استارت سرویس", "یک سرویس یا همه را دوباره راه می‌اندازد.", "سرور",
            (Param("service", "سرویس", "choice", "all", SERVICE_CHOICES),), timeout_s=120, dangerous=True),
    JobKind("status", "وضعیت سرور", "وضعیت سرویس‌ها، فضای دیسک، حافظه و آخرین به‌روزرسانی.", "سرور", timeout_s=60),
    JobKind("logs", "لاگ سرویس", "آخرین خطوط لاگ یک سرویس.", "سرور",
            (Param("service", "سرویس", "choice", "telec-analysis", SERVICE_CHOICES[1:]),
             Param("lines", "تعداد خط", "int", 100, lo=20, hi=1000)), timeout_s=60),
    JobKind("extend_history", "گرفتن تاریخچهٔ قیمت", "کندل‌های گذشته را از بایننس می‌گیرد.", "داده",
            (Param("days", "چند روز", "int", 400, lo=30, hi=3000),
             Param("tf", "تایم‌فریم", "choice", "1d", TF_CHOICES),
             Param("universe", "ارزها", "choice", "all", (("analysis", "ارزهای تحلیل"), ("all", "۱۰۰ ارز امروز"),
                                                           ("binance-all", "همهٔ ارزهای بایننس (با حذف‌شده‌ها)"))),
             Param("symbols", "فقط این نمادها (اختیاری)", "symbols", optional=True)), timeout_s=3 * 3600),
    JobKind("bt_signals", "بک‌تست سیگنال‌های v2.1", "سیستم سیگنال فعلی را روی گذشته آزمایش می‌کند.", "بک‌تست",
            (Param("days", "چند روز", "int", 120, lo=30, hi=1000),), timeout_s=4 * 3600),
    JobKind("bt_trend", "بک‌تست روندی v3", "مجموعهٔ دانچیان روی کندل روزانه.", "بک‌تست",
            (Param("since", "از تاریخ (اختیاری)", "date", optional=True),), timeout_s=2 * 3600),
    JobKind("bt_intraday", "بک‌تست درون‌روز", "شکست محدودهٔ نویز و مومنتوم نیم‌ساعته.", "بک‌تست",
            (Param("symbols", "نمادها (اختیاری)", "symbols", optional=True),), timeout_s=3600),
    JobKind("bt_crowding", "بک‌تست ازدحام (فاندینگ)", "خرید کم‌فاندینگ‌ها و فروش پرفاندینگ‌ها.", "بک‌تست",
            (Param("universe", "ارزها", "choice", "everything", (("everything", "همه، با ارزهای حذف‌شده"), ("today", "فقط ارزهای امروز"))),
             Param("since", "از تاریخ (اختیاری)", "date", optional=True),
             Param("top_n", "تعداد ارز پرحجم", "int", 50, lo=10, hi=200)), timeout_s=3 * 3600),
    JobKind("bt_lab", "آزمایشگاه سبک‌ها", "ده‌ها سبک را با قانون‌های یکسان و محافظت در برابر شانس آزمایش می‌کند.", "بک‌تست",
            timeout_s=2 * 3600),
    JobKind("bt_lab_reveal", "آزمایشگاه: دورهٔ قفل نامزدها", "دورهٔ ۲۰۲۵ به بعد را فقط برای نامزدهای از پیش اعلام‌شده نشان می‌دهد: "
            "سه ترکیب (combo_top، combo_trend، combo_all_pos) و btc_ma50، tsmom_regime، tsmom_14.", "بک‌تست", timeout_s=2 * 3600),
    JobKind("portfolio_run", "اجرای پرتفوی همین حالا", "داده‌های روز را می‌گیرد و پرتفوی آزمایشی را به‌روز می‌کند (اگر امروز انجام نشده).", "داده",
            timeout_s=3600),
    JobKind("whales_once", "نمونهٔ پوزیشن نهنگ‌ها", "یک بار پوزیشن نهنگ‌های Hyperliquid را می‌خواند و خلاصه می‌دهد.", "داده",
            timeout_s=600),
]}


def _services(choice: str) -> list[str]:
    return SERVICES if choice == "all" else [choice]


def build_steps(key: str, p: dict, app_dir: str) -> list[Step]:
    """The only place where commands are made. Every value in p was cleaned by JobKind.clean."""
    if key == "update":
        units = [Step(("sh", "-c", 'for s in "$@"; do sed "s#__APP_DIR__#%s#g" deploy/$s.service > /etc/systemd/system/$s.service; done' % app_dir,
                       "sh", *SERVICES, RUNNER), as_root=True)]
        return [Step(("git", "pull", "--ff-only"), as_root=True),
                Step((f"{app_dir}/.venv/bin/pip", "install", "-q", "-r", "requirements.txt"), as_root=True),
                *units,
                Step(("systemctl", "daemon-reload"), as_root=True),
                Step(("systemctl", "enable", *SERVICES, RUNNER), as_root=True),
                Step(("systemctl", "restart", *SERVICES), as_root=True),
                Step(("git", "log", "-1", "--format=نسخهٔ فعلی: %h  %s  (%cr)"), as_root=True)]
    if key == "restart":
        names = _services(p["service"])
        if p["service"] == RUNNER:
            return [Step(("echo", "سرویس عملیات بعد از پایان این کار ری‌استارت می‌شود."), as_root=True)]
        return [Step(("systemctl", "restart", *names), as_root=True),
                Step(("systemctl", "--no-pager", "--lines=0", "status", *names), as_root=True)]
    if key == "status":
        return [Step(("systemctl", "--no-pager", "--lines=0", "status", *SERVICES, RUNNER), as_root=True),
                Step(("df", "-h", "/"), as_root=True), Step(("free", "-m"), as_root=True), Step(("uptime",), as_root=True),
                Step(("git", "log", "-3", "--format=%h  %s  (%cr)"), as_root=True)]
    if key == "logs":
        return [Step(("journalctl", "-u", p["service"], "-n", str(p["lines"]), "--no-pager", "-o", "short-iso"), as_root=True)]
    if key == "extend_history":
        argv = [PY, "scripts/extend_history.py", "--days", str(p["days"]), "--tf", p["tf"], "--universe", p["universe"]]
        if p.get("symbols"):
            argv += ["--symbols", *p["symbols"]]
        return [Step(tuple(argv))]
    if key == "bt_signals":
        return [Step((PY, "-m", "app.backtest.run", "--days", str(p["days"])))]
    if key == "bt_trend":
        return [Step((PY, "-m", "app.trend.backtest", *(["--since", p["since"]] if p.get("since") else [])))]
    if key == "bt_intraday":
        return [Step((PY, "-m", "app.intraday.backtest", *(p.get("symbols") or [])))]
    if key == "bt_crowding":
        argv = [PY, "-m", "app.crowding.backtest", "--universe", p["universe"], "--top-n", str(p["top_n"])]
        if p.get("since"):
            argv += ["--since", p["since"]]
        return [Step(tuple(argv))]
    if key == "bt_lab":
        return [Step((PY, "-m", "app.lab.run"))]
    if key == "bt_lab_reveal":
        return [Step((PY, "-m", "app.lab.run", "--reveal", "combo_top", "combo_trend", "combo_all_pos",
                      "btc_ma50", "tsmom_regime", "tsmom_14"))]
    if key == "portfolio_run":
        return [Step((PY, "-m", "app.portfolio.service", "--once"))]
    if key == "whales_once":
        return [Step((PY, "-m", "app.whales.collector", "--once"))]
    raise InvalidParams("کار ناشناخته")


def restarts_runner(key: str, p: dict) -> bool:
    return KINDS[key].restarts_runner or (key == "restart" and p.get("service") in ("all", RUNNER))


def describe() -> list[dict]:
    """What the panel needs to draw the forms."""
    return [{"key": k.key, "label": k.label, "help": k.help, "group": k.group, "dangerous": k.dangerous,
             "params": [{"name": p.name, "label": p.label, "kind": p.kind, "default": p.default,
                         "choices": [list(c) for c in p.choices], "lo": p.lo, "hi": p.hi, "optional": p.optional}
                        for p in k.params]} for k in KINDS.values()]
