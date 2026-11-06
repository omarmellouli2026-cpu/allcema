# تشغيل Allcema: 4 خطوات فقط

## 1) GitHub (10 دقائق)
1. أنشئ مستودعًا **خاصًا (Private)** باسم `allcema` على github.com.
2. **الأسهل:** اربط GitHub بحسابك في Claude ثم أرسل لي `اسمك/allcema` وأنا أرفع كل الملفات.
   أو **يدويًا:** في صفحة المستودع Add file ← Upload files، واسحب محتويات هذا المجلد كلها (بما فيها مجلد `.github` و`site`).
3. Settings ← Actions ← General ← Workflow permissions ← **Read and write permissions** ← Save.

## 2) المفاتيح (أنت وحدك تضعها)
Settings ← Secrets and variables ← Actions:
| النوع | الاسم | القيمة |
|---|---|---|
| Secret | `ANTHROPIC_API_KEY` | من console.anthropic.com ← API Keys |
| Secret | `CLOUDFLARE_API_TOKEN` | Cloudflare ← My Profile ← API Tokens ← Create (قالب «Edit Cloudflare Pages») |
| Secret | `CLOUDFLARE_ACCOUNT_ID` | Cloudflare ← أي موقع ← العمود الأيمن Account ID |
| Variable | `SITE_URL` | `https://allcema.com` |
| Variable | `CF_PROJECT` | `allcema` |
| Variable | `SITE_TARGET` | `jsonl` |
| Variable | `PUBLISH_STATUS` | `draft` (غيّره إلى `publish` بعد مراجعة المقالات) |
| Variable | `CONTACT_EMAIL` | بريدك |
| Variable | `DISCLOSURE_LINE` | مثال: أُعدّ بمساعدة الذكاء الاصطناعي وراجعه فريق التحرير |

## 3) Cloudflare (15 دقيقة)
1. Workers & Pages ← Create ← Pages ← **Direct Upload** ← سمّه `allcema` (مشروع فارغ).
2. أضف `allcema.com` في Add a site (Free) وغيّر الـnameservers عند NamesLink إلى ما يعطيك إياه Cloudflare.
3. في المشروع ← Custom domains ← أضف `allcema.com`.

## 4) أول تشغيل
Actions ← `elcinema-auto` ← Run workflow. راقب الخطوات. أرسل لي أي خطأ يظهر وأصلحه.
بعد نجاحه يبدأ `deploy-site` وحده. ثم سجّل الموقع في Google Search Console وقدّم `https://allcema.com/sitemap.xml`.
