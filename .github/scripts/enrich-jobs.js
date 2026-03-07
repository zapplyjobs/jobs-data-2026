/**
 * enrich-jobs.js
 *
 * Reads all_jobs.json, enriches new jobs (up to BATCH_SIZE per run),
 * appends results to enriched_jobs.json (JSONL).
 *
 * Enrichment extracts:
 *   - required_skills[]        (from requirements/qualifications sections)
 *   - nice_to_have_skills[]    (from preferred/bonus sections)
 *   - sponsors_visa            (true | false | null — text-based, kept as fallback)
 *   - visa_question_present    (true | false | null — from ATS application form)
 *   - is_remote                (bool, from tags.locations includes 'remote')
 *   - experience_level         (from tags.employment)
 *   - summary_line             (string | null — DATA-7: first non-boilerplate sentence)
 *   - key_requirements         (string[] — DATA-7: top 6 required_skills, display alias)
 *   - is_simple_apply          (bool | null — DATA-8: GH only, question_count <= 13)
 *   - question_count           (int | null — DATA-8: GH only; Ashby/Lever pending schema verification)
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
const BATCH_SIZE = 40;
const DATA_DIR = path.join(process.cwd(), '.github', 'data');
const ALL_JOBS_PATH = path.join(DATA_DIR, 'all_jobs.json');
const ENRICHED_PATH = path.join(DATA_DIR, 'enriched_jobs.json');
const PROCESSED_PATH = path.join(DATA_DIR, 'processed_ids.json');
const DESCRIPTIONS_PATH = path.join(DATA_DIR, 'descriptions.jsonl');
const TAXONOMY_PATH = path.join(__dirname, 'enrich', 'skills-taxonomy.json');

// ---------------------------------------------------------------------------
// Load taxonomy — flatten all categories into a single Set for O(1) lookup,
// preserving canonical casing from the JSON for output.
// ---------------------------------------------------------------------------
function loadTaxonomy() {
  const raw = JSON.parse(fs.readFileSync(TAXONOMY_PATH, 'utf8'));
  // Map lowercase → canonical term
  const termMap = new Map();
  for (const [category, terms] of Object.entries(raw)) {
    if (category === '_meta') continue;
    for (const term of terms) {
      termMap.set(term.toLowerCase(), term);
    }
  }
  return termMap;
}

// ---------------------------------------------------------------------------
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
  /what we.re looking for[:\s]?$/i,
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
    const end = nextBoundary ? Math.min(nextBoundary.idx, start + 40) : start + 40;
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
  return lines.map(l => JSON.parse(l));
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
  const ids = loadProcessedIds();
  if (!fs.existsSync(ENRICHED_PATH)) return ids;
  const lines = fs.readFileSync(ENRICHED_PATH, 'utf8').trim().split('\n').filter(Boolean);
  for (const line of lines) {
    try {
      const obj = JSON.parse(line);
      if (obj.id) ids.add(obj.id);
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
];

function extractSummaryLine(plainText) {
  if (!plainText) return null;

  // Pre-split on double newlines to isolate paragraphs.
  // Plain-text section headers ("Opportunity Overview", "About the Role") have no trailing
  // punctuation, so sentence splitting alone concatenates them with the next sentence.
  // Discarding ≤4-word paragraphs removes headers without needing an exhaustive list.
  const paragraphs = plainText.split(/\n\n+/).map(p => p.replace(/\n/g, ' ').trim()).filter(Boolean);
  const substantiveParagraphs = paragraphs.filter(p => {
    if (p.length < 30) return false;
    if (/^###SECTION:/.test(p)) return false;
    // Discard short paragraphs that are likely section headers (≤4 words)
    const wordCount = p.split(/\s+/).filter(Boolean).length;
    if (wordCount <= 4) return false;
    return true;
  });

  // Sentence-split each substantive paragraph in order, return first non-boilerplate sentence
  for (const para of substantiveParagraphs) {
    const sentences = para.split(/(?<=[.!?])\s+/).map(s => s.trim()).filter(Boolean);
    for (const sentence of sentences) {
      if (sentence.length < 30) continue;
      if (BOILERPLATE_OPENERS.some(re => re.test(sentence))) continue;
      if (/^###SECTION:/.test(sentence)) continue;
      return sentence.length > 200 ? sentence.slice(0, 200).trimEnd() + '…' : sentence;
    }
  }

  // Fallback: first sentence of full text, stripped of ###SECTION:### markers
  const allSentences = plainText.split(/(?<=[.!?])\s+/).map(s => s.trim()).filter(Boolean);
  const fallback = allSentences.find(s => s.length >= 10);
  if (!fallback) return null;
  const stripped = fallback.replace(/###SECTION:[^#]*###\s*/g, '').trim();
  return stripped.slice(0, 200) || null;
}

const TECH_DOMAINS = new Set(['software', 'data_science', 'hardware', 'ai']);

