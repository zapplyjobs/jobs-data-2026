# jobs-data-2026

Job data pipeline and Discord poster for [Zapply](https://zapply.jobs). Hosts the aggregated job feed and posts new positions to the Zapply Discord server.

## What it does

- Receives `all_jobs.json` from the aggregation pipeline every 15 minutes
- Posts new entry-level and internship jobs to Discord channels (routed by industry and location)
- Runs daily/weekly/monthly stats reports to Discord
- Monitors pipeline health and fires alerts on failures

## Related repositories

- [New-Grad-Jobs-2026](https://github.com/zapplyjobs/New-Grad-Jobs-2026) — entry-level jobs board
- [Internships-2026](https://github.com/zapplyjobs/Internships-2026) — internships board
- [New-Grad-Software-Engineering-Jobs-2026](https://github.com/zapplyjobs/New-Grad-Software-Engineering-Jobs-2026)
- [New-Grad-Data-Science-Jobs-2026](https://github.com/zapplyjobs/New-Grad-Data-Science-Jobs-2026)
- [New-Grad-Hardware-Engineering-Jobs-2026](https://github.com/zapplyjobs/New-Grad-Hardware-Engineering-Jobs-2026)
- [New-Grad-Nursing-Jobs-2026](https://github.com/zapplyjobs/New-Grad-Nursing-Jobs-2026)
