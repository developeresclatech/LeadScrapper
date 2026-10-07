# Multi-Platform Lead Generator

A Python CLI that collects US/Canada client leads from Upwork, Vollna,
Freelancer, and Guru, analyzes them, and exports the results to CSV/JSON and
optionally Google Sheets.

## Architecture

This project is a concurrent, queue-oriented modular monolith. Platform
scrapers run independently. Their nested keyword workers process leads and
send accepted results to a dedicated output queue for that platform.

```text
main.py
   |
   v
LeadEngine
   |
   v
PlatformScheduler
   |
   +-- Upwork platform worker
   |      +-- keyword workers: scrape -> analyze -> qualify
   |      `-- bounded Upwork output queue -> Upwork output worker ---+
   +-- Freelancer platform worker                                  |
   |      +-- keyword workers: scrape -> analyze -> qualify         |
   |      `-- bounded Freelancer output queue -> output worker -----+
   +-- Guru platform worker                                        |
   |      +-- keyword workers: scrape -> analyze -> qualify         |
   |      `-- bounded Guru output queue -> output worker -----------+
   `-- Vollna platform worker                                      |
          +-- fetch once -> resolve job countries -> qualify        |
          `-- bounded Vollna output queue -> output worker ---------+
                                                                   |
                                          +------------------------+--+
                                          |                           |
                                          v                           v
                                  SQLite checkpoints       synchronized Sheets writes
                                                                      |
                                                               unified Leads tab
                                                                      |
                                                               batches of 5

LeadEngine/main thread: supervise events, print progress, export final CSV/JSON
```

Platforms run concurrently with a bounded worker pool. Upwork Selenium uses
two keyword workers with isolated Chrome/ChromeDriver sessions. Freelancer
and Guru use three keyword workers with separate scraper sessions. The global
browser semaphore still permits at most two simultaneous browsers. Vollna
fetches its RSS feed once per run, filters every keyword locally, and uses an
authenticated Upwork detail resolver only for linked jobs whose country is
missing. Resolved countries are cached by Upwork job ID across both sources.

Upwork searches are prefiltered at the source with separate client-location
queries for `United States` and `Canada`. The per-keyword result allowance is
divided between both countries, and each request asks for 50 jobs per page.
The central processing pipeline does not perform a second country rejection.
Upwork is restricted by its search query; Freelancer and Guru filter their
platform country metadata before returning; Vollna resolves its linked
Upwork client country and filters before analysis.

The platform scraper implementations retain their original selectors,
requests, parsing, login, and fallback behavior. The adapter layer only gives
them a common `scrape(keyword)` interface.

## Processing lifecycle

For every keyword result, the nested keyword worker:

1. Convert platform output to the shared `JobLead` model.
2. Remove duplicate titles across the current run.
3. Extract business information and calculate the lead score.
4. Trust Upwork's server-side client-country query when present; otherwise
   resolve missing Upwork/Vollna countries from authenticated detail pages.
5. Put each accepted lead into its platform's bounded output queue.

Each platform output worker then saves its leads to `data/leads.db` and adds
them to the shared Google Sheets buffer. The buffer uploads every 5 eligible
leads and flushes the remainder as platforms finish. Actual operations on the
unified `Leads` tab use a short shared lock so concurrent platform workers
cannot race worksheet creation, duplicate checks, or row insertion. The main
thread only supervises progress and creates the final CSV/JSON after all
output queues drain.

## Project layout

```text
upwork_scraper/
|-- config.py                  runtime configuration
|-- models.py                  shared JobLead model
|-- analyzer.py                extraction, scoring, and classification
|-- engine.py                  backward-compatible LeadEngine import
|
|-- platforms/
|   |-- base.py                common adapter
|   `-- registry.py            wraps existing scraper classes
|
|-- orchestration/
|   |-- engine.py              application coordinator
|   |-- scheduler.py           bounded platform concurrency
|   |-- events.py              worker-to-engine progress messages
|   `-- output_worker.py       per-platform output queues and workers
|
|-- pipeline/
|   |-- processor.py           processing stage coordinator
|   |-- deduplicator.py        thread-safe run deduplication
|   `-- location_filter.py     retained location-matching utility (not active)
|
|-- storage/
|   `-- sqlite_repository.py   durable qualified-lead checkpoints
|
|-- exporters/
|   |-- local.py               CSV/JSON export
|   |-- sheets.py              synchronized unified-tab batch upload
|   |-- rows.py                output row conversion
|   `-- schema.py              stable headers and tab mappings
|
|-- scraper.py                 existing Upwork fallback scraper
|-- selenium_scraper.py        existing authenticated Upwork scraper
|-- vollna.py                  existing Vollna RSS scraper
|-- freelancer.py              existing Freelancer scraper
|-- guru.py                    existing Guru scraper
`-- bark_scraper.py            preserved but temporarily disabled
```

## Installation

Python 3.10 or newer is recommended.

```powershell
python -m pip install -r requirements.txt
```

Chrome is required for the authenticated Upwork and Bark workers. Local runs
detect the installed Chrome major version automatically so
`undetected-chromedriver` downloads a compatible driver. For nonstandard
installations, set `CHROME_BINARY` and, only if detection still fails,
`CHROME_VERSION_MAIN` in `.env`.

## Running with Docker

The Docker image includes Python, Chromium, and a virtual display for the
Selenium workers. Docker Compose also persists the SQLite database and local
exports in the host's `data/` and `output/` directories.

1. Copy `.env.example` to `.env` and add the credentials you need.
2. Put the Google service-account key at `service-account.json`. When Sheets
   is disabled, an empty placeholder file is sufficient for the Compose mount.
3. Build and run one collection cycle.

### Docker Compose commands

Build the image and start the scraper:

```powershell
docker compose up --build
```

While the scraper is running, open
[`http://localhost:7900/vnc.html`](http://localhost:7900/vnc.html) to view and
interact with Chromium inside Docker. The port is bound to localhost only.
Docker keeps a separate Chrome profile for each browser worker in the named
`browser_profiles` volume, so Upwork login and verification state survives
container replacement without concurrent workers locking the same profile.

