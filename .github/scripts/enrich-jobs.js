/**
* enrich-jobs.js
*
* Reads all_jobs.json, enriches new jobs (split batch: fast CPU-only + slow HTTP sources),
* appends results to enriched_jobs.json (JSONL).
*
* Enrichment extracts:
*   - required_skills[]        (from requirements/qualifications sections)
*   - nice_to_have_skills[]    (from preferred/bonus sections)
*   - sponsors_visa            (true | false | null — text-based, kept as fallback)
*   - possible_sponsor         (true | null — DOL LCA signal, only when sponsors_visa=null)
*   - visa_question_present    (true | false | null — from ATS application form)
*   - is_remote                (bool, from tags.locations includes 'remote')
*   - experience_level         (from tags.employment)
*   - is_simple_apply          (bool | null — DATA-8: GH only, question_count <= 13)
*   - question_count           (int | null — DATA-8: GH/Ashby/Lever)
*   - min_degree               ('bachelors'|'masters'|'phd'|'associates'|'none'|null — DATA-3)
*   - experience_level_from_desc ('entry_level'|'mid_level'|'senior'|null — DATA-4)
*   - has_description          (bool — whether a description was available during enrichment)
*   + denormalized display fields: title, company_name, job_city, job_state, url, posted_at
*
* visa_question_present detection (per ATS):
*   Greenhouse: GET /v1/boards/{slug}/jobs/{id}?questions=true → questions[].label
*   Ashby:      fetch apply_url page → window.__appData JSON → field.title
*   Lever:      fetch apply_url page → HTML-entity-encoded JSON → fields[].text
*/

'use strict';

const fs = require('fs');
const https = require('https');
const path = require('path');
const he = require('he');

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const ENRICHER_VERSION = 36;   // ENR-53: filter openai company-name FP in skills extraction
const SLOW_BATCH_SIZE = 200;   // GH, Ashby/Lever — HTTP calls per job (ENR-49: 120→200)
const FAST_BATCH_SIZE = 500;  // WD, SR, JSearch, Amazon, Netflix, EF — CPU only
const FAST_SOURCES = new Set(['workday', 'smartrecruiters', 'jsearch', 'amazon', 'netflix', 'eightfold']);
const DESC_FETCH_PER_RUN = 500; // DESC-MIGRATE-1: WD/SR descriptions fetched by enrichment (3s timeout per)
const DESC_FETCH_DELAY_MS = 300;
const DATA_DIR = path.join(process.cwd(), '.github', 'data');
const ALL_JOBS_PATH = path.join(DATA_DIR, 'all_jobs.json');
const ENRICHED_PATH = path.join(DATA_DIR, 'enriched_jobs.json');
const PROCESSED_PATH = path.join(DATA_DIR, 'processed_ids.json');
const DESCRIPTIONS_PATH = path.join(DATA_DIR, 'descriptions.jsonl');
const TAXONOMY_PATH = path.join(__dirname, 'enrich', 'skills-taxonomy.json');
const LCA_SPONSORS_PATH = path.join(DATA_DIR, 'lca-sponsors.json');

// ---------------------------------------------------------------------------
// Load taxonomy — flatten all categories into a single Set for O(1) lookup,
// preserving canonical casing from the JSON for output.
// Aliases are canonicalized so consumers see consistent skill names.
// ---------------------------------------------------------------------------
const SKILL_ALIASES = {
'react.js': 'React', 'reactjs': 'React',
'vue.js': 'Vue', 'vuejs': 'Vue',
'node.js': 'Node.js',
'postgres': 'PostgreSQL',
'k8s': 'Kubernetes',
'nlp': 'Natural Language Processing',
};
function loadTaxonomy() {
const raw = JSON.parse(fs.readFileSync(TAXONOMY_PATH, 'utf8'));
// Map lowercase → canonical term (aliases resolve to primary form)
const termMap = new Map();
for (const [category, terms] of Object.entries(raw)) {
if (category === '_meta') continue;
for (const term of terms) {
const canonical = SKILL_ALIASES[term.toLowerCase()] || term;
termMap.set(term.toLowerCase(), canonical);
}
}
return termMap;
}

// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// ENRICH-GAP-5: DOL LCA sponsor lookup
//
// Loads pre-built Set of normalized employer names from DOL OFLC certified
// H-1B/H-1B1/E-3 LCA filings. Used to set possible_sponsor=true for jobs
// where sponsors_visa=null (no text signal). Never emits false from absence.
//
// Normalization: lowercase, strip legal suffixes (inc/llc/corp/…), collapse
// punctuation/whitespace. Matching: exact OR prefix (handles subsidiaries).
// ---------------------------------------------------------------------------
function loadLcaSponsors() {
if (!fs.existsSync(LCA_SPONSORS_PATH)) {
console.log('[enrich-jobs] lca-sponsors.json not found — possible_sponsor will be null for all jobs');
return new Set();
}
const data = JSON.parse(fs.readFileSync(LCA_SPONSORS_PATH, 'utf8'));
const raw = data.employers || [];
// ENR-35: Normalize at load time so exact/prefix matching is consistent.
// 10% of LCA entries contain special chars (&, commas, hyphens) that break
// comparison against normalizeLcaName()'s output (which strips those chars).
const set = new Set(raw.map(normalizeLcaName).filter(Boolean));
console.log(`[enrich-jobs] LCA sponsors loaded: ${set.size} normalized employers from ${raw.length} raw (${data._meta?.source || 'unknown source'})`);
return set;
}

function normalizeLcaName(name) {
if (!name) return '';
let n = name.toLowerCase().trim();
n = n.replace(/\b(inc|llc|ltd|corp|co|lp|llp|plc|gmbh|ag|sa|nv|bv|pte|pvt|limited|incorporated|corporation|company|group|holdings?|technologies?|solutions?|services?|systems?)\b\.?/g, '');
n = n.replace(/[^a-z0-9\s]/g, ' ');
n = n.replace(/\s+/g, ' ').trim();
return n;
}

// LCA_COMPANY_ALIASES: job company_name → LCA database entry name.
// Companies whose common name doesn't normalize-match their LCA filing entity.
// Verified against lca-sponsors.json (66,579 employers, FY2025Q1–FY2026Q1).
const LCA_COMPANY_ALIASES = {
'SpaceX': 'Space Exploration Technologies Corp',
'Cursor': 'Anysphere Inc',
'SharkNinja': 'SharkNinja Operating LLC',
'HPE': 'Hewlett Packard Enterprise Company LP',
'HPE (University)': 'Hewlett Packard Enterprise Company LP',
'DeepMind': 'Google LLC',
'Intuitive': 'Intuitive Surgical Operations Inc',
'Cadence': 'Cadence Design Systems Inc',
'Cadence (University)': 'Cadence Design Systems Inc',
'FLIR Systems': 'Teledyne FLIR LLC',
'LLNL': 'Lawrence Livermore National Security LLC',
'Bosch Group': 'Robert Bosch LLC',
'Dell Technologies': 'Dell USA LP',
'Intrinsic Robotics': 'Intrinsic Innovation LLC',
'Veolia Environnement SA': 'Veolia North America LLC',
'The Hartford': 'Hartford Fire Insurance Company',
'GE Vernova': 'General Electric Company',
'GE Aerospace': 'General Electric Company',
'GE HEALTHCARE': 'General Electric Company',
'Merck & Co.': 'Merck Sharp & Dohme Corp',
'Warner Bros. Discovery': 'Warner Bros Entertainment Inc',
'Prudential Financial': 'The Prudential Insurance Company of America',
'Freddie Mac': 'Federal Home Loan Mortgage Corporation',
'Western Digital': 'Western Digital Technologies Inc',
// ENR-34: Amazon subsidiaries — normalizeLcaName creates spaces from dots ('amazon.com' → 'amazon com')
// but LCA data has 'amazoncom' (dots removed without space). Prefix match bridges the gap.
'Amazon.com Services LLC': 'amazoncom services llc',
'Amazon Development Center U.S., Inc.': 'amazon development center us inc',
'Amazon Web Services, Inc. - A97': 'amazon web services inc',
'Amazon.com LLC': 'amazon llc',
// ENR-55: Restore lost aliases (Together AI, F5) + add new (Annapurna Labs, NXP, HP Inc)
'Together AI': 'Together Computer',
'F5': 'F5 Networks Inc',
'Annapurna Labs (U.S.) Inc.': 'annapurna labs (us) inc',
'Annapurna Labs (U.S.) Inc. - D63': 'Amazon Web Services, Inc.',
'US63 NXP USA Inc.': 'NXP USA',
'HP Inc': 'HP Inc',
};

