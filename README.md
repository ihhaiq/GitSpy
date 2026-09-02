# GitSpy

خدمة مركزية تستقبل التعليقات الجديدة من جميع مستودعات GitHub في حسابك، ثم ترسلها فوراً إلى مجموعة أو قناة Telegram.

## الأحداث المدعومة

- تعليق جديد على Issue.
- تعليق جديد داخل محادثة Pull Request.
- تعليق مراجعة جديد على سطر كود في Pull Request.
- تعليق جديد على Discussion.
- تعليق جديد على Commit.
- Push جديد يحتوي تعديلات وCommits.
- Merge جديد عند دمج Pull Request.

التعديل والحذف لا يُرسلان. الخدمة تتحقق من توقيع GitHub، ترفض الأحداث الصادرة من مالك آخر، وتمنع تكرار GitHub Delivery أثناء عمل النسخة الحالية.

تصل الرسالة كجدول Rich Message حقيقي. التعليقات تستخدم صف عنوان مدمج باسم **تعليق جديد** وتحته تفاصيل التعليق. وعند الـPush يظهر **Push جديد**، وعند دمج Pull Request يظهر **Merge جديد**. حقل الـCommits يعرض المعرّف المختصر والرسالة بدون اسم الكاتب. التحديثات المتقاربة في المستودع نفسه تعدّل الرسالة الحالية خلال 90 ثانية بدل إرسال عدة رسائل. اسم المستودع واسم الكاتب أو الناشر زران غنيان داخل خلايا الجدول، وزر فتح الحدث موجود داخل بلوك التذييل.

## 1. تجهيز بوت Telegram

1. أنشئ بوتاً من `@BotFather` وخذ التوكن.
2. أضف البوت إلى المجموعة، أو أضفه مشرفاً في القناة مع صلاحية نشر الرسائل.
3. احصل على `chat_id`. للقناة العامة يمكن استعمال `@channel_username`، وللمجموعات والقنوات الخاصة استعمل الرقم الذي يبدأ غالباً بـ `-100`.

## 2. النشر على Railway

1. أنشئ مشروعاً من هذا المستودع.
2. أضف المتغيرات التالية في Variables:

| المتغير | القيمة |
|---|---|
| `TELEGRAM_BOT_TOKEN` | توكن BotFather |
| `TELEGRAM_CHAT_ID` | رقم المجموعة/القناة أو `@username` |
| `GITHUB_WEBHOOK_SECRET` | نص سري طويل وعشوائي |
| `GITHUB_OWNER` | `ihhaiq` |

إذا كانت الوجهة موضوعاً داخل Forum أضف `TELEGRAM_MESSAGE_THREAD_ID`.

3. أنشئ Railway Public Domain وانسخ رابطه، مثلاً `https://gitspy-production.up.railway.app`.
4. تأكد أن `https://YOUR_DOMAIN/healthz` يعيد `{"ok": true, ...}`.

## 3. إنشاء GitHub App

من GitHub افتح: **Settings → Developer settings → GitHub Apps → New GitHub App**.

- **GitHub App name:** اسم فريد مثل `GitSpy-ihhaiq`.
- **Homepage URL:** رابط Railway.
- **Webhook:** Active.
- **Webhook URL:** `https://YOUR_DOMAIN/github/webhook`.
- **Webhook secret:** نفس قيمة `GITHUB_WEBHOOK_SECRET` في Railway.

في **Repository permissions** اختر `Read-only` لكل من:

- Issues
- Pull requests
- Discussions
- Contents (مطلوبة لتعليقات الـCommit)

صلاحية **Metadata: Read-only** إلزامية ويضيفها GitHub تلقائياً.

في **Subscribe to events** فعّل:

- Issue comment
- Pull request review comment
- Discussion comment
- Commit comment
- Push
- Pull request

اختر **Only on this account** ثم أنشئ التطبيق. من صفحة التطبيق اضغط **Install App**، اختر حساب `ihhaiq` ثم **All repositories**. هذا يشمل المستودعات العامة والخاصة والمستودعات الجديدة لاحقاً.

> لا تحتاج هذه الخدمة إلى GitHub token أو Private Key؛ استقبال الـWebhook الموقّع كافٍ.

## الاختبار

اكتب تعليقاً جديداً في Issue أو Pull Request. يجب أن تصل رسالة إلى Telegram مع اسم المستودع والكاتب والنص وزر فتح التعليق.

إذا لم تصل الرسالة:

1. افتح GitHub App ثم **Advanced → Recent deliveries**.
2. نتيجة `200` تعني أن GitSpy استلم وأرسل الرسالة.
3. `401` يعني أن Webhook secret مختلف.
4. `502` يعني غالباً أن Telegram رفض التوكن أو `chat_id` أو أن البوت لا يملك صلاحية النشر.

## تشغيل الاختبارات محلياً

```bash
python -m unittest discover -v
```
