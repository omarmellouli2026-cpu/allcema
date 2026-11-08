import { load } from '../lib/data.js';
export function GET() {
  const { series, episodes, films } = load();
  const rows = [...series.map((s) => ({ t: s.title, a: s.alt, p: s.path, x: s.blurb, k: 's' })),
    ...films.map((m) => ({ t: `فيلم ${m.title}`, a: m.alt, p: m.path, x: m.blurb, k: 'f' })),
    ...episodes.map((e) => ({ t: `مسلسل ${e.series.title} حلقة ${e.ep}`, p: e.path, x: e.excerpt, k: 'e', n: e.numeral }))];
  return new Response(JSON.stringify(rows), { headers: { 'Content-Type': 'application/json; charset=utf-8' } });
}