function isPossibleSponsor(companyName, lcaSet) {
if (!lcaSet.size) return null;
// Check alias map first — handles companies whose common name doesn't match LCA entity
const aliasName = LCA_COMPANY_ALIASES[companyName];
const nameToCheck = aliasName || companyName;
const n = normalizeLcaName(nameToCheck);
if (!n || n.length < 2) return null;
if (lcaSet.has(n)) return true;
// Prefix match: handles "Amazon Web Services" → matches "amazon" parent or subsidiaries
for (const entry of lcaSet) {
if (entry.startsWith(n + ' ') || n.startsWith(entry + ' ')) return true;
}
return null;
}

// Load per-source description sidecars → Map<id, description_text>
//
// Reads all files matching descriptions-*.jsonl in DATA_DIR.
// Handles both single-source files (descriptions-greenhouse.jsonl) and
// chunked files (descriptions-greenhouse-1.jsonl, descriptions-greenhouse-2.jsonl).
// Falls back to legacy descriptions.jsonl if per-source files are absent
// (handles transition period between old and new aggregator).
// ---------------------------------------------------------------------------
function loadDescriptionsMap() {
const map = new Map();

const files = fs.readdirSync(DATA_DIR)
.filter(f => /^descriptions-.*\.jsonl$/.test(f))
.map(f => path.join(DATA_DIR, f));

// Fallback: legacy single-file sidecar
if (files.length === 0 && fs.existsSync(DESCRIPTIONS_PATH)) {
files.push(DESCRIPTIONS_PATH);
}

for (const filePath of files) {
const lines = fs.readFileSync(filePath, 'utf8').trim().split('\n').filter(Boolean);
for (const line of lines) {
try {
const { id, description_text } = JSON.parse(line);
if (id) map.set(id, description_text || null);
} catch (_) { /* skip malformed */ }
}
}

return map;
}

// ---------------------------------------------------------------------------
// DESC-MIGRATE-1: Fetch WD/SR descriptions on-demand for enrichable jobs
// Reconstructs API URLs from job.url — no _raw fields needed.
// Only fetches for tech+US jobs missing from sidecar (targeted, no waste).
// ---------------------------------------------------------------------------
function buildWdDescUrl(jobUrl) {
// job.url: https://{tenant}.wd{N}.myworkdayjobs.com/{site}/job/{path}
// API:    https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{path}
const m = jobUrl.match(/^(https:\/\/([^.]+)\.wd\d+\.myworkdayjobs\.com)\/([^/]+)(\/.*)/);
if (!m) return null;
return `${m[1]}/wday/cxs/${m[2]}/${m[3]}${m[4]}`;
}

function buildSrDescUrl(jobId, companySlug) {
// id format: sr-{CompanySlug}-{numericId}
const numericId = jobId.split('-').slice(2).join('-');
return `https://api.smartrecruiters.com/v1/companies/${companySlug}/postings/${numericId}`;
}

// Fast GET for description fetches — shorter timeout, no redirect following
function quickGet(url) {
return new Promise((resolve) => {
const req = https.get(url, {
headers: { 'User-Agent': 'Mozilla/5.0 (compatible; job-board-bot/1.0)' }
}, (res) => {
if (res.statusCode !== 200) { resolve({ status: res.statusCode, body: '' }); return; }
let d = '';
res.on('data', c => d += c);
res.on('end', () => resolve({ status: res.statusCode, body: d }));
});
req.setTimeout(3000, () => { req.destroy(); resolve(null); });
req.on('error', () => resolve(null));
});
}

// Failure cache: skip URLs that returned 403/404 for 24h
const DESC_FAIL_CACHE_PATH = path.join(DATA_DIR, 'desc-fetch-failures.json');
function loadFailCache() {
if (!fs.existsSync(DESC_FAIL_CACHE_PATH)) return {};
try { return JSON.parse(fs.readFileSync(DESC_FAIL_CACHE_PATH, 'utf8')); } catch { return {}; }
}
function saveFailCache(cache) {
// Prune entries older than 24h
const cutoff = Date.now() - 24 * 60 * 60 * 1000;
const pruned = {};
for (const [id, ts] of Object.entries(cache)) {
if (ts > cutoff) pruned[id] = ts;
}
fs.writeFileSync(DESC_FAIL_CACHE_PATH, JSON.stringify(pruned), 'utf8');
}

// Determine which enriched chunk to write to for this entire run.
// Checked once at startup — never switches mid-run to avoid splitting a batch across files.
// A new chunk file is started when the current tail chunk exceeds CHUNK_LIMIT_BYTES.
// Chunks: descriptions-enriched-1.jsonl, -2.jsonl, -3.jsonl, ...
function resolveActiveChunk() {
const CHUNK_LIMIT_BYTES = 50 * 1024 * 1024; // 50 MB per chunk
let n = 1;
while (true) {
const p = path.join(DATA_DIR, `descriptions-enriched-${n}.jsonl`);
const size = fs.existsSync(p) ? fs.statSync(p).size : 0;
if (size < CHUNK_LIMIT_BYTES) return p; // this chunk has room
n++;
}
}

async function fetchMissingDescriptions(allJobs, descriptionsMap, activeChunkPath) {
const failCache = loadFailCache();
const failCacheSize = Object.keys(failCache).length;

// TAG-7: Fetch descriptions for ALL US WD/SR jobs (not just tech+US).
// General-tagged WD jobs need descriptions for classification — the tag engine's
// description fallback can reclassify them once descriptions are in the sidecar.
// Previous: tech+US only (DESC-MIGRATE-1). Expanded to ALL US for TAG-7.
const pending = allJobs.filter(j => {
if (j.source !== 'workday' && j.source !== 'smartrecruiters') return false;
if (descriptionsMap.has(j.id)) return false;
if (failCache[j.id]) return false; // skip known-failed URLs for 24h
const locs = j.tags?.locations || [];
return locs.includes('us');
});

if (pending.length === 0) {
console.log(`[enrich-jobs] DESC-MIGRATE-1: 0 WD/SR jobs need descriptions (${failCacheSize} in fail cache)`);
saveFailCache(failCache);
return 0;
}

const batch = pending.slice(0, DESC_FETCH_PER_RUN);
console.log(`[enrich-jobs] DESC-MIGRATE-1: ${pending.length} pending (${failCacheSize} skipped via fail cache), fetching ${batch.length}...`);

let fetched = 0;
const newEntries = [];
const startTime = Date.now();
const MAX_FETCH_TIME_MS = 3 * 60 * 1000; // 3 min max — leave budget for enrichment batch

for (const job of batch) {
if (Date.now() - startTime > MAX_FETCH_TIME_MS) {
console.log(`[enrich-jobs] DESC-MIGRATE-1: time limit reached after ${fetched} fetches`);
break;
}
let url, rawHtml;
if (job.source === 'workday') {
url = buildWdDescUrl(job.url);
if (!url) continue;
const result = await quickGet(url);
if (!result || result.status !== 200) { failCache[job.id] = Date.now(); continue; }
try {
const data = JSON.parse(result.body);
rawHtml = data?.jobPostingInfo?.jobDescription || null;
} catch (_) { failCache[job.id] = Date.now(); continue; }
} else {
// smartrecruiters
url = buildSrDescUrl(job.id, job.company_slug);
const result = await quickGet(url);
if (!result || result.status !== 200) { failCache[job.id] = Date.now(); continue; }
try {
const data = JSON.parse(result.body);
rawHtml = data?.jobAd?.sections?.jobDescription?.text || null;
} catch (_) { failCache[job.id] = Date.now(); continue; }
}

if (rawHtml) {
// Strip HTML inline (same logic as toPlainText but simpler — just strip tags + decode)
const text = he.decode(he.decode(rawHtml)).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
if (text.length > 20) {
descriptionsMap.set(job.id, text);
newEntries.push({ id: job.id, description_text: text });
fetched++;
} else {
failCache[job.id] = Date.now(); // too short to use
}
} else {
failCache[job.id] = Date.now(); // API returned 200 but no description field
}

await new Promise(r => setTimeout(r, DESC_FETCH_DELAY_MS));
}

// Append to enrichment-owned sidecar (activeChunkPath determined once at run start).
// Aggregator rewrites descriptions-workday.jsonl from scratch each run — appending
// there would lose entries. Using -enriched suffix avoids conflict.
// loadDescriptionsMap() auto-discovers all descriptions-*.jsonl files via glob,
// so new chunk numbers are transparent to the reader.
if (newEntries.length > 0) {
fs.appendFileSync(activeChunkPath,
newEntries.map(e => JSON.stringify(e)).join('\n') + '\n', 'utf8');
}

saveFailCache(failCache);
console.log(`[enrich-jobs] DESC-MIGRATE-1: fetched ${fetched} descriptions (${Object.keys(failCache).length} in fail cache, skip for 24h)`);
return fetched;
}

