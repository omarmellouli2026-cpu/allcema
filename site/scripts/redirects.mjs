// يولّد public/_redirects: تحويل روابط النسخة الأولى (/episode/ و /work/) إلى الهيكل الجديد /series/
import fs from 'node:fs';
import { load, slugify } from '../src/lib/data.js';
const { series, episodes } = load();
const old = (p) => encodeURI(p);
const lines = [
  ...series.map((s) => `${old(`/work/${s.id}-${slugify(s.title)}/`)} ${old(s.path)} 301`),
  ...episodes.map((e) => `${old(`/episode/${e.wid}-${e.ep}-${slugify(e.series.title)}/`)} ${old(e.path)} 301`),
].slice(0, 500);
fs.mkdirSync('public', { recursive: true });
// رسوم الذكاء الاصطناعي تُحفظ نصًّا (base64) في media/ وتُفكّ إلى public/media/ عند البناء
if (fs.existsSync('media')) {
  fs.mkdirSync('public/media', { recursive: true });
  for (const f of fs.readdirSync('media').filter((x) => x.endsWith('.b64'))) {
    fs.writeFileSync(`public/media/${f.replace(/\.b64$/, '.jpg')}`, Buffer.from(fs.readFileSync(`media/${f}`, 'utf8').trim(), 'base64'));
  }
  if (fs.existsSync('media/pack.json')) {
    const pack = JSON.parse(fs.readFileSync('media/pack.json', 'utf8'));
    for (const [k, v] of Object.entries(pack)) fs.writeFileSync(`public/media/${k}.jpg`, Buffer.from(v, 'base64'));
  }
}
fs.writeFileSync('public/_redirects', lines.join('\n') + '\n');
console.log(`redirects: ${lines.length}`);
