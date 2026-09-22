const ALLOWED_TYPES = ['video/mp4', 'video/quicktime', 'video/webm'];
const MAX_BYTES = 60 * 1024 * 1024;

module.exports = async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).send('Method Not Allowed');
  try {
    const body = typeof req.body === 'string' ? JSON.parse(req.body || '{}') : (req.body || {});
    const request = new Request('https://' + (req.headers.host || 'localhost') + (req.url || '/api/blob-upload'), {
      method: 'POST', headers: req.headers, body: JSON.stringify(body),
    });
    const { handleUpload } = await import('@vercel/blob/client');
    const response = await handleUpload({
      request,
      body,
      onBeforeGenerateToken: async () => ({
        allowedContentTypes: ALLOWED_TYPES,
        maximumSizeInBytes: MAX_BYTES,
        addRandomSuffix: true,
        tokenPayload: JSON.stringify({ purpose: 'quran-shorts-custom-video' }),
      }),
      onUploadCompleted: async () => {},
    });
    return res.status(response.status || 200).json(await response.json());
  } catch (error) {
    return res.status(400).json({ error: error instanceof Error ? error.message : 'Upload token generation failed.' });
  }
};
