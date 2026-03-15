/**
 * Global Dedupe Manager
 *
 * Tracks job fingerprints across all repos to prevent duplicate posts
 */

const fs = require('fs');
const path = require('path');

const DATA_DIR = path.join(process.cwd(), '.github', 'data');
const STORE_FILE = path.join(DATA_DIR, 'global-dedupe-store.json');
const TTL_DAYS = 7; // Time-to-live for dedupe entries (matches 7-day staleness gate)

class GlobalDedupeManager {
  constructor() {
    this.store = this.loadStore();
  }

  /**
   * Load dedupe store from disk
   */
  loadStore() {
    try {
      if (fs.existsSync(STORE_FILE)) {
        const data = fs.readFileSync(STORE_FILE, 'utf8');
        const json = JSON.parse(data);
        this.cleanup(json); // Clean old entries
        return json;
      }
    } catch (error) {
      console.warn('⚠️  Could not load global dedupe store:', error.message);
    }

    // Return default store
    return {
      version: 1,
      lastUpdated: new Date().toISOString(),
      fingerprints: {}
    };
  }

  /**
   * Save dedupe store to disk
   */
  saveStore() {
    try {
      this.store.lastUpdated = new Date().toISOString();
      fs.mkdirSync(DATA_DIR, { recursive: true });
      fs.writeFileSync(STORE_FILE, JSON.stringify(this.store, null, 2), 'utf8');
    } catch (error) {
      console.error('❌ Could not save global dedupe store:', error.message);
    }
  }

  /**
   * Check if fingerprint has been posted (within TTL)
   */
  hasBeenPosted(fingerprint) {
    const entry = this.store.fingerprints[fingerprint];
    if (!entry) {
      return false;
    }

    // Check if entry is still valid (within TTL)
    const postedDate = new Date(entry.postedAt);
    const now = new Date();
    const daysSincePosted = (now - postedDate) / (1000 * 60 * 60 * 24);

    if (daysSincePosted > TTL_DAYS) {
      // Entry expired, remove it
      delete this.store.fingerprints[fingerprint];
      return false;
    }

    return true;
  }

  /**
   * Mark job as posted
   */
  markAsPosted(fingerprint, jobId, sourceRepo, channelId, messageId) {
    this.store.fingerprints[fingerprint] = {
      jobId,
      sourceRepo,
      channelId,
      messageId,
      postedAt: new Date().toISOString()
    };
  }

  /**
   * Remove old entries (outside TTL window)
   */
  cleanup(store = this.store) {
    const now = new Date();
    let cleaned = 0;

    for (const fingerprint in store.fingerprints) {
      const entry = store.fingerprints[fingerprint];
      const postedDate = new Date(entry.postedAt);
      const daysSincePosted = (now - postedDate) / (1000 * 60 * 60 * 24);

      if (daysSincePosted > TTL_DAYS) {
        delete store.fingerprints[fingerprint];
        cleaned++;
      }
    }

    if (cleaned > 0) {
      console.log(`🧹 Cleaned ${cleaned} expired dedupe entries`);
    }
  }

  /**
   * Get statistics
   */
  getStats() {
    const totalEntries = Object.keys(this.store.fingerprints).length;
    const entriesByRepo = {};

    for (const entry of Object.values(this.store.fingerprints)) {
      entriesByRepo[entry.sourceRepo] = (entriesByRepo[entry.sourceRepo] || 0) + 1;
    }

    return {
      totalEntries,
      entriesByRepo,
      ttlDays: TTL_DAYS,
      lastUpdated: this.store.lastUpdated
    };
  }
}

module.exports = GlobalDedupeManager;