If Upwork displays a Cloudflare verification page, complete it in the browser
view within the configured `UPWORK_VERIFICATION_TIMEOUT` (180 seconds by
default). The scraper detects this page and waits instead of incorrectly
reporting that it is already logged in. A visible browser does not itself
bypass Upwork's verification; it provides the interactive session needed to
complete it legitimately.

The SQLite database uses the Docker-managed `scraper_data` volume because
SQLite WAL files are unreliable on Docker Desktop's Windows bind mounts.
Exports remain available directly in the host `output/` directory.

Run only the Freelancer and Guru platforms:

```powershell
docker compose run --rm scraper -p freelancer guru
```

Run those platforms once with a limit of 20 results per keyword:

```powershell
docker compose run --rm scraper -p freelancer guru --runs 1 -r 20
```

Run without Compose (Google Sheets is disabled unless its environment and
credential volume are supplied explicitly):

```powershell
docker build -t lead-generator .
docker run --rm --env-file .env -v "${PWD}/data:/app/data" -v "${PWD}/output:/app/output" --shm-size=2g lead-generator --runs 1
```

On Linux, ensure `data/` and `output/` are writable by container user UID
`10001`. To stop a continuous or multi-run session, use `Ctrl+C` or
`docker compose down`.

## Environment configuration

Create a `.env` file in the project root:

```dotenv
UPWORK_USERNAME=
UPWORK_PASSWORD=

BARK_USERNAME=
BARK_PASSWORD=

GOOGLE_SHEET_ID=
GOOGLE_CREDENTIALS_PATH=service-account.json
```

Google Sheets upload is disabled when `GOOGLE_SHEET_ID` is empty. Local
SQLite and CSV/JSON output still operate.

Do not commit `.env` or the Google service-account JSON file.

## Running

Run every configured platform and keyword:

```powershell
python main.py
```

Run selected platforms:

```powershell
python main.py -p upwork freelancer guru
```

Run selected keywords:

```powershell
python main.py -k "react developer" "shopify development"
```

Limit results per keyword:

```powershell
python main.py -r 20
```

Passing `-r` disables the adaptive daily policy for that run so the explicit
CLI value is respected.

Export JSON:

```powershell
python main.py --format json
```

Show detailed logs:

```powershell
python main.py -v
```

By default, one command starts a session of 10 runs. Each completed run is
followed by a 30-second wait. The first eligible run uses catch-up mode; after
that successful checkpoint, later session runs use normal mode.

Run only once:

```powershell
python main.py --runs 1
```

Keep running until manually stopped with `Ctrl+C`:

```powershell
python main.py --continuous
```

Change the delay or session limit:

```powershell
python main.py --runs 10 --interval 30
```

## Runtime configuration

The main structural settings are in `ScraperConfig`:

| Setting | Default | Meaning |
|---|---:|---|
| `max_platform_workers` | `4` | Maximum platform workers running concurrently |
| `max_browser_workers` | `2` | Maximum browser-capable searches running concurrently |
| `upwork_keyword_workers` | `2` | Isolated Selenium keyword/browser workers |
| `http_keyword_workers` | `3` | Keyword workers for Freelancer and Guru |
| `event_queue_size` | `100` | Maximum keyword-result batches awaiting processing |
| `output_queue_size` | `500` | Qualified leads awaiting the output worker |
| `upwork_location_timeout` | `12` | Seconds to wait for client country on an Upwork detail page |
| `max_results_per_keyword` | `50` | Result cap returned by each platform search |
| `google_sheet_tab` | `Leads` | Single worksheet receiving every platform |
| `sheets_batch_size` | `5` | Eligible leads per streaming Sheets upload |
| `sheets_min_lead_score` | `0` | Minimum score saved to Google Sheets |
| `sheets_min_write_interval` | `1.1` | Minimum seconds between Sheets write requests |
| `sheets_retry_attempts` | `5` | Attempts for quota and temporary API failures |
| `sheets_quota_cooldown` | `60` | Cooldown after a batch exhausts its retries |
| `database_path` | `data/leads.db` | SQLite checkpoint database |
| `output_dir` | `output` | Final CSV/JSON directory |
| `output_format` | `csv` | Local output format |

