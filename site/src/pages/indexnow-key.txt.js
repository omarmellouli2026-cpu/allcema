export function GET() { return new Response(process.env.INDEXNOW_KEY || '', { headers: { 'Content-Type': 'text/plain' } }); }
