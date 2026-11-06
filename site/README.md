# موقع الدراما (Astro — صفحات ثابتة بلا JavaScript)

- **لماذا Astro:** يولّد HTML جاهزًا لكل صفحة، ولا يرسل أي JavaScript للقارئ (تحقّقتُ: صفر `<script src>` في الصفحات).
  هذا أسهل طريق لتحقيق عتبات Core Web Vitals التي توصي بها Google (LCP < 2.5 ثانية، INP < 200 ms، CLS < 0.1).
- **الاستضافة:** Cloudflare Pages (شبكة CDN عالمية).
- **المصدر:** يقرأ `elcinema_data/site_items.jsonl` (الأعمال) و`recaps.jsonl` (الحلقات)، ويعرض **فقط** ما حالته `publish`.
  المسودات لا تظهر (للمعاينة: `INCLUDE_DRAFTS=1`).

## الصفحات
الرئيسية · صفحة لكل عمل `/work/ID-اسم/` تجمع ملخصات حلقاته · صفحة لكل حلقة `/episode/ID-رقم-اسم/` (السابق/التالي) ·
`/year/2026/` · من نحن · الخصوصية · اتصل بنا · 404 · `sitemap.xml` (مقسّم، وlastmod حقيقي) · `robots.txt` · `rss.xml`.
كل صفحة: canonical، OG، JSON‑LD (Movie/TVSeries/TVEpisode + BreadcrumbList)، روابط داخلية، تنبيه حرق.

## تشغيل محلي
```bash
cd site && npm install
DATA_DIR=../elcinema_data INCLUDE_DRAFTS=1 SITE_URL=http://localhost:4321 npm run dev
```

## نشر تلقائي (GitHub Actions → Cloudflare Pages)
1. أنشئ مشروع Pages فارغًا في Cloudflare، وأنشئ API Token بصلاحية Pages.
2. Secrets: `CLOUDFLARE_API_TOKEN`، `CLOUDFLARE_ACCOUNT_ID`، (اختياري) `INDEXNOW_KEY`.
3. Variables: `SITE_URL` (مثل https://allcema.com)، `CF_PROJECT`، `SITE_NAME`، `CONTACT_EMAIL`، `DISCLOSURE_LINE`، و`PUBLISH_STATUS=publish` حين تطمئن للجودة.
4. بعد كل دورة جلب ينجح `elcinema-auto` يعيد `deploy-site` بناء الموقع ونشره تلقائيًا.

## الفهرسة
- بعد أول نشر: أضف الموقع في Google Search Console وقدّم `https://موقعك/sitemap.xml` **مرة واحدة**؛ Google يعيد قراءته وحده (طلب «ping» الخاص بخرائط الموقع أُلغي).
- **لا تستخدم Google Indexing API:** هي رسميًا لوظائف التوظيف والبث المباشر فقط.
- `IndexNow` يبلّغ Bing وYandex بالروابط الجديدة فور النشر (لا يخص Google).
- لا تضع `lastmod` غير صحيح؛ هنا يأتي من تاريخ آخر تحديث فعلي.

## حدود يجب معرفتها
- كل نشر يعيد بناء الموقع كاملًا. مع آلاف الصفحات يستغرق ثوانٍ إلى دقائق؛ ومع عشرات الآلاف راقب زمن البناء.
- Cloudflare Pages المجاني له سقف لعدد الملفات في الموقع (20 ألف وقت كتابة هذا الدليل، تحقّق من الحدّ الحالي).
- الموقع بلا صور حاليًا (للسرعة والتجنّب مشاكل الحقوق). صور الأغلفة تحتاج ترخيصًا صريحًا.
