#!/usr/bin/env node
// Uploads dashboard data files to R2 at data/<name>. Part of INF-R2-GITCUTOVER-1 (migrate the
// jobs-data-2026 dashboard OFF public git onto R2, served via the zjp-data-proxy Worker).
// Usage: node scripts/upload-dashboard-data-to-r2.js <file1> [file2 ...]  (names relative to .github/data/)
// Non-fatal on missing files (SKIP). Exits 1 only on a real R2 write error.
const fs = require('fs');
const path = require('path');
const { S3Client, PutObjectCommand } = require('@aws-sdk/client-s3');

const files = process.argv.slice(2);
if (!files.length) { console.log('upload-dashboard-data-to-r2: no files given'); process.exit(0); }

const client = new S3Client({
  region: 'auto',
  endpoint: process.env.R2_ENDPOINT,
  credentials: { accessKeyId: process.env.R2_ACCESS_KEY_ID, secretAccessKey: process.env.R2_SECRET_ACCESS_KEY },
});

(async () => {
  for (const f of files) {
    const local = path.join('.github/data', f);
    if (!fs.existsSync(local)) { console.log(`SKIP(missing):${f}`); continue; }
    await client.send(new PutObjectCommand({
      Bucket: process.env.R2_BUCKET_NAME,
      Key: 'data/' + f,
      Body: fs.readFileSync(local),
      ContentType: f.endsWith('.jsonl') ? 'application/x-ndjson' : 'application/json',
    }));
    console.log(`UPLOADED_R2:data/${f}`);
  }
})().catch(e => { console.error('upload-dashboard-data-to-r2 ERROR:', e.message); process.exit(1); });
