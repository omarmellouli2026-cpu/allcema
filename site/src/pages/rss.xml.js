import { load, desc } from '../lib/data.js';
import { SITE_NAME, SITE_TAGLINE } from '../config.js';
const x = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
export function GET({ site }) {
  const items = load().episodes.slice(0, 50);
  const xml = `<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>${x(SITE_NAME)}</title><link>${site.href}</link><description>${x(SITE_TAGLINE)}</description><language>ar</language>` +
    items.map((i) => `<item><title>${x(i.title)}</title><link>${new URL(i.path, site).href}</link><guid>${new URL(i.path, site).href}</guid><pubDate>${new Date(i.at).toUTCString()}</pubDate><description>${x(desc(i.excerpt, 200))}</description></item>`).join('') + '</channel></rss>';
  return new Response(xml, { headers: { 'Content-Type': 'application/rss+xml; charset=utf-8' } });
}
