import { allUrls, CHUNK, esc } from '../lib/urls.js';
export function getStaticPaths() {
  const n = Math.max(1, Math.ceil(allUrls(new URL('https://x.invalid')).length / CHUNK));
  return Array.from({ length: n }, (_, i) => ({ params: { n: String(i) } }));
}
export function GET({ params, site }) {
  const part = allUrls(site).slice(+params.n * CHUNK, (+params.n + 1) * CHUNK);
  const xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">' +
    part.map((u) => `<url><loc>${esc(u.loc)}</loc>${u.lastmod ? `<lastmod>${u.lastmod}</lastmod>` : ''}${u.img ? `<image:image><image:loc>${esc(u.img)}</image:loc></image:image>` : ''}</url>`).join('') + '</urlset>';
  return new Response(xml, { headers: { 'Content-Type': 'application/xml; charset=utf-8' } });
}
