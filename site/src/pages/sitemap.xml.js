import { allUrls, CHUNK } from '../lib/urls.js';
export function GET({ site }) {
  const n = Math.max(1, Math.ceil(allUrls(site).length / CHUNK));
  const xml = '<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
    Array.from({ length: n }, (_, i) => `<sitemap><loc>${new URL(`/sitemap-${i}.xml`, site).href}</loc></sitemap>`).join('') + '</sitemapindex>';
  return new Response(xml, { headers: { 'Content-Type': 'application/xml; charset=utf-8' } });
}
