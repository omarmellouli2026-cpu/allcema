import fs from 'node:fs';
import path from 'node:path';

const DIR = process.env.DATA_DIR || path.resolve('../elcinema_data');
const DRAFTS = process.env.INCLUDE_DRAFTS === '1';
const BUILT = Date.now();

const DIAC = new RegExp('[' + String.fromCharCode(1611) + '-' + String.fromCharCode(1618) + String.fromCharCode(1600) + ']', 'g');   // التشكيل والتطويل
export const slugify = (s) =>
  String(s || '').normalize('NFKC').replace(DIAC, '').trim()
    .replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-+|-+$/g, '').slice(0, 60) || 'x';
const latin = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
const idStr = (n) => (n < 0 ? `g${-n}` : String(n));
const strip = (h) => String(h || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

function readJsonl(file) {
  const p = path.join(DIR, file);
  if (!fs.existsSync(p)) return [];
  const m = new Map();                       // آخر سطر لكل عمل يغلب (التحديثات تُلحَق بالملف)
  for (const line of fs.readFileSync(p, 'utf8').split('\n')) {
    if (!line.trim()) continue;
    try { const o = JSON.parse(line); m.set(o.work_id, o); } catch { /* سطر تالف */ }
  }
  return [...m.values()].filter((o) => DRAFTS || o.status === 'publish');
}

export const joinAr = (a) => {
  const l = (a || []).filter(Boolean);
  if (l.length <= 1) return l.join('');
  return l.slice(0, -1).join('، ') + ' و' + l[l.length - 1];
};
export const num = (n) => String(Number(n));
export const minutes = (n) => (n === 1 ? 'دقيقة واحدة' : n === 2 ? 'دقيقتان' : n <= 10 ? `${n} دقائق` : `${n} دقيقة`);
export const fmtDate = (iso) => {
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleDateString('ar-EG-u-nu-latn', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' });
};
export const desc = (s, n = 155) => {
  const t = String(s || '').replace(/\s+/g, ' ').trim();
  return t.length <= n ? t : t.slice(0, n - 1).replace(/\s+\S*$/, '') + '…';
};

// تفكيك مقال الحلقة المخزَّن كـ HTML إلى أجزاء يعيد القالب ترتيبها
export function parseArticle(html) {
  let note = '', spoiler = false;
  let body = String(html || '').replace(/<p><small>([\s\S]*?)<\/small><\/p>/g, (m, t) => { note = strip(t); return ''; });
  body = body.replace(/<p><strong>تنبيه[^<]*<\/strong><\/p>/, () => { spoiler = true; return ''; });
  const parts = body.split(/<h2>([\s\S]*?)<\/h2>/);
  const lead = parts[0].trim();
  const sections = [];
  let card = [], faq = [];
  for (let i = 1; i < parts.length; i += 2) {
    const title = strip(parts[i]), inner = (parts[i + 1] || '').trim();
    if (title === 'بطاقة العمل') card = [...inner.matchAll(/<tr><th>([\s\S]*?)<\/th><td>([\s\S]*?)<\/td><\/tr>/g)].map((m) => [strip(m[1]), strip(m[2])]);
    else if (title === 'أسئلة شائعة') faq = [...inner.matchAll(/<h3>([\s\S]*?)<\/h3>\s*<p>([\s\S]*?)<\/p>/g)].map((m) => ({ q: strip(m[1]), a: strip(m[2]) }));
    else if (title === 'أعمال ذات صلة') continue;
    else sections.push({ title, html: inner });
  }
  return { spoiler, lead, sections, card, faq, note };
}

function blurb(f, up) {
  if (up) {
    const u = [`مسلسل مصري مرتقب ضمن أعمال رمضان ${f.year}`];
    if (f.cast?.length) u.push('بطولة ' + joinAr(f.cast.slice(0, 4)));
    if (f.directors?.length) u.push('وإخراج ' + joinAr(f.directors));
    return u.join('، ') + '.';
  }
  const kind = f.kind || f.category || 'عمل';
  const a = [`${kind}${f.countries?.length ? ' من إنتاج ' + joinAr(f.countries) : ''}${f.year ? ' عُرض عام ' + f.year : ''}`];
  if (f.genres?.length) a.push('من فئة ' + joinAr(f.genres));
  if (f.cast?.length) a.push('بطولة ' + joinAr(f.cast.slice(0, 4)));
  if (f.directors?.length) a.push('وإخراج ' + joinAr(f.directors));
  return a.join('، ') + '.';
}

let cache;
export function load() {
  if (cache) return cache;
  const recs = readJsonl('recaps.jsonl');
  const items = readJsonl('site_items.jsonl');
  const itemBy = new Map(items.map((o) => [o.work_id, o]));
  const groups = new Map();
  for (const o of recs) {
    const wid = o.facts.work_id;
    if (!groups.has(wid)) groups.set(wid, []);
    groups.get(wid).push(o);
  }
  for (const o of items) if (!groups.has(o.work_id)) groups.set(o.work_id, []);

  const series = [], episodes = [];
  for (const [wid, recList] of groups) {
    const item = itemBy.get(wid);
    const eps0 = [...recList].sort((a, b) => a.facts.episode_no - b.facts.episode_no);
    const f = item ? item.facts : eps0[eps0.length - 1].facts;
    const title = f.title;
    const slug = `${latin(f.alt_title) || slugify(title)}-${idStr(wid)}`;
    const up = !!(item && item.upcoming);
    const s = {
      upcoming: up, noindex: !!(item && item.noindex),
      kind: 'series', id: wid, title, alt: f.alt_title || '', slug, path: `/series/${slug}/`, facts: f,
      hue: [...title].reduce((a, c) => a + c.charCodeAt(0), 0) % 360,
      image: fs.existsSync(path.resolve('media', `${wid}.b64`)) ? `/media/${wid}.jpg` : '',
      item, excerpt: (item && item.excerpt) || blurb(f, up), blurb: blurb(f, up), episodes: [],
    };
    s.episodes = eps0.map((o) => {
      const ep = o.facts.episode_no;
      return { ...o, kind: 'episode', wid, ep, series: s, numeral: num(ep), parsed: parseArticle(o.html),
        path: `${s.path}episode-${ep}/`, fresh: BUILT - new Date(o.at).getTime() < 48 * 3600e3 };
    });
    s.episodes.forEach((e, i) => { e.prev = s.episodes[i - 1] || null; e.next = s.episodes[i + 1] || null;
      e.mins = Math.max(1, Math.round(strip(e.html).split(/\s+/).length / 180)); });
    s.latest = s.episodes[s.episodes.length - 1] || null;   // أعلى رقم حلقة
    const newest = s.episodes.reduce((m, e) => (!m || String(e.at) > String(m.at) ? e : m), null);
    s.at = newest ? newest.at : (item ? item.at : '');
    s.count = s.episodes.length;
    series.push(s); episodes.push(...s.episodes);
  }
  const byDate = (a, b) => String(b.at).localeCompare(String(a.at)) || (b.ep || 0) - (a.ep || 0);
  const all = series.sort(byDate);
  cache = { series: all, episodes: episodes.sort(byDate), live: all.filter((x) => !x.upcoming), upcoming: all.filter((x) => x.upcoming) };
  return cache;
}
