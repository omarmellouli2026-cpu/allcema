import { load } from '../lib/data.js';
export function GET() {
  const { series, episodes } = load();
  const rows = [...series.map((s) => ({ t: s.title, a: s.alt, p: s.path, x: s.blurb, k: 's' })),
    ...episodes.map((e) => ({ t: `مسلسل ${e.series.title} حلقة ${e.ep}`, p: e.path, x: e.excerpt, k: 'e', n: e.numeral }))];
  return new Response(JSON.stringify(rows), { headers: { 'Content-Type': 'application/json; charset=utf-8' } });
}
