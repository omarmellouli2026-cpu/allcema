// يرسل الروابط الجديدة/المحدَّثة إلى IndexNow (Bing وYandex وغيرهما). لا يخص Google.
// الاستخدام: SITE_URL=... INDEXNOW_KEY=... node scripts/indexnow.mjs [dist] [ساعات=3]
import fs from 'node:fs';
const [dist = 'dist', hours = '3'] = process.argv.slice(2);
const site = process.env.SITE_URL, key = process.env.INDEXNOW_KEY;
if (!site || !key) { console.log('indexnow: تخطّي (SITE_URL/INDEXNOW_KEY غير مضبوطين)'); process.exit(0); }
const since = Date.now() - Number(hours) * 3600e3;
const urls = [];
for (const f of fs.readdirSync(dist).filter((n) => /^sitemap-\d+\.xml$/.test(n))) {
  for (const m of fs.readFileSync(`${dist}/${f}`, 'utf8').matchAll(/<loc>([^<]+)<\/loc><lastmod>([^<]+)<\/lastmod>/g))
    if (Date.parse(m[2]) >= since) urls.push(m[1]);
}
if (!urls.length) { console.log('indexnow: لا روابط جديدة'); process.exit(0); }
const host = new URL(site).host;
for (let i = 0; i < urls.length; i += 9000) {
  const r = await fetch('https://api.indexnow.org/indexnow', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ host, key, keyLocation: `${site}/indexnow-key.txt`, urlList: urls.slice(i, i + 9000) }) });
  console.log('indexnow:', r.status, Math.min(9000, urls.length - i), 'url');
}