function isEnrichable(job) {
  const domains = job.tags?.domains || [];
  const locations = job.tags?.locations || [];
  return domains.some(d => TECH_DOMAINS.has(d)) && locations.includes('us');
}

async function enrichJob(job, termMap, descriptionsMap) {

  const rawDescription = descriptionsMap.get(job.id) || null;
  const plainText = toPlainText(rawDescription || '');
  const { required, preferred } = splitSections(plainText);

  if (!required) {
    console.log(`[enrich] no section found for ${job.id} — using full text`);
  }
  const text = required || plainText;
  const requiredSkills = matchSkills(text, termMap);
  const niceToHaveSkills = matchSkills(preferred, termMap).filter(
    s => !requiredSkills.includes(s)
  );

  const sponsorsVisa = detectVisa(plainText);
  const { visaPresent: visaQuestionPresent, questionCount } = await fetchApplicationVisaStatus(job);
  const isRemote = (job.tags?.locations || []).includes('remote');
  const experienceLevel = job.tags?.employment || null;

  // DATA-7: summary_line — try required section first (role-specific), fall back to full text
  const summaryLine = (required ? extractSummaryLine(required) : null) ?? extractSummaryLine(plainText);
  // DATA-7: key_requirements — alias for required_skills (already extracted, no new work)
  const keyRequirements = requiredSkills.slice(0, 6);

  // DATA-8: simple apply detection — GH only (question count exact); Ashby/Lever schema unverified
  const isSimpleApply = questionCount !== null ? questionCount <= SIMPLE_APPLY_THRESHOLD : null;

  return {
    id: job.id,
    source: job.source || null,
    enricher_version: 1,
    required_skills: requiredSkills,
    nice_to_have_skills: niceToHaveSkills,
    sponsors_visa: sponsorsVisa,
    visa_question_present: visaQuestionPresent,
    is_remote: isRemote,
    experience_level: experienceLevel,
    // DATA-7: job summary panel fields
    summary_line: summaryLine,
    key_requirements: keyRequirements,
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

  const descriptionsMap = loadDescriptionsMap();
  console.log(`[enrich-jobs] Descriptions loaded: ${descriptionsMap.size} entries`);

  const allJobs = loadAllJobs();
  console.log(`[enrich-jobs] Total jobs in pool: ${allJobs.length}`);

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
  for (const job of pending) {
    if (!isEnrichable(job) && !processedMap[job.id]) {
      const domains = job.tags?.domains || [];
      const locations = job.tags?.locations || [];
      const reason = !domains.some(d => TECH_DOMAINS.has(d)) ? 'non-tech' : 'non-us';
      processedMap[job.id] = { status: 'skipped', reason, processed_at: now };
      bulkMarked++;
    }
  }
  if (bulkMarked > 0) {
    console.log(`[enrich-jobs] Bulk-marked ${bulkMarked} non-enrichable jobs as processed (non-tech or non-US)`);
  }

  const enrichablePending = pending.filter(j => isEnrichable(j));
  console.log(`[enrich-jobs] Enrichable pending: ${enrichablePending.length}`);

  const batch = enrichablePending.slice(0, BATCH_SIZE);
  console.log(`[enrich-jobs] Processing batch of ${batch.length}`);

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

  const enriched = await Promise.all(batch.map(job => enrichJob(job, termMap, descriptionsMap)));
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

  // Prune enriched_jobs.json to only keep IDs still present in all_jobs.json.
  // all_jobs.json is a 14-day rolling window — jobs that age out are gone and
  // their enriched records are no longer useful.
  if (fs.existsSync(ENRICHED_PATH)) {
    const allEnrichedLines = fs.readFileSync(ENRICHED_PATH, 'utf8').trim().split('\n').filter(Boolean);
    const prunedLines = allEnrichedLines.filter(line => {
      try {
        const obj = JSON.parse(line);
        return liveIds.has(obj.id);
      } catch (_) {
        return false; // drop malformed lines
      }
    });

    if (prunedLines.length < allEnrichedLines.length) {
      const pruned = allEnrichedLines.length - prunedLines.length;
      fs.writeFileSync(ENRICHED_PATH, prunedLines.join('\n') + '\n', 'utf8');
      console.log(`[enrich-jobs] Pruned ${pruned} expired records (no longer in all_jobs.json)`);
    }

    // Quick stats
    const withRequired = results.filter(r => r.required_skills.length > 0).length;
    const withVisa = results.filter(r => r.sponsors_visa !== null).length;
    const withVisaForm = results.filter(r => r.visa_question_present !== null).length;
    console.log(`[enrich-jobs] Stats: ${withRequired}/${results.length} had required skills, ${withVisa}/${results.length} had visa text signal, ${withVisaForm}/${results.length} had visa form signal`);
    console.log(`[enrich-jobs] Total enriched (post-prune): ${prunedLines.length}`);
  }
}

main().catch(err => { console.error('[enrich-jobs] Fatal:', err); process.exit(1); });
