# آموزش نصب ربات مدیریت گروه بله روی VPS

## پیش‌نیازها

سرور Ubuntu/Debian پیشنهاد می‌شود. با کاربر root یا کاربری که sudo دارد وارد شو.

```bash
apt update
apt install -y python3 python3-venv python3-pip unzip nano
```

## 1. آپلود و استخراج سورس

فایل ZIP را داخل `/root` آپلود کن، بعد بزن:

```bash
cd /root
unzip -o bale_group_manager_latest_full_with_install.zip
cd /root/bale_group_manager
```

اگر اسم فایل ZIP فرق داشت، همان اسم را در دستور `unzip` بگذار.

## 2. ساخت فایل تنظیمات

```bash
cp .env.example .env
nano .env
```

حداقل این دو مورد را تنظیم کن:

```env
BOT_TOKEN=توکن_ربات_بله
OWNER_IDS=آیدی_عددی_خودت
```

برای اینکه callbackهای قدیمی اسپم نزنند، این مقدار بهتر است روشن باشد:

```env
CLEAR_PENDING_ON_START=1
```

## 3. اجرای تستی

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python bot.py
```

اگر بدون خطا اجرا شد، داخل بله ربات را تست کن.

## 4. نصب به عنوان سرویس دائمی

بعد از اینکه تست گرفتی، ربات را با `Ctrl + C` خاموش کن و بزن:

```bash
chmod +x tools/install_service.sh
./tools/install_service.sh
```

بررسی وضعیت سرویس:

```bash
systemctl status bale-group-manager --no-pager
journalctl -u bale-group-manager -f
```

ری‌استارت ربات:

```bash
systemctl restart bale-group-manager
```

خاموش کردن ربات:

```bash
systemctl stop bale-group-manager
```

## 5. نصب ربات داخل گروه

1. ربات را به گروه بله اضافه کن.
2. ربات باید ادمین باشد و دسترسی حذف پیام داشته باشد.
3. داخل گروه بزن:

```text
نصب
```

بعد برای باز کردن پنل بزن:

```text
پنل
```

## 6. تنظیم پرداخت از پیوی مالک

داخل پیوی ربات بزن:

```text
پنل مالک
```

از آنجا می‌توانی شماره کارت، نام صاحب کارت، متن راهنما، توکن پرداخت بله، پلن‌ها و وضعیت پرداخت‌ها را تنظیم کنی.

## 7. تنظیم شخصی‌سازی متن‌ها از پیوی

داخل گروه از پنل، دکمه «🎨 شخصی‌سازی» را بزن تا به پیوی هدایت شوی. یا داخل پیوی بزن:

```text
شخصی سازی آیدی_گروه
```

برای ریست:

```text
ریست متن‌ها آیدی_گروه
ریست دستورها آیدی_گروه
ریست شخصی سازی آیدی_گروه
```

## 8. پاکسازی گروه

داخل پنل دکمه «🧹 پاکسازی گروه» هست. دستورهای دستی:

```text
پاکسازی 100
پاکسازی گروه 1000
پاکسازی گروه کل
```

نکته: ربات فقط پیام‌هایی را می‌تواند حذف کند که بله اجازه حذفشان را بدهد و ربات دسترسی حذف پیام داشته باشد.

## 9. آپدیت نسخه جدید بدون پاک شدن دیتابیس

فرض کن نسخه جدید ZIP داخل `/root` است:

```bash
cd /root
systemctl stop bale-group-manager 2>/dev/null || pkill -f bot.py
cp -r bale_group_manager bale_group_manager_backup_$(date +%Y%m%d_%H%M%S)
cp bale_group_manager/.env /root/.env_backup
cp -r bale_group_manager/data /root/data_backup_$(date +%Y%m%d_%H%M%S) 2>/dev/null
unzip -o bale_group_manager_latest_full_with_install.zip
cp /root/.env_backup /root/bale_group_manager/.env
cd /root/bale_group_manager
source venv/bin/activate 2>/dev/null || true
pip install -r requirements.txt
systemctl restart bale-group-manager 2>/dev/null || python bot.py
```

## 10. خطاهای رایج

### خطای `ModuleNotFoundError: No module named dotenv`

یعنی پکیج‌ها داخل venv نصب نشده‌اند:

```bash
cd /root/bale_group_manager
source venv/bin/activate
pip install -r requirements.txt
python bot.py
```

### خطای PEP 668 یا `externally-managed-environment`

روی سیستم‌های جدید نباید مستقیم با `pip3` روی Python سیستم نصب کنی. حتماً از venv استفاده کن:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### ربات پیام‌ها را حذف نمی‌کند

- ربات باید ادمین گروه باشد.
- دسترسی حذف پیام داشته باشد.
- بعضی پیام‌های قدیمی یا پیام‌های خاص ممکن است توسط بله قابل حذف نباشند.