// ---------------------------------------------------------------------------
// HTML → plain text with structural section markers
// Strategy:
//   - <h1>–<h4>: always structural → emit ###SECTION:text###
//   - <strong>/<b> inside a block that contains ONLY the strong tag → structural
//   - All other <strong>/<b> → inline emphasis, stripped normally
// Sampling (5 GH + 5 Ashby, 2026-02-28): GH uses <strong> for section headers
// (Anduril, SpaceX, Lucid, Okta); <h2> seen only in Elastic. Ashby uses <h1>–<h3>
// depending on company. No single tag is universal, so both paths needed.
// ---------------------------------------------------------------------------
function toPlainText(html) {
if (!html) return '';
// Double-decode: &amp;nbsp; → &nbsp; → (space). Handles double-encoded HTML from ATS sources.
const decoded = he.decode(he.decode(html));

// Step 1: Replace <h1>–<h4> with structural markers before any other processing.
// Capture tag content, strip inner tags, emit ###SECTION:text###.
let marked = decoded.replace(/<h[1-4][^>]*>([\s\S]*?)<\/h[1-4]>/gi, (_, inner) => {
const text = inner.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
return text ? `\n###SECTION:${text}###\n` : '\n';
});

// Step 2: Replace block-level <p> and <div> that contain ONLY a <strong> or <b>
// (possibly with whitespace/&nbsp;) with a structural marker.
// Pattern: <p> or <div> whose entire content is <strong>text</strong> or <b>text</b>
marked = marked.replace(/<(p|div)[^>]*>\s*<(?:strong|b)[^>]*>([\s\S]*?)<\/(?:strong|b)>\s*<\/\1>/gi, (_, _tag, inner) => {
const text = inner.replace(/<[^>]+>/g, '').replace(/&nbsp;/gi, ' ').replace(/\s+/g, ' ').trim();
return text ? `\n###SECTION:${text}###\n` : '\n';
});

// Step 3: Replace remaining block-level tags with newline for section splitting
const withNewlines = marked.replace(/<\/(p|div|li|h[1-6]|br)>/gi, '\n');
// Strip remaining tags
const stripped = withNewlines.replace(/<[^>]+>/g, ' ');
// Normalize whitespace (but preserve newlines for section detection)
return stripped.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
}

