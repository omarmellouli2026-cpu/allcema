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
fs.writeFileSync('public/_redirects', lines.join('\n') + '\n');
console.log(`redirects: ${lines.length}`);
