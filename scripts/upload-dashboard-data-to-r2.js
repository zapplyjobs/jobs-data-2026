#!/usr/bin/env node
// Dep-free R2 (S3-API) uploader — Node 22 built-ins only (crypto + fetch). No npm install needed,
// so it runs in any jobs-data-2026 workflow. Uploads dashboard data files to R2 at data/<name>.
// Usage: node upload-dashboard-data-to-r2-nodep.js <file1> [file2 ...]  (names relative to .github/data/)
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const files = process.argv.slice(2);
if (!files.length) { console.log('no files given'); process.exit(0); }
const AK = process.env.R2_ACCESS_KEY_ID, SK = process.env.R2_SECRET_ACCESS_KEY;
const EP = process.env.R2_ENDPOINT, BK = process.env.R2_BUCKET_NAME;
if (!AK || !SK || !EP || !BK) { console.error('missing R2 env vars'); process.exit(1); }

const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
const hmac = (k, d) => crypto.createHmac('sha256', k).update(d).digest();
const sigKey = (sec, date) => hmac(hmac(hmac(hmac('AWS4' + sec, date), 'auto'), 's3'), 'aws4_request');

async function putR2(key, body, ct) {
  const host = new URL(EP).host;
  const resource = `/${BK}/${key}`;
  const amzDate = new Date().toISOString().replace(/[:-]|\.\d{3}/g, '');
  const dateStamp = amzDate.slice(0, 8);
  const payloadHash = sha(body);
  const hdrs = { host, 'x-amz-content-sha256': payloadHash, 'x-amz-date': amzDate, 'content-type': ct };
  const canonHeaders = Object.keys(hdrs).sort().map(k => `${k}:${hdrs[k]}\n`).join('');
  const signedHeaders = Object.keys(hdrs).sort().join(';');
  const canonReq = `PUT\n${resource}\n\n${canonHeaders}\n${signedHeaders}\n${payloadHash}`;
  const scope = `${dateStamp}/auto/s3/aws4_request`;
  const strToSign = `AWS4-HMAC-SHA256\n${amzDate}\n${scope}\n${sha(canonReq)}`;
  const signature = crypto.createHmac('sha256', sigKey(SK, dateStamp)).update(strToSign).digest('hex');
  const auth = `AWS4-HMAC-SHA256 Credential=${AK}/${scope}, SignedHeaders=${signedHeaders}, Signature=${signature}`;
  const res = await fetch(`${EP}${resource}`, { method: 'PUT', headers: { ...hdrs, authorization: auth }, body });
  if (!res.ok) throw new Error(`PUT ${key} → ${res.status}: ${await res.text()}`);
}

(async () => {
  for (const f of files) {
    const local = path.join('.github/data', f);
    if (!fs.existsSync(local)) { console.log(`SKIP(missing):${f}`); continue; }
    const body = fs.readFileSync(local);
    const ct = f.endsWith('.jsonl') ? 'application/x-ndjson' : 'application/json';
    await putR2('data/' + f, body, ct);
    console.log(`UPLOADED_R2:data/${f}`);
  }
})().catch(e => { console.error('upload ERROR:', e.message); process.exit(1); });
