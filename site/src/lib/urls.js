import { load } from './data.js';
export function allUrls(site) {
  const { works, episodes, years } = load();
  const abs = (p) => new URL(p, site).href;
  const latest = (list) => list.reduce((m, x) => (String(x.at) > m ? String(x.at) : m), '');
  const out = [{ loc: abs('/'), lastmod: latest([...works, ...episodes]) }];
  for (const y of years) out.push({ loc: abs(`/year/${y}/`), lastmod: latest(works.filter((w) => w.facts.year === y)) });
  for (const w of works) out.push({ loc: abs(w.path), lastmod: String(w.at) });
  for (const e of episodes) out.push({ loc: abs(e.path), lastmod: String(e.at) });
  for (const p of ['/about/', '/privacy/', '/contact/']) out.push({ loc: abs(p), lastmod: '' });
  return out;
}
export const CHUNK = 40000;
export const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