// ---------------------------------------------------------------------------
// Section splitter
// Returns { required: string, preferred: string }
// Matches both ###SECTION:### markers (from HTML tags) and plain-text headers
// (fallback for JSearch/Lever plain-text descriptions).
// [:\s]? makes trailing colon/space optional — handles all-caps headers with no suffix.
// ---------------------------------------------------------------------------
const REQUIRED_HEADERS = [
/requirements?[:\s]?$/i,
/(?<!preferred\s)(?<!desired\s)qualifications?[:\s]?$/i,
/what you (need|bring|must have)[:\s]?$/i,
/what you need to succeed[:\s]?$/i,
/what we('?re| are) looking for[:\s]?$/i,
/education (and|&).{0,10}experience[:\s]?$/i,
/minimum qualifications?[:\s]?$/i,
/basic qualifications?[:\s]?$/i,
/required (skills?|qualifications?)[:\s]?$/i,
/must[ -]have[:\s]?$/i,
/you (will need|should have)[:\s]?$/i,
/skills? you.ll need[:\s]?/i,
/in practice this looks like[:\s]?$/i,
/you might thrive here if[:\s]?$/i,
/who you are[:\s]?$/i,
/what you.ll bring[:\s]?$/i,
/about you[:\s]?$/i,
/the ideal candidate[:\s]?$/i,
/^experience[:\s]?$/i,
/successful candidates?.{0,50}(will|should|must)/i,
];

const PREFERRED_HEADERS = [
/preferred (qualifications?|skills?|experience)/i,
/nice[ -]to[ -]haves?[:\s]?$/i,
/bonus (points?|if|qualifications?)?[:\s]?$/i,
/desired qualifications?/i,
/plus (if|points?)?[:\s]?$/i,
/it'?s? (a )?(bonus|plus|nice)[:\s]?$/i,
/while not required/i,
/added (plus|bonus)/i,
];

function splitSections(text) {
const lines = text.split('\n');
let requiredStart = -1;
let preferredStart = -1;

// Collect all section boundaries so we can cap extraction correctly
const allBoundaries = []; // { idx, type }

for (let i = 0; i < lines.length; i++) {
// ###SECTION:text### markers (from <h1>–<h4> and block-level <strong>)
// Extract the section label and match against header patterns
const sectionMatch = lines[i].match(/^###SECTION:(.+?)###$/);
const line = sectionMatch ? sectionMatch[1].trim() : lines[i];

if (REQUIRED_HEADERS.some(r => r.test(line))) {
allBoundaries.push({ idx: i, type: 'required' });
if (requiredStart === -1) requiredStart = i;
} else if (PREFERRED_HEADERS.some(r => r.test(line))) {
allBoundaries.push({ idx: i, type: 'preferred' });
if (preferredStart === -1) preferredStart = i;
}
}

// Find the line where a section ends: start of next section, or start + 40, whichever is earlier
const extractSection = (start) => {
if (start === -1) return '';
const nextBoundary = allBoundaries.find(b => b.idx > start);
const end = nextBoundary ? Math.min(nextBoundary.idx, start + 80) : start + 80;
return lines.slice(start, end).join(' ');
};

return {
required: extractSection(requiredStart),
preferred: extractSection(preferredStart),
};
}

// ---------------------------------------------------------------------------
// Taxonomy matcher
// Returns deduplicated array of canonical skill names found in text.
// Uses word-boundary aware matching to avoid "r" matching "requirements".
//
// Ambiguous short terms (go, r, c, rest, etc.) require explicit tech context
// nearby to avoid false positives like "go-to-market" or "the rest of".
// ---------------------------------------------------------------------------

// Terms that are too ambiguous on their own — require a tech context signal
// within the same sentence/bullet to count as a match.
const AMBIGUOUS_TERMS = new Set(['go', 'r', 'c', 'rest', 'restful', 'assembly', 'lean', 'chef', 'classification', 'move']);

// ENR-53: Terms that match company names in boilerplate text. Filter these out
// when the job's company_name contains the term — the match is almost always
// boilerplate ("OpenAI's mission is to..."), not a skill requirement.
const COMPANY_NAME_TERMS = new Set(['openai']);

const TECH_CONTEXT_SIGNALS = [
/\b(programming|language|developer|engineer|code|software|written in|experience with|proficien|framework|backend|api)\b/i,
];

function hasTechContext(text, matchIdx) {
// Check within 120 chars before/after the match for a tech context signal
const window = text.slice(Math.max(0, matchIdx - 120), matchIdx + 120);
return TECH_CONTEXT_SIGNALS.some(re => re.test(window));
}

function matchSkills(text, termMap) {
if (!text) return [];
const lower = text.toLowerCase();
const found = new Set();

for (const [termLower, termCanonical] of termMap) {
let searchFrom = 0;
let idx;
// Check all occurrences (a term may appear multiple times)
while ((idx = lower.indexOf(termLower, searchFrom)) !== -1) {
searchFrom = idx + 1;

const before = idx === 0 ? ' ' : lower[idx - 1];
const after = idx + termLower.length >= lower.length ? ' ' : lower[idx + termLower.length];
const wordBefore = /[a-z0-9]/.test(before);
const wordAfter = /[a-z0-9]/.test(after);

if (!wordBefore && !wordAfter) {
// For ambiguous short terms, require tech context nearby
if (AMBIGUOUS_TERMS.has(termLower) && !hasTechContext(lower, idx)) {
continue;
}
found.add(termCanonical);
break; // found at least once at word boundary — no need to check more occurrences
}
}
}

return Array.from(found).sort();
}

// ---------------------------------------------------------------------------
// Visa sponsorship detector
// Returns true | false | null
// ---------------------------------------------------------------------------

// Patterns that appear in EEO boilerplate — strip these paragraphs first
const EEO_BOILERPLATE = [
/equal opportunity employer/i,
/without regard to race/i,
/eeo statement/i,
/disability.{0,40}veteran/i,
/reasonable accommodation/i,
];

// Negative signals → false (company explicitly will NOT sponsor)
const VISA_NEGATIVE = [
/\bno\b.{0,30}\bvisa sponsorship\b/i,
/will not sponsor/i,
/cannot sponsor/i,
/unable to sponsor/i,
/does not (offer|provide) (visa )?sponsorship/i,
/sponsorship (is )?not available/i,
/must be (authorized|eligible) to work.{0,60}without (sponsorship|authorization)/i,
/authorized to work in the u\.?s\.?(a\.?)? without/i,
/u\.?s\.? citizen(ship)? (or|and) (permanent resident|green card)/i,
/legally authorized to work.{0,40}united states/i,
/work authorization.{0,40}required/i,
/must be authorized to work in the (u\.?s\.?|united states)/i,
/applicant must be.{0,30}(u\.?s\.? citizen|permanent resident)/i,
/must be.{0,20}(citizen|permanent resident).{0,30}united states/i,
];

// Positive signals → true
const VISA_POSITIVE = [
/will (provide|offer|consider) (visa )?sponsorship/i,
/visa sponsorship (is )?available/i,
/sponsorship available\.?$/im,          // "Sponsorship available." (Ashby bullet-list benefit format)
/^[-•]\s*visa sponsorship\s*$/im,       // "- Visa Sponsorship" (Ashby benefit line, standalone)
/h[\s-]?1[\s-]?b sponsorship/i,
/open to (visa )?sponsorship/i,
/able to sponsor/i,
/sponsorship (for|of) (work )?visa/i,
/we (do )?sponsor/i,
];

function detectVisa(text) {
if (!text) return null;

// Strip EEO boilerplate paragraphs — split into paragraphs, remove boilerplate ones
const paragraphs = text.split(/\n{2,}/);
const filtered = paragraphs
.filter(p => !EEO_BOILERPLATE.some(re => re.test(p)))
.join('\n\n');

// Scan bottom 40% of filtered text (sponsorship language almost always appears at end)
const scanStart = Math.floor(filtered.length * 0.6);
const bottomText = filtered.slice(scanStart);
const fullText = filtered; // also scan full text for explicit signals

for (const re of VISA_NEGATIVE) {
if (re.test(bottomText) || re.test(fullText)) return false;
}

for (const re of VISA_POSITIVE) {
if (re.test(bottomText) || re.test(fullText)) return true;
}

return null;
}

// ---------------------------------------------------------------------------
// ATS application form visa detection
// Returns: true (question present) | false (not present) | null (fetch failed / source unsupported)
// ---------------------------------------------------------------------------

const GH_VISA_RE = /sponsor|visa/i;
const ASHBY_VISA_RE = /sponsor/i;
const LEVER_VISA_RE = /sponsor/i;
const FETCH_TIMEOUT_MS = 8000;

function httpsGet(url) {
return new Promise((resolve) => {
const req = https.get(url, { headers: { 'User-Agent': 'Mozilla/5.0' } }, (res) => {
// Follow redirects (max 2)
if ((res.statusCode === 301 || res.statusCode === 302) && res.headers.location) {
resolve(httpsGet(res.headers.location));
return;
}
let d = '';
res.on('data', c => d += c);
res.on('end', () => resolve({ status: res.statusCode, body: d }));
});
req.setTimeout(FETCH_TIMEOUT_MS, () => { req.destroy(); resolve(null); });
req.on('error', () => resolve(null));
});
}

// DATA-8: Simple apply threshold — forms with <= this many fields are considered "simple"
// GH embeds standard fields (name/email/phone/resume/location) as questions, so minimum is ~7.
// Bottom quartile of GH distribution is ~13. Threshold calibrated to GH observed data (S137).
const SIMPLE_APPLY_THRESHOLD = 13;

// fetchApplicationVisaStatus returns { visaPresent, questionCount }
// visaPresent: true | false | null
// questionCount: integer (GH/Ashby/Lever) | null (JSearch/Workday/Amazon — no form access)
async function fetchApplicationVisaStatus(job) {
try {
if (job.source === 'greenhouse') {
// Parse slug + numeric ID from job.id format: "greenhouse-{slug}-{numeric_id}"
const m = job.id.match(/^greenhouse-(.+)-(\d+)$/);
if (!m) return { visaPresent: null, questionCount: null };
const [, slug, jobId] = m;
const url = `https://boards-api.greenhouse.io/v1/boards/${slug}/jobs/${jobId}?questions=true`;
const result = await httpsGet(url);
if (!result || result.status !== 200) return { visaPresent: null, questionCount: null };
const data = JSON.parse(result.body);
const questions = data.questions || [];
return {
visaPresent: questions.some(q => GH_VISA_RE.test(q.label || '')) ? true : false,
questionCount: questions.length,
};
}

if (job.source === 'ashby') {
const applyUrl = job.apply_url;
if (!applyUrl) return { visaPresent: null, questionCount: null };
const result = await httpsGet(applyUrl);
if (!result || result.status !== 200) return { visaPresent: null, questionCount: null };
// window.__appData = {...}; — extract JSON, search field titles for visa/sponsor
const m = result.body.match(/window\.__appData\s*=\s*(\{[\s\S]*?\});\s*\n/);
if (!m) {
console.log(`[enrich] Ashby window.__appData not found for ${job.id} — visa check skipped`);
return { visaPresent: null, questionCount: null };
}
const appData = JSON.parse(m[1]);
const str = JSON.stringify(appData);
// applicationForm.fieldEntries = application fields only (excludes surveyForms — EEO/demographics)
const fieldEntries = appData.posting?.applicationForm?.fieldEntries;
const questionCount = Array.isArray(fieldEntries) ? fieldEntries.length : null;
return { visaPresent: ASHBY_VISA_RE.test(str) ? true : false, questionCount };
}

if (job.source === 'lever') {
const applyUrl = job.apply_url;
if (!applyUrl) return { visaPresent: null, questionCount: null };
const result = await httpsGet(applyUrl);
if (!result || result.status !== 200) return { visaPresent: null, questionCount: null };
// Visa question is HTML-entity-encoded JSON embedded in page
const decoded = he.decode(result.body);
// fields[] = custom application questions only (standard name/email/resume handled separately by Lever UI)
// Bracket-depth counter required — greedy regex misses nested closing bracket
let questionCount = null;
const fieldsIdx = decoded.indexOf('"fields":[');
if (fieldsIdx >= 0) {
let depth = 0, end = null;
const snippet = decoded.slice(fieldsIdx + '"fields":'.length);
for (let i = 0; i < snippet.length; i++) {
if (snippet[i] === '[') depth++;
else if (snippet[i] === ']') { depth--; if (depth === 0) { end = i + 1; break; } }
}
if (end) {
try {
const fields = JSON.parse(snippet.slice(0, end));
questionCount = fields.length;
} catch (_) {}
}
}
return { visaPresent: LEVER_VISA_RE.test(decoded) ? true : false, questionCount };
}

return { visaPresent: null, questionCount: null }; // JSearch or other sources — no application page
} catch (_) {
return { visaPresent: null, questionCount: null };
}
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
function loadAllJobs() {
if (!fs.existsSync(ALL_JOBS_PATH)) {
console.log('all_jobs.json not found — nothing to enrich');
process.exit(0);
}
const lines = fs.readFileSync(ALL_JOBS_PATH, 'utf8').trim().split('\n');
return lines.filter(l => l.trim()).map(l => {
try { return JSON.parse(l); }
catch (_) { console.warn(`[enrich-jobs] skipped malformed line: ${l.slice(0, 60)}`); return null; }
}).filter(Boolean);
}

function loadProcessedIds() {
if (!fs.existsSync(PROCESSED_PATH)) return new Set();
try {
const raw = JSON.parse(fs.readFileSync(PROCESSED_PATH, 'utf8'));
// Support both legacy flat array and current map format
if (Array.isArray(raw)) return new Set(raw);
if (raw && typeof raw === 'object') return new Set(Object.keys(raw));
return new Set();
} catch (_) {
return new Set();
}
}

function loadProcessedMap() {
if (!fs.existsSync(PROCESSED_PATH)) return {};
try {
const raw = JSON.parse(fs.readFileSync(PROCESSED_PATH, 'utf8'));
// Migrate legacy flat array to map format on first read
if (Array.isArray(raw)) {
const map = {};
for (const id of raw) map[id] = { status: 'enriched', processed_at: null };
return map;
}
if (raw && typeof raw === 'object') return raw;
return {};
} catch (_) {
return {};
}
}

function loadEnrichedIds() {
// RE-ENRICH-1 fix: only load "skipped" (non-enrichable) IDs from processed_ids.json.
// "enriched" IDs must go through the version filter below — otherwise stale v2/v3
// records are pre-loaded as "done" and never re-enter the queue.
const ids = new Set();
if (fs.existsSync(PROCESSED_PATH)) {
try {
const raw = JSON.parse(fs.readFileSync(PROCESSED_PATH, 'utf8'));
if (Array.isArray(raw)) {
// Legacy format — treat all as done (no status info to distinguish)
for (const id of raw) ids.add(id);
} else if (raw && typeof raw === 'object') {
for (const [id, val] of Object.entries(raw)) {
if (val && val.status === 'skipped') ids.add(id);
// "enriched" status entries intentionally NOT added — version filter below decides
}
}
} catch (_) {}
}
if (!fs.existsSync(ENRICHED_PATH)) return ids;
const lines = fs.readFileSync(ENRICHED_PATH, 'utf8').trim().split('\n').filter(Boolean);
for (const line of lines) {
try {
const obj = JSON.parse(line);
// RE-ENRICH-1: skip stale versions so they re-enter the pending queue
if (obj.id && (obj.enricher_version || 0) >= ENRICHER_VERSION) {
        // ENR-37: Don't treat as "done" if enrichment produced zero skills AND no summary.
        // Race condition: enricher processed the job before description sidecar was updated.
        // These jobs should retry on the next run when descriptions may be available.
        // ENR-47: Use has_description instead of summary_line for the "got nothing" check.
        const hasResults = (obj.required_skills?.length > 0) || obj.has_description;
        if (hasResults) ids.add(obj.id);
      }
} catch (_) {}
}
return ids;
}

// ---------------------------------------------------------------------------
// DATA-7: Summary line extraction
// Returns the first non-boilerplate sentence from plain text description.
// Boilerplate openers (company mission, "at X we..." intros) are skipped.
// Falls back to first sentence of full text if no non-boilerplate sentence found.
// ---------------------------------------------------------------------------
// Boilerplate openers: company-about sentences, NOT role description sentences.
// Deliberately excludes "we are looking for" / "we're hiring" — those describe the role.
// Targets: "At [Company]...", "About us", "Our mission", "Founded in", company overview intros.
const BOILERPLATE_OPENERS = [
/^at [a-z]/i,                                      // "At Acme, we..." — company intro
/^(about us|about the company|company overview)/i, // section headers that leak in
/^our (mission|vision|company|culture|values)/i,   // mission/culture openers
/^(founded in|incorporated in)/i,                  // founding year openers
/^(we are a |we're a )/i,                          // "We are a fast-growing..." — company description
/^join (us|our team|the team)/i,                   // "Join us at..."
/\bwith \d+\+?\s*years of experience\b/i,          // "[Company], with 25+ years of experience..." — company history
];

// ---------------------------------------------------------------------------
// DATA-3: Education requirement extraction
// Returns: 'bachelors' | 'masters' | 'phd' | 'none' | null
//   null  = no education language found in description
//   'none' = explicitly states no degree required / equivalent experience accepted
//
// Operates on the required section (or full text as fallback), same as skills.
// Sampling (GH n=6542, Ashby n=1419, Lever n=1060):
//   Degree mentions: GH=19%, Ashby=10%, Lever=2%
//   "equivalent experience": GH/Ashby common, Lever rare
//   No-degree explicit: ~5% of GH pool
// ---------------------------------------------------------------------------
const DEGREE_PHD = /\b(ph\.?d\.?|doctoral|doctorate)\b/i;
const DEGREE_MASTERS = /\b(master'?s?)\s*(degree|of science|of arts|of engineering|in\s+\w|or higher|preferred|required|or phd|or doctoral|\/bs|\/bachelor)/i;
const DEGREE_MASTERS_ABBREV = /\bm\.?s\.?(\s|,|$)/i;
const DEGREE_MBA = /\bmba\b/i;
const DEGREE_BACHELORS = /\b(bachelor'?s?)\s*(degree|of science|of arts|of engineering|in\s+\w|or higher|preferred|required|or master|\/ms|\/master)/i;
const DEGREE_BACHELORS_ABBREV = /\bb\.?s\.?(\s|,|$)|\bb\.?e\.?(\s|,|$)|\bba\s*(degree|$)/i;
const DEGREE_BACHELORS_SHORT = /\b(bachelor'?s?|bs|ba)\s*[\+\/]\s*\d/i;
const DEGREE_MS_BS = /\bms\s*\/\s*bs\b|\bbs\s*\/\s*ms\b/i;
const DEGREE_ASSOCIATE = /\b(associate'?s?)\s*(degree|in\s+\w)/i;
const DEGREE_NONE = /\b(no (degree|college required)|equivalent experience|without (a )?degree|degree not required|equivalent combination|in lieu of degree|high school diploma|hs diploma|ged\b)/i;
const DEGREE_STANDALONE = /\bdegree\s+(required|preferred|in\s+\w|or\s+(higher|equivalent))\b/i;

function extractMinDegree(text) {
if (!text) return null;
// Detect all degree levels present, then return the minimum requirement.
// "Bachelor's or Master's or PhD" → bachelors (lowest mentioned = minimum).
const hasBachelors = DEGREE_BACHELORS.test(text) || DEGREE_BACHELORS_ABBREV.test(text) || DEGREE_BACHELORS_SHORT.test(text) || DEGREE_MS_BS.test(text);
const hasMasters = DEGREE_MASTERS.test(text) || DEGREE_MASTERS_ABBREV.test(text) || DEGREE_MBA.test(text) || DEGREE_MS_BS.test(text);
const hasPhd = DEGREE_PHD.test(text);
const hasAssociate = DEGREE_ASSOCIATE.test(text);
const hasNone = DEGREE_NONE.test(text);
const hasStandalone = DEGREE_STANDALONE.test(text);
if (hasAssociate) return 'associates';
if (hasBachelors) return 'bachelors';
if (hasMasters) return 'masters';
if (hasPhd) return 'phd';
if (hasNone) return 'none';
if (hasStandalone) return 'bachelors'; // "degree required" → at least bachelor's
return null;
}

// ---------------------------------------------------------------------------
// DATA-4: Experience level extraction from description text
// Returns: 'entry_level' | 'mid_level' | 'senior' | null
//   null = no year-range language found
//
// Year ranges map to levels:
//   0–2 years  → entry_level
//   3–5 years  → mid_level
//   6+ years   → senior
// When a range spans levels (e.g. "2-4 years"), use the lower bound.
//
// Sampling: GH=31%, Ashby=26%, Lever=2% have explicit year patterns.
// Patterns seen: "N+ years of experience", "N-M years of experience",
//   "N years experience", "N to M years of experience"
// ---------------------------------------------------------------------------
const EXP_YEAR_RE = /(\d+)\+?\s*(?:[-–to]+\s*\d+\s*)?years?\s*(?:of\s*)?(?:relevant\s*|related\s*|professional\s*|work\s*)?(?:experience|exp\b)/i;

function extractExperienceLevel(text) {
if (!text) return null;
// Strip boilerplate sentences before scanning to avoid false positives from
// company-history language ("OpenTable, with 25+ years of experience...").
const filteredText = text.split(/(?<=[.!?])\s+/)
.filter(s => !BOILERPLATE_OPENERS.some(re => re.test(s.trim())))
.join(' ');
const m = EXP_YEAR_RE.exec(filteredText);
if (!m) return null;
const years = parseInt(m[1], 10); // use lower bound of range
if (years <= 2) return 'entry_level';
if (years <= 5) return 'mid_level';
return 'senior';
}

const TECH_DOMAINS = new Set(['software', 'data_science', 'hardware', 'ai']);

function isEnrichable(job, descriptionsMap) {
const domains = job.tags?.domains || [];
const locations = job.tags?.locations || [];
if (!domains.some(d => TECH_DOMAINS.has(d))) return false;
if (!locations.includes('us')) return false;
// ENRICH-OBS-2: Workday and SmartRecruiters jobs are only enrichable if a description is available.
// Without a description, enrichJob() produces null for skills/summary/visa — permanently
// blocking the slot and masking these fields as "enriched" when they're actually empty.
// These sources fetch descriptions asynchronously in the aggregator (Step 1b/1c).
if (job.source === 'workday' || job.source === 'smartrecruiters') {
return !!descriptionsMap.get(job.id);
}
return true;
}

async function enrichJob(job, termMap, descriptionsMap, lcaSet) {

// ENR-32: Fall back to job.description for sources that embed descriptions in the
// job data itself (JSearch, Greenhouse, Ashby, Lever) rather than requiring sidecar.
// WD/SR descriptions are fetched asynchronously and stored in descriptions-*.jsonl.
const rawDescription = descriptionsMap.get(job.id) || job.description || null;
const plainText = toPlainText(rawDescription || '');
const { required, preferred } = splitSections(plainText);

if (!required) {
console.log(`[enrich] no section found for ${job.id} — using full text`);
}
const text = required || plainText;
let requiredSkills = matchSkills(text, termMap);
let niceToHaveSkills = matchSkills(preferred, termMap).filter(
s => !requiredSkills.includes(s)
);

// ENRICH-QUALITY-1: If required section yielded zero skills but preferred has them,
// promote preferred skills to required. Common pattern: SpaceX, Palantir, Hermeus put
// degree under "Basic Qualifications" and all tech skills under "Preferred Skills."
if (requiredSkills.length === 0 && niceToHaveSkills.length > 0) {
requiredSkills = niceToHaveSkills;
niceToHaveSkills = [];
}

// TAXONOMY-AUDIT-1: Full-text fallback — when both required section and preferred
// section yielded zero skills but a section WAS found (non-empty required), the
// section may contain only degree/experience text while tech skills are elsewhere
// in the description. Fall back to full text as last resort.
if (requiredSkills.length === 0 && required && plainText.length > required.length) {
requiredSkills = matchSkills(plainText, termMap);
}

// ENR-53: Remove skills that match company-name boilerplate. e.g., "openai"
// appears in OpenAI job descriptions as company boilerplate, not as a skill.
// Only filter when company_name contains the term (keeps legitimate matches
// at non-OpenAI companies like "looking for OpenAI API experience").
const companyLower = (job.company_name || '').toLowerCase();
if (companyLower) {
for (const term of COMPANY_NAME_TERMS) {
if (companyLower.includes(term) && requiredSkills.includes(term)) {
requiredSkills = requiredSkills.filter(s => s.toLowerCase() !== term);
}
if (companyLower.includes(term) && niceToHaveSkills.includes(term)) {
niceToHaveSkills = niceToHaveSkills.filter(s => s.toLowerCase() !== term);
}
}
}

const sponsorsVisa = detectVisa(plainText);
// ENRICH-GAP-5: weak positive from DOL LCA filings — only set when sponsors_visa=null
const possibleSponsor = sponsorsVisa === null ? isPossibleSponsor(job.company_name, lcaSet) : null;
const { visaPresent: visaQuestionPresent, questionCount } = await fetchApplicationVisaStatus(job);
const isRemote = (job.tags?.locations || []).includes('remote');
const experienceLevel = job.tags?.employment || null;

// DATA-8: simple apply detection — GH only (question count exact); Ashby/Lever schema unverified
const isSimpleApply = questionCount !== null ? questionCount <= SIMPLE_APPLY_THRESHOLD : null;

// DATA-3: education requirement — extracted from required section (fallback: full text)
const minDegree = extractMinDegree(text);
// DATA-4: experience level from description — extracted from required section (fallback: full text)
const experienceLevelFromDesc = extractExperienceLevel(text);

return {
id: job.id,
source: job.source || null,
enricher_version: ENRICHER_VERSION,
required_skills: requiredSkills,
nice_to_have_skills: niceToHaveSkills,
sponsors_visa: sponsorsVisa,
possible_sponsor: possibleSponsor,
visa_question_present: visaQuestionPresent,
is_remote: isRemote,
experience_level: experienceLevel,
has_description: !!rawDescription,
// DATA-3: education requirement extracted from description text
min_degree: minDegree,
// DATA-4: experience level extracted from description text (distinct from tags.employment)
experience_level_from_desc: experienceLevelFromDesc,
// DATA-8: simple apply signal (GH: exact; Ashby/Lever: null pending schema verification)
is_simple_apply: isSimpleApply,
question_count: questionCount,
enriched_at: new Date().toISOString(),
// Denormalized display fields
title: job.title || null,
company_name: job.company_name || null,
job_city: job.job_city || null,
job_state: job.job_state || null,
url: job.url || null,
posted_at: job.posted_at || null,
};
}

async function main() {
console.log('[enrich-jobs] Starting enrichment run');

const termMap = loadTaxonomy();
console.log(`[enrich-jobs] Taxonomy loaded: ${termMap.size} terms`);

const lcaSet = loadLcaSponsors();

const descriptionsMap = loadDescriptionsMap();
console.log(`[enrich-jobs] Descriptions loaded: ${descriptionsMap.size} entries`);

const allJobs = loadAllJobs();
console.log(`[enrich-jobs] Total jobs in pool: ${allJobs.length}`);

// DESC-MIGRATE-1: Fetch WD/SR descriptions for tech+US jobs missing from sidecar.
// Active chunk is determined once at run start (not per-append) so a single workflow
// never splits a batch across two files mid-run.
const activeChunkPath = resolveActiveChunk();
console.log(`[enrich-jobs] Active enriched chunk: ${path.basename(activeChunkPath)}`);
await fetchMissingDescriptions(allJobs, descriptionsMap, activeChunkPath);

const enrichedIds = loadEnrichedIds();
console.log(`[enrich-jobs] Already enriched: ${enrichedIds.size}`);

const pending = allJobs.filter(j => !enrichedIds.has(j.id));
console.log(`[enrich-jobs] Pending enrichment: ${pending.length}`);

// PIPELINE-2: Bulk-mark non-enrichable jobs as processed so they exit the queue permanently.
// Previously these were marked one-at-a-time inside each batch, wasting ~83% of batch capacity
// on jobs that would be skipped. Now we mark them all upfront and only batch enrichable jobs.
const processedMap = loadProcessedMap();
const now = new Date().toISOString();
let bulkMarked = 0;
let descWaiting = 0;
for (const job of pending) {
if (!isEnrichable(job, descriptionsMap) && !processedMap[job.id]) {
const domains = job.tags?.domains || [];
const locations = job.tags?.locations || [];
// ENRICH-OBS-2: WD/SR US jobs with no description yet are NOT permanently skipped.
// They stay unprocessed so each run retries them as description sidecars grow.
// TAG-7: expanded from tech+US to ALL US (descriptions needed for classification).
if ((job.source === 'workday' || job.source === 'smartrecruiters') && locations.includes('us')) {
descWaiting++;
continue;
}
const reason = !domains.some(d => TECH_DOMAINS.has(d)) ? 'non-tech' : 'non-us';
processedMap[job.id] = { status: 'skipped', reason, processed_at: now };
bulkMarked++;
}
}
if (bulkMarked > 0) {
console.log(`[enrich-jobs] Bulk-marked ${bulkMarked} non-enrichable jobs as processed (non-tech or non-US)`);
}
if (descWaiting > 0) {
console.log(`[enrich-jobs] WD/SR jobs waiting for description: ${descWaiting} (will retry each run)`);
}

const enrichablePending = pending.filter(j => isEnrichable(j, descriptionsMap));
// ENR-49: Process newest jobs first — posted_at descending.
// Prevents new jobs from waiting behind stale re-enrichment records.
enrichablePending.sort((a, b) => (b.posted_at || '').localeCompare(a.posted_at || ''));
// ENR-41: Count stale-version jobs awaiting re-enrichment
const pendingIds = new Set(pending.map(j => j.id));
let reenrichmentPending = 0;
if (fs.existsSync(ENRICHED_PATH)) {
  const enrichedLines = fs.readFileSync(ENRICHED_PATH, 'utf8').trim().split('\n');
  const seen = new Set();
  for (let i = enrichedLines.length - 1; i >= 0; i--) {
    try {
      const obj = JSON.parse(enrichedLines[i]);
      if (seen.has(obj.id)) continue; // dedup: last wins
      seen.add(obj.id);
      if (pendingIds.has(obj.id) && (obj.enricher_version || 0) < ENRICHER_VERSION) {
        reenrichmentPending++;
      }
    } catch (_) {}
  }
}
console.log(`[enrich-jobs] Enrichable pending: ${enrichablePending.length} (re-enrichment: ${reenrichmentPending})`);

// ENRICH-THROUGHPUT-1: Split batch by source type. Fast sources (CPU-only, no HTTP)
// can process 500/run. Slow sources (GH/Ashby/Lever need HTTP per job) stay at 40.
const fastPending = enrichablePending.filter(j => FAST_SOURCES.has(j.source));
const slowPending = enrichablePending.filter(j => !FAST_SOURCES.has(j.source));
const fastBatch = fastPending.slice(0, FAST_BATCH_SIZE);
const slowBatch = slowPending.slice(0, SLOW_BATCH_SIZE);
const batch = [...fastBatch, ...slowBatch];
console.log(`[enrich-jobs] Processing batch: ${fastBatch.length} fast + ${slowBatch.length} slow = ${batch.length} total`);

if (batch.length === 0) {
// Still need to persist the bulk-marked non-enrichable IDs and prune expired ones
const liveIds = new Set(allJobs.map(j => j.id));
for (const id of Object.keys(processedMap)) {
if (!liveIds.has(id)) delete processedMap[id];
}
fs.writeFileSync(PROCESSED_PATH, JSON.stringify(processedMap), 'utf8');
console.log('[enrich-jobs] Nothing to enrich. Exiting.');
return;
}

const enriched = await Promise.all(batch.map(job => enrichJob(job, termMap, descriptionsMap, lcaSet)));
// All batch jobs are enrichable (pre-filtered) — no skips expected here
const results = enriched.filter(r => r && !r.skipped);
console.log(`[enrich-jobs] Enriched and appended ${results.length} jobs`);

// Append new enriched results
if (results.length > 0) {
const newLines = results.map(r => JSON.stringify(r)).join('\n') + '\n';
fs.appendFileSync(ENRICHED_PATH, newLines, 'utf8');
}

// Mark enriched batch IDs as processed
const liveIds = new Set(allJobs.map(j => j.id));

for (const job of batch) {
processedMap[job.id] = { status: 'enriched', processed_at: now };
}

// Prune: remove IDs no longer in the live pool (aged out of 14-day window)
for (const id of Object.keys(processedMap)) {
if (!liveIds.has(id)) delete processedMap[id];
}
fs.writeFileSync(PROCESSED_PATH, JSON.stringify(processedMap), 'utf8');
console.log(`[enrich-jobs] processed_ids.json: ${Object.keys(processedMap).length} total (pruned to live pool)`);

// Prune enriched_jobs.json: remove expired IDs + dedup (keep latest per ID).
// RE-ENRICH-1: re-enriched jobs produce a second record — dedup keeps the newer one.
if (fs.existsSync(ENRICHED_PATH)) {
const allEnrichedLines = fs.readFileSync(ENRICHED_PATH, 'utf8').trim().split('\n').filter(Boolean);
// Dedup: last occurrence wins (new records appended at end → latest is last)
const seenIds = new Map(); // id → line index
const prunedLines = [];
for (let i = 0; i < allEnrichedLines.length; i++) {
try {
const obj = JSON.parse(allEnrichedLines[i]);
if (!liveIds.has(obj.id)) continue; // expired
if (seenIds.has(obj.id)) {
// Replace earlier occurrence with this newer one
prunedLines[seenIds.get(obj.id)] = null;
}
seenIds.set(obj.id, prunedLines.length);
prunedLines.push(allEnrichedLines[i]);
} catch (_) {
// drop malformed lines
}
}
const finalLines = prunedLines.filter(Boolean);

if (finalLines.length < allEnrichedLines.length) {
const removed = allEnrichedLines.length - finalLines.length;
fs.writeFileSync(ENRICHED_PATH, finalLines.join('\n') + '\n', 'utf8');
console.log(`[enrich-jobs] Pruned ${removed} records (expired + deduped)`);
}

// Quick stats
const withRequired = results.filter(r => r.required_skills.length > 0).length;
const withVisa = results.filter(r => r.sponsors_visa !== null).length;
const withVisaForm = results.filter(r => r.visa_question_present !== null).length;
console.log(`[enrich-jobs] Stats: ${withRequired}/${results.length} had required skills, ${withVisa}/${results.length} had visa text signal, ${withVisaForm}/${results.length} had visa form signal`);
console.log(`[enrich-jobs] Total enriched (post-prune): ${finalLines.length}`);

// ENRICH-OBS-1: Write enrichment-stats.json for observability.
// Per-source: enrichable pool size, description coverage, enriched count, field fill rates.
const STATS_PATH = path.join(DATA_DIR, 'enrichment-stats.json');
const statsBySource = {};

// Build per-source counts from all_jobs.json pool
for (const job of allJobs) {
const src = job.source || 'unknown';
if (!statsBySource[src]) {
statsBySource[src] = { total: 0, tech_us: 0, has_desc: 0, enriched: 0,
required_skills: 0, sponsors_visa: 0, question_count: 0,
min_degree: 0, experience_level_from_desc: 0,
possible_sponsor: 0, any_visa_signal: 0, visa_question_present: 0 };
}
statsBySource[src].total++;
const domains = job.tags?.domains || [];
const locs = job.tags?.locations || [];
if (domains.some(d => TECH_DOMAINS.has(d)) && locs.includes('us')) {
statsBySource[src].tech_us++;
if (descriptionsMap.get(job.id)) statsBySource[src].has_desc++;
}
}

// Field fill rates from enriched_jobs.json (post-prune)
// Only count enriched records for jobs CURRENTLY in the tech+US pool.
// Without this filter, jobs that lost their tech domain tag (e.g., TAG-1 software-title-only)
// still have enriched records, causing enriched > tech_us (>100% fill rate).
const techUsIds = new Set(allJobs
.filter(j => (j.tags?.domains || []).some(d => TECH_DOMAINS.has(d)) && (j.tags?.locations || []).includes('us'))
.map(j => j.id));

// ENRICH-QUALITY-2: Also build per-company stats for dashboard
const companyMap = {}; // company_name → { source, enriched, has_skills, has_desc, has_degree, has_visa, has_any_visa }
// ENR-47: Per-record tier classification (honest metrics, no Math.min approximation)
const tiersBySource = {};
let totalT0 = 0, totalT1 = 0, totalT2 = 0, totalT3 = 0;
for (const line of finalLines) {
try {
const obj = JSON.parse(line);
if (!techUsIds.has(obj.id)) continue; // skip enriched records for non-tech-US jobs
const src = obj.source || 'unknown';
if (!statsBySource[src]) continue;
statsBySource[src].enriched++;
if (obj.required_skills?.length > 0) statsBySource[src].required_skills++;
if (obj.sponsors_visa !== null) statsBySource[src].sponsors_visa++;
if (obj.question_count !== null) statsBySource[src].question_count++;
if (obj.min_degree !== null && obj.min_degree !== undefined) statsBySource[src].min_degree++;
if (obj.experience_level_from_desc !== null && obj.experience_level_from_desc !== undefined) statsBySource[src].experience_level_from_desc++;
if (obj.possible_sponsor !== null) statsBySource[src].possible_sponsor++;
if (obj.visa_question_present !== null) statsBySource[src].visa_question_present++;
// any_visa_signal: union of all three visa signal types
if (obj.sponsors_visa !== null || obj.possible_sponsor !== null || obj.visa_question_present !== null) statsBySource[src].any_visa_signal++;

// ENR-47: Per-record tier classification
// Transition: pre-v32 records have summary_line (description proxy) but no has_description
const hasDesc = obj.has_description !== undefined ? !!obj.has_description : !!obj.summary_line;
const hasSkills = obj.required_skills?.length > 0;
const hasDegree = obj.min_degree !== null && obj.min_degree !== undefined;
const hasVisa = obj.sponsors_visa !== null || obj.possible_sponsor !== null || obj.visa_question_present !== null;
let tier;
if (!hasDesc) tier = 0;
else if (!hasSkills) tier = 1;
else if (hasDegree && hasVisa) tier = 3;
else tier = 2; // has skills but missing degree and/or visa
if (!tiersBySource[src]) tiersBySource[src] = { t0: 0, t1: 0, t2: 0, t3: 0 };
tiersBySource[src][`t${tier}`]++;
if (tier === 0) totalT0++; else if (tier === 1) totalT1++; else if (tier === 2) totalT2++; else totalT3++;

// Per-company tracking
const co = obj.company_name || 'Unknown';
if (!companyMap[co]) companyMap[co] = { source: src, enriched: 0, has_skills: 0, has_desc: 0, has_degree: 0, has_visa: 0, has_any_visa: 0, t3: 0 };
companyMap[co].enriched++;
if (hasSkills) companyMap[co].has_skills++;
if (hasDesc) companyMap[co].has_desc++;
if (hasDegree) companyMap[co].has_degree++;
if (obj.sponsors_visa !== null) companyMap[co].has_visa++;
if (hasVisa) companyMap[co].has_any_visa++;
if (tier === 3) companyMap[co].t3++;
} catch (_) {}
}

// Top 30 companies by enriched count
const byCompany = Object.entries(companyMap)
.sort((a, b) => b[1].enriched - a[1].enriched)
.slice(0, 30)
.map(([co, s]) => ({
company: co, source: s.source, enriched: s.enriched,
skills_pct: Math.round(100 * s.has_skills / s.enriched),
desc_pct: Math.round(100 * s.has_desc / s.enriched),
degree_pct: Math.round(100 * s.has_degree / s.enriched),
visa_pct: Math.round(100 * s.has_any_visa / s.enriched),
t3_pct: Math.round(100 * s.t3 / s.enriched),
}));

// ENR-54: Per-company funnel — how many jobs entered vs how many enriched, by company.
// Iterates allJobs + processedMap to build input-side visibility (not just output-side).
// companyMap (built above) has enriched-side data; this adds the skipped/unprocessed side.
const funnelMap = {};
for (const job of allJobs) {
  const co = job.company_name || 'Unknown';
  const src = job.source || 'unknown';
  const domains = job.tags?.domains || [];
  const locations = job.tags?.locations || [];
  const isTechUs = domains.some(d => TECH_DOMAINS.has(d)) && locations.includes('us');
  const pm = processedMap[job.id];

  if (!funnelMap[co]) {
    funnelMap[co] = { company: co, source: src, total_fetched: 0, tech_us: 0, non_tech_skipped: 0, non_us_skipped: 0, desc_waiting: 0, enriched: 0, t0: 0, t1: 0, t2: 0, t3: 0 };
  }
  funnelMap[co].total_fetched++;
  if (isTechUs) funnelMap[co].tech_us++;

  if (pm && pm.status === 'skipped') {
    if (pm.reason === 'non-tech') funnelMap[co].non_tech_skipped++;
    else if (pm.reason === 'non-us') funnelMap[co].non_us_skipped++;
  }
  // WD/SR desc-waiting: in tech+US pool, not enriched, not skipped, no description
  if (!pm && (src === 'workday' || src === 'smartrecruiters') && isTechUs && !descriptionsMap.get(job.id)) {
    funnelMap[co].desc_waiting++;
  }
}
// Merge enriched counts + tiers from companyMap into funnelMap
for (const [co, s] of Object.entries(companyMap)) {
  if (funnelMap[co]) {
    funnelMap[co].enriched = s.enriched;
    // Tier breakdown comes from the enriched records loop above — reconstruct from companyMap
    funnelMap[co].t0 = s.enriched - s.has_desc; // no description = T0
    funnelMap[co].t1 = s.has_desc - s.has_skills; // has desc but no skills = T1
    // T2 vs T3: use t3 count directly
    funnelMap[co].t3 = s.t3;
    funnelMap[co].t2 = s.enriched - funnelMap[co].t0 - funnelMap[co].t1 - funnelMap[co].t3;
  }
}
const companyFunnel = Object.values(funnelMap)
  .filter(f => f.tech_us > 0)
  .sort((a, b) => b.tech_us - a.tech_us);

const totalTechUs = Object.values(statsBySource).reduce((s, v) => s + v.tech_us, 0);
const totalEnriched = Object.values(statsBySource).reduce((s, v) => s + v.enriched, 0);
const totalHasDesc = Object.values(statsBySource).reduce((s, v) => s + v.has_desc, 0);
const totalSkills = Object.values(statsBySource).reduce((s, v) => s + v.required_skills, 0);

// ENR-47: Tiers computed per-record above. No more Math.min approximation.

const enrichmentStats = {
enricher_version: ENRICHER_VERSION,
generated: new Date().toISOString(),
total_tech_us: totalTechUs,
total_enriched: totalEnriched,
total_has_description: totalHasDesc,
desc_waiting: descWaiting,
reenrichment_pending: reenrichmentPending,
tiers: { t0: totalT0, t1: totalT1, t2: totalT2, t3: totalT3 },
tiers_by_source: tiersBySource,
by_source: Object.fromEntries(
        Object.entries(statsBySource).map(([src, v]) => {
          const e = v.enriched || 1;
          return [src, { ...v,
            skills_pct: Math.round(100 * v.required_skills / e),
            degree_pct: Math.round(100 * v.min_degree / e),
            visa_pct: Math.round(100 * v.any_visa_signal / e),
          }];
        })
      ),
by_company: byCompany,
company_funnel: companyFunnel,
};

fs.writeFileSync(STATS_PATH, JSON.stringify(enrichmentStats, null, 2), 'utf8');
console.log(`[enrich-jobs] enrichment-stats.json written (${totalEnriched}/${totalTechUs} enriched, ${totalHasDesc} have description)`);

// ENRICH-QUALITY-2: Append daily snapshot to enrichment-history.jsonl (1 entry/day)
const HISTORY_PATH = path.join(DATA_DIR, 'enrichment-history.jsonl');
const today = new Date().toISOString().slice(0, 10);
let shouldAppend = true;
if (fs.existsSync(HISTORY_PATH)) {
const lines = fs.readFileSync(HISTORY_PATH, 'utf8').trim().split('\n').filter(Boolean);
if (lines.length > 0) {
try {
const last = JSON.parse(lines[lines.length - 1]);
if (last.date === today) shouldAppend = false;
} catch (_) {}
}
}
if (shouldAppend) {
const srcSummary = {};
for (const [src, v] of Object.entries(statsBySource)) {
srcSummary[src] = {
enriched: v.enriched,
skills_pct: v.enriched > 0 ? Math.round(100 * v.required_skills / v.enriched) : 0,
degree_pct: v.enriched > 0 ? Math.round(100 * v.min_degree / v.enriched) : 0,
exp_pct: v.enriched > 0 ? Math.round(100 * v.experience_level_from_desc / v.enriched) : 0,
visa_pct: v.enriched > 0 ? Math.round(100 * v.any_visa_signal / v.enriched) : 0,
};
}
// POSTING-HISTORY-1: Count jobs posted today by source
const postedToday = {};
for (const job of allJobs) {
const pa = job.posted_at;
if (pa && pa.startsWith(today)) {
const src = job.source || 'unknown';
postedToday[src] = (postedToday[src] || 0) + 1;
}
}
const snapshot = {
date: today,
enricher_version: ENRICHER_VERSION,
total_enriched: totalEnriched,
total_tech_us: totalTechUs,
pool_total: allJobs.length,
skills_pct: totalEnriched > 0 ? Math.round(100 * totalSkills / totalEnriched) : 0,
degree_pct: totalEnriched > 0 ? Math.round(100 * Object.values(statsBySource).reduce((s, v) => s + v.min_degree, 0) / totalEnriched) : 0,
exp_pct: totalEnriched > 0 ? Math.round(100 * Object.values(statsBySource).reduce((s, v) => s + v.experience_level_from_desc, 0) / totalEnriched) : 0,
visa_pct: totalEnriched > 0 ? Math.round(100 * Object.values(statsBySource).reduce((s, v) => s + v.any_visa_signal, 0) / totalEnriched) : 0,
t3_pct: totalEnriched > 0 ? Math.round(100 * totalT3 / totalEnriched) : 0,
tiers: { t0: totalT0, t1: totalT1, t2: totalT2, t3: totalT3 },
reenrichment_pending: reenrichmentPending,
posted_today: postedToday,
by_source: srcSummary,
};
fs.appendFileSync(HISTORY_PATH, JSON.stringify(snapshot) + '\n', 'utf8');
console.log(`[enrich-jobs] enrichment-history.jsonl: appended snapshot for ${today}`);
}
}
}

main().catch(err => { console.error('[enrich-jobs] Fatal:', err); process.exit(1); });