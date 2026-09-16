# راه‌اندازی نسخه حرفه‌ای ربات مدیریت گروه بله

## نصب سریع

```bash
cd /root/bale_group_manager
source venv/bin/activate
pip install -r requirements.txt
python bot.py
```

اگر سرویس systemd داری:

```bash
sudo systemctl restart bale-group-manager
sudo journalctl -u bale-group-manager -f
```

## تنظیمات کارت به کارت

داخل فایل `.env`:

```env
CARD_NUMBER=6037990000000000
CARD_HOLDER=نام دارنده کارت
PLAN_PRICE_IRR=99000
DEFAULT_PLAN_DAYS=30
```

## تنظیمات پرداخت بله

```env
PAYMENTS_ENABLED=1
BALE_PROVIDER_TOKEN=توکن پرداخت بله
```

اگر پرداخت بله تنظیم نباشد، ربات خودکار کارت‌به‌کارت را نشان می‌دهد.

## دسترسی‌ها

ربات باید در گروه ادمین باشد و دسترسی حذف پیام/بن داشته باشد تا قفل‌ها، ضداسپم، بن و میوت درست کار کنند.



## آپدیت فروش از پیوی

از این نسخه به بعد تنظیمات مالی از داخل پیوی مالک انجام می‌شود. داخل پیوی ربات بزن:

```text
پنل مالک
```

خرید اشتراک هم فقط داخل پیوی انجام می‌شود. داخل گروه دستور زیر فقط وضعیت اشتراک و پلن‌ها را نشان می‌دهد:

```text
اشتراک
```

برای توضیح کامل‌تر فایل `README_PM_SALES.md` را ببین.
