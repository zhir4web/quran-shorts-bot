module.exports = async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).send('Method Not Allowed');
  const expected = process.env.DASHBOARD_KEY || '';
  const supplied = req.headers['x-dashboard-key'] || '';
  if (!expected || supplied !== expected) return res.status(401).json({ error: 'Unauthorized' });
  try {
    const body = typeof req.body === 'string' ? JSON.parse(req.body || '{}') : (req.body || {});
    const parsed = new URL(String(body.url || ''));
    if (parsed.protocol !== 'https:' || !parsed.hostname.endsWith('.public.blob.vercel-storage.com')) {
      return res.status(400).json({ error: 'Only Vercel Blob URLs can be deleted.' });
    }
    const { del } = await import('@vercel/blob');
    await del(parsed.toString());
    return res.status(200).json({ ok: true, deleted: parsed.toString() });
  } catch (error) {
    return res.status(400).json({ error: error instanceof Error ? error.message : 'Blob deletion failed.' });
  }
};
