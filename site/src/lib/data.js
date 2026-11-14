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
// رسوم الذكاء الاصطناعي لأعمال رمضان: media/pack.json = { g2027001: base64, ... }
const PACK = (() => { try { return JSON.parse(fs.readFileSync(path.resolve('media', 'pack.json'), 'utf8')); } catch { return {}; } })();
// الملصقات الأصلية (بترخيص كتابي): تُنزَّل إلى public/media/posters/<id>.jpg بواسطة workflow باسم posters
const POSTER = (id) => (fs.existsSync(path.resolve('public', 'media', 'posters', `${id}.jpg`)) ? `/media/posters/${id}.jpg` : '');
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
// تطبيع الأسماء: يوحّد أشكال العرض (ﻛﻮﻣﻴﺪﻱ → كوميدي) ويزيل التشكيل والمسافات الزائدة
const norm = (s) => String(s || '').normalize('NFKC').replace(DIAC, '').replace(/\s+/g, ' ').trim().replace(/^(المملكة العربية السعودية|المملكة السعودية)$/, 'السعودية');
const escH = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
export const roleText = (e) => { const a = []; if (e.roles.has('dir')) a.push('مخرج'); if (e.roles.has('writer')) a.push('مؤلف'); if (e.roles.has('cast')) a.push('ممثل'); return joinAr(a) || 'من صنّاع الأعمال'; };
function buildTax(works) {
  const people = new Map(), genres = new Map(), countries = new Map(), used = new Set();
  const add = (m, name, w, role) => {
    const k = norm(name); if (!k) return;
    let e = m.get(k);
    if (!e) { let slug = slugify(k); while (used.has(m === people ? 'p' + slug : m === genres ? 'g' + slug : 'c' + slug)) slug += '-2'; used.add((m === people ? 'p' : m === genres ? 'g' : 'c') + slug);
      e = { name: k, slug, works: new Map(), roles: new Set() }; m.set(k, e); }
    if (!e.works.has(w)) e.works.set(w, new Set());
    if (role) { e.works.get(w).add(role); e.roles.add(role); }
  };
  for (const w of works) { const f = w.facts;
    (f.directors || []).forEach((n) => add(people, n, w, 'dir')); (f.writers || []).forEach((n) => add(people, n, w, 'writer')); (f.cast || []).forEach((n) => add(people, n, w, 'cast'));
    (f.genres || []).forEach((n) => add(genres, n, w)); (f.countries || []).forEach((n) => add(countries, n, w)); }
  const fin = (m, min) => [...m.values()].filter((e) => e.works.size >= min).map((e) => ({ ...e, list: [...e.works.keys()], count: e.works.size }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name, 'ar'));
  const P = fin(people, 2), G = fin(genres, 2), C = fin(countries, 1);
  return { people: P, genres: G, countries: C, personBy: new Map(P.map((e) => [e.name, e])), genreBy: new Map(G.map((e) => [e.name, e])), countryBy: new Map(C.map((e) => [e.name, e])) };
}
// يحوّل قائمة أسماء إلى روابط HTML لصفحات الأشخاص/الأنواع/الدول عند وجودها
export function linkList(kind, names) {
  const t = load().tax, by = kind === 'p' ? t.personBy : kind === 'g' ? t.genreBy : t.countryBy, base = kind === 'p' ? '/people/' : kind === 'g' ? '/genres/' : '/countries/';
  return [...new Set((names || []).map(norm).filter(Boolean))].map((n) => (by.has(n) ? `<a href="${base}${by.get(n).slug}/">${escH(n)}</a>` : escH(n))).join('، ');
}
// أعمال مشابهة: تشابه في الممثلين (×2) والمخرجين (×3) والأنواع والمؤلفين، مع أفضلية بسيطة لنفس النوع (مسلسل/فيلم)
export function related(w, n = 4) {
  const L = load(), pool = [...L.live.filter((x) => !x.noindex), ...L.films].filter((x) => x !== w);
  const set = (a) => new Set((a || []).map(norm)), f = w.facts;
  const C = set(f.cast), G = set(f.genres), Dr = set(f.directors), Wr = set(f.writers);
  const hit = (a, S) => (a || []).reduce((c, x) => c + (S.has(norm(x)) ? 1 : 0), 0);
  return pool.map((x) => ({ x, sc: hit(x.facts.cast, C) * 2 + hit(x.facts.directors, Dr) * 3 + hit(x.facts.genres, G) + hit(x.facts.writers, Wr) + (x.isFilm === w.isFilm ? 0.5 : 0) }))
    .filter((r) => r.sc >= 2).sort((a, b) => b.sc - a.sc || String(b.x.at).localeCompare(String(a.x.at))).slice(0, n).map((r) => r.x);
}
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
  // توحيد أشكال الحروف في الأسماء والأنواع (ﻛﻮﻣﻴﺪﻱ → كوميدي) قبل أي عرض أو تجميع
  const fixNames = (f) => { if (!f) return; for (const k of ['genres', 'cast', 'directors', 'writers', 'countries']) if (Array.isArray(f[k])) f[k] = [...new Set(f[k].map(norm).filter(Boolean))]; };
  items.forEach((o) => fixNames(o.facts)); recs.forEach((o) => fixNames(o.facts));
  const itemBy = new Map(items.map((o) => [o.work_id, o]));
  const groups = new Map();
  for (const o of recs) {
    const wid = o.facts.work_id;
    if (!groups.has(wid)) groups.set(wid, []);
    groups.get(wid).push(o);
  }
  const isFilm = (o) => (o.facts && o.facts.kind) === 'فيلم';
  for (const o of items) if (!groups.has(o.work_id) && !isFilm(o)) groups.set(o.work_id, []);

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
      image: fs.existsSync(path.resolve('media', `${wid}.b64`)) ? `/media/${wid}.jpg` : POSTER(idStr(wid)) || (PACK[idStr(wid)] ? `/media/${idStr(wid)}.jpg` : ''),
      art: !fs.existsSync(path.resolve('media', `${wid}.b64`)) && !POSTER(idStr(wid)) && !!PACK[idStr(wid)],
      item, article: item && item.html && !up ? parseArticle(item.html) : null, excerpt: (item && item.excerpt) || blurb(f, up), blurb: blurb(f, up), episodes: [],
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
  // الأفلام: عناصر site_items من نوع «فيلم»؛ كل فيلم صفحة واحدة بلا حلقات
  const films = items.filter(isFilm).map((o) => {
    const f = o.facts, slug = `${latin(f.alt_title) || slugify(f.title)}-${idStr(o.work_id)}`;
    return { kind: 'film', isFilm: true, id: o.work_id, title: f.title, headline: o.title, alt: f.alt_title || '', slug, path: `/films/${slug}/`, facts: f, item: o,
      hue: [...f.title].reduce((a, c) => a + c.charCodeAt(0), 0) % 360, image: POSTER(idStr(o.work_id)), count: 0, at: o.at || '',
      parsed: parseArticle(o.html), excerpt: o.excerpt || blurb(f, false), blurb: o.excerpt || blurb(f, false), schema: o.schema || null,
      mins: Math.max(1, Math.round(strip(o.html).split(/\s+/).length / 180)) };
  }).sort((a, b) => String(b.at).localeCompare(String(a.at)));
  const live = all.filter((x) => !x.upcoming);
  cache = { films, series: all, episodes: episodes.sort(byDate), live, upcoming: all.filter((x) => x.upcoming), tax: buildTax([...live.filter((x) => !x.noindex), ...films]) };
  return cache;
}

// الأعمال الأكثر بحثًا ومشاهدة اليوم: ملف trending.json يكتبه workflow يومي (ترتيب تقديري من بحث الويب، لا إحصاء رسمي)
let trendCache;
export function trending() {
  if (trendCache) return trendCache;
  let raw = null;
  try { raw = JSON.parse(fs.readFileSync(path.join(DIR, 'trending.json'), 'utf8')); } catch { raw = null; }
  const L = load();
  const bySeries = new Map(L.live.filter((x) => !x.noindex).map((x) => [Number(x.id), x]));
  const byFilm = new Map(L.films.map((x) => [Number(x.id), x]));
  const pick = (arr, by) => (Array.isArray(arr) ? arr : []).map((e) => ({ ...e, work: by.get(Number(e.work_id)) || null })).filter((e) => e.work);
  trendCache = { date: (raw && raw.date) || '', series: pick(raw && raw.series, bySeries).slice(0, 10), films: pick(raw && raw.films, byFilm).slice(0, 10) };
  return trendCache;
}
