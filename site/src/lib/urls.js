import { load, trending } from './data.js';
export function allUrls(site) {
  const { series, episodes, films, tax, upcoming } = load();
  const abs = (p) => new URL(p, site).href;
  const latest = (list) => list.reduce((m, x) => (String(x.at) > m ? String(x.at) : m), '');
  const out = [{ loc: abs('/'), lastmod: latest(episodes) }, { loc: abs('/series/'), lastmod: latest(series) },
    { loc: abs('/episodes/'), lastmod: latest(episodes) }];
  for (const s of series) if (!s.noindex) out.push({ loc: abs(s.path), lastmod: String(s.at), img: s.image ? abs(s.image) : '' });
  for (const e of episodes) if (!e.series.noindex) out.push({ loc: abs(e.path), lastmod: String(e.at) });
  const epPages = Math.ceil(episodes.filter((e) => !e.series.noindex).length / 20);
  for (let n = 2; n <= epPages; n++) out.push({ loc: abs(`/episodes/${n}/`), lastmod: latest(episodes) });
  if (films.length) out.push({ loc: abs('/films/'), lastmod: latest(films) });
  for (const m of films) out.push({ loc: abs(m.path), lastmod: String(m.at), img: m.image ? abs(m.image) : '' });
  const lm = latest([...series, ...films]);
  if (tax.genres.length) out.push({ loc: abs('/genres/'), lastmod: lm });
  for (const x of tax.genres) if (x.count >= 6) out.push({ loc: abs(`/genres/${x.slug}/`), lastmod: latest(x.list) });
  for (const x of tax.countries) out.push({ loc: abs(`/countries/${x.slug}/`), lastmod: latest(x.list) });
  // صفحة رمضان 2027 فقط تُفتح للأرشفة؛ صفحات مسلسلاتها تبقى noindex وخارج الخريطة
  if (upcoming.length) { const first = upcoming.find((s) => s.image); out.push({ loc: abs('/ramadan-2027/'), lastmod: latest(upcoming), img: first ? abs(first.image) : '' }); }
  const TR = trending();
  if (TR.series.length + TR.films.length) out.push({ loc: abs('/trending/'), lastmod: TR.date });
  for (const p of ['/about/', '/privacy/', '/contact/', '/copyright/']) out.push({ loc: abs(p), lastmod: '' });
  return out;
}
export const CHUNK = 40000;
export const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
