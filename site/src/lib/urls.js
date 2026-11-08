import { load } from './data.js';
export function allUrls(site) {
  const { series, episodes, films } = load();
  const abs = (p) => new URL(p, site).href;
  const latest = (list) => list.reduce((m, x) => (String(x.at) > m ? String(x.at) : m), '');
  const out = [{ loc: abs('/'), lastmod: latest(episodes) }, { loc: abs('/series/'), lastmod: latest(series) },
    { loc: abs('/episodes/'), lastmod: latest(episodes) }];
  for (const s of series) if (!s.noindex) out.push({ loc: abs(s.path), lastmod: String(s.at) });
  for (const e of episodes) if (!e.series.noindex) out.push({ loc: abs(e.path), lastmod: String(e.at) });
  if (films.length) out.push({ loc: abs('/films/'), lastmod: latest(films) });
  for (const m of films) out.push({ loc: abs(m.path), lastmod: String(m.at) });
  for (const p of ['/about/', '/privacy/', '/contact/']) out.push({ loc: abs(p), lastmod: '' });
  return out;
}
export const CHUNK = 40000;
export const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
