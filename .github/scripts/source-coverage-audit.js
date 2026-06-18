#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');

const DATA_DIR = path.join(process.cwd(), '.github', 'data');
const SOURCES = ['workday', 'oracle', 'smartrecruiters', 'icims', 'microsoft', 'tiktok', 'apple'];
const TECH = new Set(['software', 'data_science', 'hardware', 'ai']);

function parseJsonOrNdjsonFile(filePath) {
  if (!fs.existsSync(filePath)) return [];
  const text = fs.readFileSync(filePath, 'utf8').trim();
  if (!text) return [];
  if (text[0] === '[') {
    try {
      const parsed = JSON.parse(text);
      return Array.isArray(parsed) ? parsed : [];
    } catch {}
  }
  return text.split('\n').filter(Boolean).map(line => {
    try { return JSON.parse(line); } catch { return null; }
  }).filter(Boolean);
}

function readJson(filePath, fallback = {}) {
  if (!fs.existsSync(filePath)) return fallback;
  try { return JSON.parse(fs.readFileSync(filePath, 'utf8')); } catch { return fallback; }
}

function sourceOf(job) {
  return String(job?.source || job?.fetcher_type || 'unknown').toLowerCase();
}

function idOf(row) {
  return row?.id || row?.job_id || null;
}

function isTechUs(job) {
  const tags = job?.tags || {};
  return (tags.locations || []).includes('us') && (tags.domains || []).some(domain => TECH.has(domain));
}

function pct(n, d) {
  return d ? Number((100 * n / d).toFixed(1)) : 0;
}

function loadSidecars(allJobsById) {
  const files = fs.readdirSync(DATA_DIR).filter(name => /^descriptions-.*\.jsonl$/.test(name)).sort();
  const activeIdsBySource = new Map();
  const rawRowsBySource = {};
  for (const file of files) {
    const rows = parseJsonOrNdjsonFile(path.join(DATA_DIR, file));
    const sourceFromFile = file.replace(/^descriptions-/, '').replace(/\.jsonl$/, '').replace(/-\d+$/, '');
    for (const row of rows) {
      const id = idOf(row);
      const text = String(row?.description_text || row?.extraction_text || row?.description || '').trim();
      if (!id || text.length < 50) continue;
      const source = sourceFromFile.startsWith('enriched') ? sourceOf(allJobsById.get(id) || {}) : sourceFromFile;
      if (!source || source === 'unknown') continue;
      rawRowsBySource[source] = (rawRowsBySource[source] || 0) + 1;
      if (!allJobsById.has(id)) continue;
      if (!activeIdsBySource.has(source)) activeIdsBySource.set(source, new Set());
      activeIdsBySource.get(source).add(id);
    }
  }
  return { files, activeIdsBySource, rawRowsBySource };
}

function loadSupplementalLanes() {
  const rowsBySource = new Map();
  const metaBySource = new Map();

  const customJobs = readJson(path.join(DATA_DIR, 'supplemental-custom-jobs.json'), []);
  const customMeta = readJson(path.join(DATA_DIR, 'supplemental-custom-metadata.json'), null);
  for (const row of customJobs) {
    const source = sourceOf(row);
    if (!source) continue;
    if (!rowsBySource.has(source)) rowsBySource.set(source, []);
    rowsBySource.get(source).push(row);
  }
  if (customMeta?.sources) {
    for (const source of Object.keys(customMeta.sources)) metaBySource.set(source, customMeta);
  }

  const oracleJobs = readJson(path.join(DATA_DIR, 'supplemental-oracle-jobs.json'), []);
  const oracleMeta = readJson(path.join(DATA_DIR, 'supplemental-oracle-metadata.json'), null);
  if (oracleJobs.length) rowsBySource.set('oracle', oracleJobs);
  if (oracleMeta) metaBySource.set('oracle', oracleMeta);

  return { rowsBySource, metaBySource };
}

function classify(row) {
  const flags = [];
  if (row.tech_us_rows === 0) return { severity: 'info', flags };
  if (row.published_pct < 80) flags.push('low_published_coverage');
  if (row.sidecar_active_rows < row.published_rows && row.published_pct >= 90) flags.push('published_gt_raw_sidecar');
  if (row.sidecar_active_rows === 0 && row.published_rows === 0) flags.push('no_source_text_surface');
  if (row.enriched_pct < 80) flags.push('low_enrichment_coverage');
  if (row.enriched_rows > 0 && row.skills_pct < 70) flags.push('low_skill_fill');
  if (row.supplemental_rows > 0 && row.supplemental_with_desc > 0 && row.published_rows < row.tech_us_rows * 0.5) {
    flags.push('supplemental_stage_gap');
  }
  let severity = 'ok';
  if (flags.includes('low_published_coverage') || flags.includes('no_source_text_surface') || flags.includes('supplemental_stage_gap')) severity = 'high';
  else if (flags.length > 0) severity = 'medium';
  return { severity, flags };
}

