# telec-signal

سامانهٔ سیگنال کریپتو — `signal.telec.shop`

## وضعیت

| مرحله | بخش | وضعیت |
| --- | --- | --- |
| ۱ | موتور داده و دیتابیس | ✅ آماده برای تست روی سرور |
| ۲ | اندیکاتورها و موتور تحلیل | ✅ آماده برای تست روی سرور |
| ۳ | مدیریت ریسک و تطبیق با ال‌بانک | ⏳ |
| ۴ | چرخه عمر سیگنال و بات تلگرام | ⏳ |
| ۵ | بک‌تست | ⏳ |
| ۶ | پنل وب | ⏳ |

## موتور داده چه می‌کند

- هر روز ۱۰۰ ارز برتر فیوچرز بایننس (بر اساس حجم) را که در فیوچرز ال‌بانک هم هستند پیدا می‌کند و در `universe_snapshots` ذخیره می‌کند.
- از بین آن‌ها ۴۰ ارز (قابل تنظیم) را برای تحلیل کامل انتخاب می‌کند: ۷۰٪ نقدشوندگی + ۳۰٪ نوسان، BTC و ETH همیشه.
- کندل‌های ۱۵ دقیقه، ۱ ساعت، ۴ ساعت و روزانه را از ۱۲۰ روز قبل پر می‌کند و بعد به‌صورت زنده از WebSocket می‌گیرد.
- **فقط کندل بسته‌شده** ذخیره می‌شود. کندل ناقص، تکراری، نامرتب یا با قیمت نامعتبر رد و در `data_issues` ثبت می‌شود.
- هر ساعت شکاف‌های داده را پیدا و از REST پر می‌کند.
- هر ۵ دقیقه Funding Rate و Open Interest را ذخیره می‌کند.
- اگر WebSocket ساکت شد یا کندل جدید نرسید، به تلگرام مدیر هشدار می‌دهد.

## موتور تحلیل چه می‌کند

- ۲۵ ثانیه بعد از بسته شدن هر کندل ۱۵ دقیقه، ارزهای فهرست تحلیل را بررسی می‌کند (سرویس `telec-analysis`).
- وضعیت بازار ۱ساعته: روندی صعودی/نزولی، رنج، فشرده یا آشفته؛ جهت کلی از ۴ساعته.
- سبک‌ها: پولبک روندی، شکست محدوده فشرده، برگشت به میانگین (فقط رنج)، شکار نقدینگی، واگرایی RSI، و VWAP/حجم به‌عنوان فیلتر.
- قواعد: حداقل دو سبک اصلی هم‌جهت با قدرت ۰٫۶+، بدون سبک مخالف قوی، امتیاز ۷۰+، حد ضرر بین ۰٫۵ تا ۳ ATR ساعتی.
- فیلتر بازار: آشفتگی یا حرکت شدید بیت‌کوین سیگنال را متوقف می‌کند؛ روند مخالف بیت‌کوین و فاندینگ یک‌طرفه امتیاز را کم می‌کند.
- خروجی در جدول `signal_candidates`؛ آخرین وضعیت هر ارز در `analysis_state`. ارسال به تلگرام در مرحله ۴.

```bash
.venv/bin/python scripts/analyze_now.py            # تحلیل همین لحظه همه ارزها (بدون ذخیره)
.venv/bin/python scripts/analyze_now.py SOLUSDT    # جزئیات یک ارز
journalctl -u telec-analysis -f
```

## نصب روی سرور (Ubuntu 24.04)

```bash
sudo git clone https://github.com/taghvamohammad-art/telec-signal /opt/telec-signal
cd /opt/telec-signal
sudo bash deploy/install.sh
```

چون مخزن خصوصی است، `git clone` نام کاربری و رمز می‌خواهد: نام کاربری گیت‌هاب و به‌جای رمز یک **Fine-grained token** با دسترسی فقط‌خواندنی (Contents: Read) به همین مخزن (GitHub → Settings → Developer settings → Personal access tokens).

اسکریپت نصب این کارها را می‌کند: PostgreSQL و TimescaleDB، Swap دو گیگی (برای سرور کم‌رم)، ساخت دیتابیس و فایل `.env`، محیط پایتون، اجرای تست‌ها، تست اتصال به بایننس و ال‌بانک، و راه‌اندازی سرویس `telec-data`.

برای هشدار تلگرام، `ADMIN_BOT_TOKEN` و `ADMIN_CHAT_ID` را در `/opt/telec-signal/.env` بنویسید و سرویس را ری‌استارت کنید:

```bash
sudo systemctl restart telec-data
```

## دستورهای کاربردی

```bash
journalctl -u telec-data -f                                  # لاگ زنده
sudo systemctl status telec-data                             # وضعیت
cd /opt/telec-signal && .venv/bin/python scripts/check_connectivity.py   # تست اتصال
sudo git pull && sudo bash deploy/install.sh                 # به‌روزرسانی
```

بررسی داده در دیتابیس:

```bash
sudo -u postgres psql telec_signal -c "SELECT symbol, tf, count(*), max(open_time) FROM candles GROUP BY 1,2 ORDER BY 1,2 LIMIT 20;"
sudo -u postgres psql telec_signal -c "SELECT ts, symbol, tf, kind, detail FROM data_issues ORDER BY ts DESC LIMIT 20;"
```

## GitHub Runner (اختیاری، توصیه‌شده)

با نصب Runner روی همین سرور، با هر تغییر کد تست‌ها و اتصال واقعی به صرافی‌ها خودکار اجرا می‌شود.
در گیت‌هاب: Settings → Actions → Runners → New self-hosted runner → Linux، و دستورهایی که نشان می‌دهد را در `/opt/actions-runner` اجرا کنید (کاربر غیر root). سپس:

```bash
sudo ./svc.sh install && sudo ./svc.sh start
```

## نکته‌های فنی

- آدرس WebSocket بایننس `wss://fstream.binance.com/market` است؛ بایننس در ۲۰۲۶ مسیرها را جدا کرد و آدرس قدیمی دیگر کندل نمی‌فرستد.
- اگر بایننس سرور را از نظر موقعیت مسدود کند (کد 451)، سرویس با کد ۲ متوقف می‌شود و دوباره تلاش نمی‌کند.
- تست‌ها: `.venv/bin/python -m pytest -q`