## Adaptive daily collection

Daily mode is enabled by default and uses the computer's local calendar date.
The default policy timezone is `Asia/Karachi`, and run state is stored in
`data/leads.db`. Set `SCRAPER_TIMEZONE` in `.env` to use another IANA timezone
such as `America/New_York`.

| Daily run | Jobs per keyword | Page/scroll limit | Posted-time behavior |
|---|---:|---:|---|
| First or catch-up run | Up to 1,000 | Up to 100 | Continue until reaching jobs older than 14 hours |
| Later normal runs | 20 | 3 | Keep only jobs posted within the last 2 hours |

A catch-up run is selected for the first run of the local day or whenever at
least 14 hours have elapsed since the last successfully completed run. Started
or aborted runs do not suppress catch-up mode. The window means “not older
than 14 hours.” The high result/page limits are safety ceilings: paginated
scrapers stop earlier when a newest-first page reaches the lookback boundary
or the platform has no more results.

Normal runs reject missing or unparseable posting dates so an unknown-age job
cannot bypass the 2-hour limit. Catch-up runs retain their existing behavior
of keeping unknown posting dates when a platform does not expose a usable age.

Platform mapping:

- Upwork Selenium and Bark treat the page limit as scroll/load batches.
- Freelancer and Guru request up to that many result pages.
- Upwork direct/Wayback attempts up to that many pages or snapshots.
- Vollna is an RSS feed, so it has no page concept; only the job limit applies.

Upwork, Vollna, Bark, and dated Guru records can be filtered using their
posting timestamps. Freelancer exposes a bid deadline such as `6 days left`,
not a reliable posting timestamp. Unparseable timestamps are retained from
newest-first results by default (`keep_unknown_posted_dates=True`) rather than
being incorrectly classified as old.

To disable adaptive mode:

```powershell
python main.py --no-adaptive-daily
```

To force and test a catch-up immediately, without changing database history:

```powershell
python main.py --catch-up
```

## Output behavior

Local output is written after the complete run:

```text
output/leads_YYYYMMDD_HHMMSS.csv
```

Qualified leads are also checkpointed immediately in SQLite:

```text
data/leads.db
```

When Sheets is enabled, leads are written to:

- A cumulative platform tab, such as `Upwork`.
- A dated platform tab, such as `Upwork 2026-07-29`.

Existing sheet titles are not appended again. Row colors represent priority:

- Green: high-priority lead
- Yellow: possible lead
- Red: low-priority lead

Sheets use a canonical header row, fixed column widths, fixed row heights, and
clipped long text. The first row is frozen and filters remain enabled, keeping
large descriptions and URLs from resizing the worksheet.

Every lead row also records:

- `Lead Found At`: UTC time when the scraper created the `JobLead`.
- `Sheet Saved At`: local time when that batch was sent to Google Sheets.
- `Found-to-Sheet Seconds`: elapsed seconds between those timestamps.

Existing sheets using the previous 23-column schema are migrated by inserting
these three columns after `Date Posted`, preserving the alignment of older
lead data.

All platforms are stored in one `Leads` worksheet and identified by the
`Job Platform` column. New five-lead batches are inserted directly below the
header while scraping is still running. The newest discovery timestamp is
placed first within each batch, while existing rows shift downward.

After the qualified lead summary, the CLI prints the complete run duration:

```text
Total scraper run time: 01:02:03 (3723.40 seconds)
```

This covers platform scraping, processing, SQLite checkpoints, Sheets flushes,
quota waits/retries, and the final local export.

## Lead scoring and Sheet eligibility

Important positive weights include:

- Company website: 25
- Company name: 15
- Business email: 20 (general email: 10)
- Decision-maker: 10
- Non-empty job description: 10
- Budget, matching service, long-term opportunity, and business clue: 10 each

Priority is based strictly on the final 0–100 score:

| Score | Priority | Google Sheets |
|---:|---|---|
| 0–29 | RED | Uploaded |
| 30–49 | RED | Uploaded |
| 50–69 | YELLOW | Uploaded |
| 70–100 | GREEN | Uploaded |

All scores are eligible for Sheets and remain available in SQLite and local CSV/JSON exports. Unique,
recency, and location filtering continue to run before this Sheets cutoff.

## Failure behavior

- A keyword failure is logged and its platform continues with the next keyword.
- A platform failure does not stop other platform workers.
- Qualified leads saved before a later failure remain in SQLite.
- Sheets writes are globally rate-limited to stay below the per-user quota.
- Quota and temporary API failures use bounded exponential backoff and honor
  Google's `Retry-After` header when present.
- A batch that exhausts its retries remains buffered, enters a 60-second
  cooldown, and is retried during a later or final flush.
- Browser and HTTP resources are closed when the CLI exits.

SQLite provides durable checkpoints, but automatic resume from a previous
run is not enabled yet. A future resume command can replay records whose
`uploaded` value is `0`.

## Tests

Run the offline structural tests:

```powershell
python -m unittest tests.test_structure -v
```

Run the analyzer sample suite:

```powershell
python test_qualification.py
```

The structural tests do not call external platforms or Google Sheets.