function main() {
  const allJobs = parseJsonOrNdjsonFile(path.join(DATA_DIR, 'all_jobs.json'));
  const usJobs = parseJsonOrNdjsonFile(path.join(DATA_DIR, 'us_jobs.json'));
  const enriched = parseJsonOrNdjsonFile(path.join(DATA_DIR, 'enriched_jobs.json'));
  const descriptions = readJson(path.join(DATA_DIR, 'softwarejobs-descriptions.json'), {});
  const descriptionsMeta = readJson(path.join(DATA_DIR, 'softwarejobs-descriptions-meta.json'), {});
  const pipelineAlert = readJson(path.join(DATA_DIR, 'pipeline-alert.json'), null);
  const allJobsById = new Map(allJobs.map(job => [idOf(job), job]).filter(([id]) => id));
  const sidecars = loadSidecars(allJobsById);
  const enrichedById = new Map(enriched.map(job => [idOf(job), job]).filter(([id]) => id));
  const supplemental = loadSupplementalLanes();

  const rows = SOURCES.map(source => {
    const pool = allJobs.filter(job => sourceOf(job) === source);
    const us = usJobs.filter(job => sourceOf(job) === source);
    const tech = us.filter(isTechUs);
    const published = tech.filter(job => Object.prototype.hasOwnProperty.call(descriptions, job.id));
    const sideIds = sidecars.activeIdsBySource.get(source) || new Set();
    const sideActive = tech.filter(job => sideIds.has(job.id));
    const enr = tech.filter(job => enrichedById.has(job.id));
    const enrSkills = enr.filter(job => ((enrichedById.get(job.id)?.required_skills || enrichedById.get(job.id)?.skills || []).length > 0));
    const supplementalRows = supplemental.rowsBySource.get(source) || [];
    const supplementalWithDesc = supplementalRows.filter(job => String(job?.description || '').trim().length >= 50).length;
    const supplementalMeta = supplemental.metaBySource.get(source) || null;
    const row = {
      source,
      pool_rows: pool.length,
      us_rows: us.length,
      tech_us_rows: tech.length,
      raw_sidecar_rows: sidecars.rawRowsBySource[source] || 0,
      sidecar_active_rows: sideActive.length,
      supplemental_rows: supplementalRows.length,
      supplemental_with_desc: supplementalWithDesc,
      supplemental_meta: supplementalMeta ? {
        generated_at: supplementalMeta.generated_at || null,
        lane_name: supplementalMeta.lane_name || null,
        jobs_fetched: supplementalMeta.jobs_fetched || supplementalRows.length,
      } : null,
      published_rows: published.length,
      published_pct: pct(published.length, tech.length),
      enriched_rows: enr.length,
      enriched_pct: pct(enr.length, tech.length),
      enriched_with_skills: enrSkills.length,
      skills_pct: pct(enrSkills.length, enr.length),
      examples: tech.filter(job => !Object.prototype.hasOwnProperty.call(descriptions, job.id)).slice(0, 8).map(job => ({ id: job.id, company: job.company_name || job.company, title: job.title })),
    };
    const classified = classify(row);
    row.severity = classified.severity;
    row.flags = classified.flags;
    return row;
  }).sort((a, b) => {
    const score = row => (row.severity === 'high' ? 2 : row.severity === 'medium' ? 1 : 0);
    return score(b) - score(a) || (b.tech_us_rows - b.published_rows) - (a.tech_us_rows - a.published_rows);
  });

  const out = {
    generated_at: new Date().toISOString(),
    truth_surfaces: {
      producer_pool: 'all_jobs.json',
      board_input: 'us_jobs.json',
      raw_sidecars: 'descriptions-*.jsonl',
      supplemental_lanes: ['supplemental-custom-jobs.json', 'supplemental-oracle-jobs.json'],
      published_board_descriptions: 'softwarejobs-descriptions.json',
      published_board_meta: 'softwarejobs-descriptions-meta.json',
      enrichment_output: 'enriched_jobs.json',
      consumer_alert: 'pipeline-alert.json',
    },
    tier_a_sources: SOURCES,
    sidecar_files_downloaded: sidecars.files,
    published_meta: descriptionsMeta,
    consumer_alert: pipelineAlert,
    rows,
  };

  const outPath = path.join(DATA_DIR, 'tier-a-source-coverage.json');
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2), 'utf8');
  console.log(`[source-coverage-audit] wrote ${outPath}`);
}

main();
