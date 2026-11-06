import fs from 'node:fs';
import path from 'node:path';

const DIR = process.env.DATA_DIR || path.resolve('../elcinema_data');
const DRAFTS = process.env.INCLUDE_DRAFTS === '1';

export const slugify = (s) =>
  String(s || '').normalize('NFKC').replace(/[ً-ْـ]/g, '').trim()
    .replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-+|-+$/g, '').slice(0, 60) || 'x';

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

let cache;
export function load() {
  if (cache) return cache;
  const works = readJsonl('site_items.jsonl').map((o) => ({ ...o, kind: 'work', id: o.work_id,
    path: `/work/${o.work_id}-${slugify(o.facts.title)}/` }));
  const byId = new Map(works.map((w) => [w.id, w]));
  const episodes = readJsonl('recaps.jsonl').map((o) => {
    const wid = o.facts.work_id, ep = o.facts.episode_no;
    return { ...o, kind: 'episode', wid, ep, parent: byId.get(wid) || null,
      path: `/episode/${wid}-${ep}-${slugify(o.facts.title)}/` };
  });
  for (const w of works) w.episodes = episodes.filter((e) => e.wid === w.id).sort((a, b) => a.ep - b.ep);
  for (const e of episodes) {
    const sib = episodes.filter((x) => x.wid === e.wid).sort((a, b) => a.ep - b.ep);
    const i = sib.indexOf(e);
    e.prev = sib[i - 1] || null; e.next = sib[i + 1] || null;
  }
  const byDate = (a, b) => String(b.at).localeCompare(String(a.at));
  const years = [...new Set(works.map((w) => w.facts.year).filter(Boolean))].sort((a, b) => b - a);
  cache = { works: works.sort(byDate), episodes: episodes.sort(byDate), years };
  return cache;
}

export const desc = (s, n = 155) => {
  const t = String(s || '').replace(/\s+/g, ' ').trim();
  return t.length <= n ? t : t.slice(0, n - 1).replace(/\s+\S*$/, '') + '…';
};
export const fmtDate = (iso) => (iso ? String(iso).slice(0, 10) : '');
