/**
 * enrich-jobs.js
 *
 * Reads all_jobs.json, enriches new jobs (up to BATCH_SIZE per run),
 * appends results to enriched_jobs.json (JSONL).
 *
 * Enrichment extracts:
 *   - required_skills[]      (from requirements/qualifications sections)
 *   - nice_to_have_skills[]  (from preferred/bonus sections)
 *   - sponsors_visa          (true | false | null)
 *   - is_remote              (bool, from tags.locations includes 'remote')
 *   - experience_level       (from tags.employment)
 *   + denormalized display fields: title, company_name, job_city, job_state, url, posted_at
 */

'use strict';

const fs = require('fs');
const path = require('path');
const he = require('he');

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const BATCH_SIZE = 40;
const DATA_DIR = path.join(process.cwd(), '.github', 'data');
const ALL_JOBS_PATH = path.join(DATA_DIR, 'all_jobs.json');
const ENRICHED_PATH = path.join(DATA_DIR, 'enriched_jobs.json');
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
// HTML → plain text
// Descriptions are HTML-entity-encoded. Decode first, then strip tags.
// ---------------------------------------------------------------------------
function toPlainText(html) {
  if (!html) return '';
  const decoded = he.decode(html);
  // Replace block-level tags with newline for section splitting
  const withNewlines = decoded.replace(/<\/(p|div|li|h[1-6]|br)>/gi, '\n');
  // Strip remaining tags
  const stripped = withNewlines.replace(/<[^>]+>/g, ' ');
  // Normalize whitespace (but preserve newlines for section detection)
  return stripped.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
}

// ---------------------------------------------------------------------------
// Section splitter
// Returns { required: string, preferred: string }
// ---------------------------------------------------------------------------
const REQUIRED_HEADERS = [
  /requirements?[:\s]/i,
  /qualifications?[:\s]/i,
  /what you (need|bring|must have)[:\s]/i,
  /minimum qualifications?[:\s]/i,
  /basic qualifications?[:\s]/i,
  /required skills?[:\s]/i,
  /must[ -]have[:\s]/i,
  /you (will need|should have)[:\s]/i,
];

const PREFERRED_HEADERS = [
  /preferred qualifications?[:\s]/i,
  /nice[ -]to[ -]have[:\s]/i,
  /bonus (points?|if|qualifications?)?[:\s]/i,
  /preferred skills?[:\s]/i,
  /desired qualifications?[:\s]/i,
  /plus (if|points?)?[:\s]/i,
  /it['']?s? (a )?(bonus|plus|nice)[:\s]/i,
  /while not required[:\s]/i,
  /added (plus|bonus)[:\s]/i,
];

function splitSections(text) {
  const lines = text.split('\n');
  let requiredStart = -1;
  let preferredStart = -1;

  // Collect all section boundaries so we can cap extraction correctly
  const allBoundaries = []; // { idx, type }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
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
const AMBIGUOUS_TERMS = new Set(['go', 'r', 'c', 'rest', 'restful', 'assembly', 'lean', 'chef']);

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
// Main
// ---------------------------------------------------------------------------
function loadAllJobs() {
  const lines = fs.readFileSync(ALL_JOBS_PATH, 'utf8').trim().split('\n');
  return lines.map(l => JSON.parse(l));
}

function loadEnrichedIds() {
  if (!fs.existsSync(ENRICHED_PATH)) return new Set();
  const lines = fs.readFileSync(ENRICHED_PATH, 'utf8').trim().split('\n').filter(Boolean);
  const ids = new Set();
  for (const line of lines) {
    try {
      const obj = JSON.parse(line);
      if (obj.id) ids.add(obj.id);
    } catch (_) {}
  }
  return ids;
}

function enrichJob(job, termMap) {
  const plainText = toPlainText(job.description || '');
  const { required, preferred } = splitSections(plainText);

  const requiredSkills = matchSkills(required, termMap);
  const niceToHaveSkills = matchSkills(preferred, termMap).filter(
    s => !requiredSkills.includes(s)
  );

  const sponsorsVisa = detectVisa(plainText);
  const isRemote = (job.tags?.locations || []).includes('remote');
  const experienceLevel = job.tags?.employment || null;

  return {
    id: job.id,
    required_skills: requiredSkills,
    nice_to_have_skills: niceToHaveSkills,
    sponsors_visa: sponsorsVisa,
    is_remote: isRemote,
    experience_level: experienceLevel,
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

function main() {
  console.log('[enrich-jobs] Starting enrichment run');

  const termMap = loadTaxonomy();
  console.log(`[enrich-jobs] Taxonomy loaded: ${termMap.size} terms`);

  const allJobs = loadAllJobs();
  console.log(`[enrich-jobs] Total jobs in pool: ${allJobs.length}`);

  const enrichedIds = loadEnrichedIds();
  console.log(`[enrich-jobs] Already enriched: ${enrichedIds.size}`);

  const pending = allJobs.filter(j => !enrichedIds.has(j.id));
  console.log(`[enrich-jobs] Pending enrichment: ${pending.length}`);

  const batch = pending.slice(0, BATCH_SIZE);
  console.log(`[enrich-jobs] Processing batch of ${batch.length}`);

  if (batch.length === 0) {
    console.log('[enrich-jobs] Nothing to enrich. Exiting.');
    return;
  }

  const results = batch.map(job => enrichJob(job, termMap));

  // Append new results
  const newLines = results.map(r => JSON.stringify(r)).join('\n') + '\n';
  fs.appendFileSync(ENRICHED_PATH, newLines, 'utf8');
  console.log(`[enrich-jobs] Enriched and appended ${results.length} jobs`);

  // Prune enriched_jobs.json to only keep IDs still present in all_jobs.json.
  // all_jobs.json is a 14-day rolling window — jobs that age out are gone and
  // their enriched records are no longer useful.
  const liveIds = new Set(allJobs.map(j => j.id));
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
  console.log(`[enrich-jobs] Stats: ${withRequired}/${results.length} had required skills, ${withVisa}/${results.length} had visa signal`);
  console.log(`[enrich-jobs] Total enriched (post-prune): ${prunedLines.length}`);
}

main();
