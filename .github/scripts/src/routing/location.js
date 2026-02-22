/**
 * Location-based Job Routing Module
 * Determines which location-specific Discord channel a job should be posted to
 *
 * UPDATED 2026-01-28: Fixed routing to use actual channel keys from board configs
 * Common channels across Internships & New-Grad:
 * - bay-area, new-york, pacific-northwest, remote-usa, other-usa
 * - southern-california (Internships only)
 */

const { LOCATION_CHANNEL_CONFIG } = require('../discord/config');

// Shared city → channel-key map used by both routing functions
const CITY_MATCHES = {
  // Bay Area -> bay-area
  'san francisco': 'bay-area', 'oakland': 'bay-area', 'berkeley': 'bay-area',
  'san jose': 'bay-area', 'palo alto': 'bay-area', 'fremont': 'bay-area',
  'hayward': 'bay-area', 'richmond': 'bay-area', 'daly city': 'bay-area',
  'alameda': 'bay-area', 'cupertino': 'bay-area', 'santa clara': 'bay-area',
  'mountain view': 'bay-area', 'sunnyvale': 'bay-area', 'san bruno': 'bay-area',
  'menlo park': 'bay-area', 'redwood city': 'bay-area', 'milpitas': 'bay-area',
  'frisco': 'bay-area',
  // NYC Metro -> new-york
  'new york': 'new-york', 'manhattan': 'new-york', 'brooklyn': 'new-york',
  'queens': 'new-york', 'bronx': 'new-york', 'staten island': 'new-york',
  'jersey city': 'new-york', 'newark': 'new-york', 'hoboken': 'new-york',
  'white plains': 'new-york', 'yonkers': 'new-york', 'long island city': 'new-york',
  'astoria': 'new-york',
  // Seattle/PNW -> pacific-northwest
  'seattle': 'pacific-northwest', 'bellevue': 'pacific-northwest', 'tacoma': 'pacific-northwest',
  'everett': 'pacific-northwest', 'renton': 'pacific-northwest', 'kent': 'pacific-northwest',
  'redmond': 'pacific-northwest', 'kirkland': 'pacific-northwest', 'bothell': 'pacific-northwest',
  'vancouver': 'pacific-northwest',
  // SoCal -> southern-california (maps to socal-int for internships, other-usa for new-grad)
  'los angeles': 'southern-california', 'santa monica': 'southern-california',
  'pasadena': 'southern-california', 'long beach': 'southern-california',
  'glendale': 'southern-california', 'irvine': 'southern-california',
  'anaheim': 'southern-california', 'burbank': 'southern-california',
  'torrance': 'southern-california', 'san diego': 'southern-california',
  'chula vista': 'southern-california', 'oceanside': 'southern-california',
  'escondido': 'southern-california', 'carlsbad': 'southern-california',
  'el cajon': 'southern-california', 'la jolla': 'southern-california',
  'culver city': 'southern-california',
};

const CITY_ABBREVIATIONS = { 'sf': 'bay-area', 'nyc': 'new-york' };

const OTHER_US_STATES = new Set([
  'tx', 'texas', 'ma', 'massachusetts', 'il', 'illinois', 'dc', 'district of columbia',
  'va', 'virginia', 'md', 'maryland', 'co', 'colorado', 'fl', 'florida', 'ga', 'georgia',
  'nc', 'north carolina', 'tn', 'tennessee', 'az', 'arizona', 'ut', 'utah',
  'nv', 'nevada', 'or', 'oregon', 'mi', 'michigan', 'oh', 'ohio', 'pa', 'pennsylvania',
  'mn', 'minnesota', 'wi', 'wisconsin', 'ct', 'connecticut', 'in', 'indiana',
  'ks', 'kansas', 'ky', 'kentucky', 'la', 'louisiana', 'mo', 'missouri',
  'nj', 'new jersey', 'ok', 'oklahoma', 'sc', 'south carolina',
]);

/**
 * Core location routing logic — shared by both exported functions.
 * @param {Object} job
 * @param {Object} locationConfig - LOCATION_CHANNEL_CONFIG to resolve channel IDs from
 * @returns {string|null} Channel ID or null
 */
function resolveLocationChannel(job, locationConfig) {
  const city = (job.job_city || '').toLowerCase().trim();
  const state = (job.job_state || '').toLowerCase().trim();
  const title = (job.job_title || '').toLowerCase();
  const description = (job.job_description || '').toLowerCase();
  const combined = `${title} ${description} ${city} ${state}`;

  // 1. Exact city match (most reliable)
  for (const [searchCity, channelKey] of Object.entries(CITY_MATCHES)) {
    if (city.includes(searchCity)) return locationConfig[channelKey] || null;
  }

  // 2. City abbreviations
  for (const [abbr, channelKey] of Object.entries(CITY_ABBREVIATIONS)) {
    if (city === abbr || city.split(/\s+/).includes(abbr)) return locationConfig[channelKey] || null;
  }

  // 3. City name anywhere in title + description
  for (const [searchCity, channelKey] of Object.entries(CITY_MATCHES)) {
    if (combined.includes(searchCity)) return locationConfig[channelKey] || null;
  }

  // 4. State-based fallback
  if (state) {
    if (state === 'ca' || state === 'california') {
      return locationConfig['southern-california'] || locationConfig['other-usa'] || null;
    }
    if (state === 'ny' || state === 'new york') return locationConfig['new-york'] || null;
    if (state === 'wa' || state === 'washington') return locationConfig['pacific-northwest'] || null;
    if (OTHER_US_STATES.has(state)) return locationConfig['other-usa'] || null;
  }

  // 5. Remote USA
  const isRemoteLocation = job.job_is_remote === true || city.includes('remote') || state.includes('remote');
  const hasStrongRemoteKeyword = /\b(remote|work from home|wfh|distributed|anywhere|location independent)\b/i.test(combined);
  const hasNonUSLocation = /\b(london|paris|berlin|toronto|vancouver|montreal|sydney|melbourne|tokyo|singapore|hong kong|dubai|mumbai|bangalore)\b/i.test(combined);

  if ((isRemoteLocation || hasStrongRemoteKeyword) && !hasNonUSLocation) {
    return locationConfig['remote-usa'] || null;
  }

  return null;
}

/**
 * Determine which location channel a job should go to (new-grad config)
 * @param {Object} job - Job object with location data
 * @returns {string|null} Channel ID or null if no location match
 */
function getJobLocationChannel(job) {
  return resolveLocationChannel(job, LOCATION_CHANNEL_CONFIG);
}

/**
 * Determine which location channel a job should go to, using a provided config
 * @param {Object} job - Job object with location data
 * @param {Object} locationConfig - LOCATION_CHANNEL_CONFIG to use (new-grad or internship)
 * @returns {string|null} Channel ID or null if no location match
 */
function getJobLocationChannelWithConfig(job, locationConfig) {
  return resolveLocationChannel(job, locationConfig);
}

module.exports = {
  getJobLocationChannel,
  getJobLocationChannelWithConfig
};
