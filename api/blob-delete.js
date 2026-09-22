import { del } from '@vercel/blob';

function json(data, status = 200) { return Response.json(data, { status }); }

export default async function handler(request) {
  if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405 });
  const expected = process.env.DASHBOARD_KEY || '';
  const supplied = request.headers.get('x-dashboard-key') || '';
  if (!expected || supplied !== expected) return json({ error: 'Unauthorized' }, 401);
  try {
    const { url } = await request.json();
    const parsed = new URL(String(url || ''));
    if (parsed.protocol !== 'https:' || !parsed.hostname.endsWith('.public.blob.vercel-storage.com')) {
      return json({ error: 'Only Vercel Blob URLs can be deleted.' }, 400);
    }
    await del(parsed.toString());
    return json({ ok: true, deleted: parsed.toString() });
  } catch (error) {
    return json({ error: error instanceof Error ? error.message : 'Blob deletion failed.' }, 400);
  }
}
