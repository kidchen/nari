# Nari — personal automation hub

One Python container: a Telegram bot (long polling, no inbound ports) plus APScheduler cron jobs.

Who gets what is set per person: `profiles.<id>.features` in settings is what they're **allowed** (`all` or a list of `market`, `realestate`, `cards`, `placeholder`). Within that, each person can `/pause <feature>` and `/resume <feature>` from the bot, which only stops scheduled messages. `/features` shows your current state.

| Module | What | Schedule (America/Los_Angeles) | Recipients |
|---|---|---|---|
| A | Monthly parking **placeholder** image (watermarked, not a receipt) | 1st of month 08:00 | `placeholder` |
| B | Redfin alerts from Gmail. New listings, price changes and back-on-market homes each arrive as a **photo** with a link to the real listing; sold homes and Redfin's suggestions come as one text message. Mortgage rates from FRED arrive weekly | Gmail check every 2 hours, 7am–9pm (silent if nothing new); mortgage Thu 18:00 | `realestate` |
| C | Two briefs per person, from their own portfolio and watchlist. **Morning:** what's moving today, with *what happened / why it matters / what to watch* for each stock that has concrete news, plus the day's market stories. **Evening:** the close, what moved your stocks and why, the day's story, and a 2-week look-ahead (key US data with ⭐ for Fed, CPI, jobs, PCE and GDP; your earnings with before-open or after-close timing; IPOs) | Mon–Fri 08:30 and 16:30 (skips holidays) | `market` |
| D | Quarterly 5% categories (Discover, Chase Freedom), read automatically from the issuers' emails, with activation status and next-quarter previews | read during the 2-hourly Gmail check; plan on the 1st of each quarter; activation reminder on the 15th (only if not activated) | `cards` |

## What is per person vs shared

| Per person | Shared (public data or infrastructure) |
|---|---|
| Features allowed, plus your own pauses | Mortgage rates, prices, news, economic calendar, IPOs |
| Portfolio, watchlist, each brief, `/brief` cache | 5% category lists (the same for every cardholder) |
| Cards (each has an `owner`), `/setcats`, activation status, your own Activate links | Company names, AI model health |
| Real-estate delivery, plus optional `profiles.<id>.real_estate` filters (zips/cities, max price, saved-search names) | The bot's Gmail inbox and processed-email list; listing facts (a price change happened) |
| | Error alerts and `/status` (primary user only, as admin) |

**Admin-only features** (`ADMIN_FEATURES` in `app/access.py`, currently `placeholder`) exist only for the primary profile, even if another profile lists them or has `features: all`. Alongside `/status` and `/run`, they're the admin's tools.

Forwarded 5% emails are attributed to a person through `profiles.<id>.email_from`, the address they forward from. An email sent straight from a bank is attributed only when exactly one person holds that bank's card.

## Configuration: public defaults + private overrides

| File | In git? | Contents |
|---|---|---|
| `config/settings.yaml`, `config/cards.yaml` | yes | generic defaults and examples: schedules, feeds, models, example profiles, zips, cards |
| `config/settings.local.yaml`, `config/cards.local.yaml` | **no** | your real names, watchlist, zips, budget, cards and labels |
| `.env` | **no** | secrets: bot token, invite code, Gemini key, Gmail app password |
| `.deploy.env` | **no** | the deploy target (VM address, SSH key) |

Each `*.local.yaml` is deep-merged over its public file. Dicts merge key by key, and lists and values replace. `key: null` deletes a key, and a section with `_replace: true` replaces the whole section, which suits profiles, zips and cards. `deploy.sh` packages the files on disk rather than what's in git, so your private files still reach the server.

To start, create the local files from the examples:

```yaml
# config/settings.local.yaml
profiles: {_replace: true, alex: {name: Alex, primary: true}, sam: {name: Sam}}
real_estate: {max_price: 1500000, zips: {_replace: true, "94110": San Francisco}, recipients: [alex, sam]}
market: {seed_lists: {alex: {portfolio: [IBM], watch: [KO]}}, recipients: [alex, sam]}
placeholder: {recipients: [alex]}
```

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. `cp .env.example .env`, then fill in `TELEGRAM_BOT_TOKEN`, a long random `BOT_INVITE_CODE` (`openssl rand -hex 16`), and optionally `GEMINI_API_KEY` from Google AI Studio.
3. **Gmail for Redfin alerts**
   - Turn on 2-Step Verification for the Gmail account, then create an app password at <https://myaccount.google.com/apppasswords>. Put the address and password in `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD`.
   - On Redfin (signed in with that Gmail): search your target cities, then filter to **House** and your max price. Choose **Save search** and set email alerts to **Instant** or **Daily**.
   - The bot reads Redfin emails read-only and never marks them as read. Each email is processed once.
4. On the OCI VM:
   ```bash
   sudo chown 1000:1000 data      # container runs as uid 1000
   docker compose up -d --build
   docker compose logs -f
   ```
5. Each person messages the bot once with `/start <INVITE_CODE>` and taps their name. The bot deletes that message because it contains the code.
6. Primary user: `/run realestate`, `/run market`, `/run placeholder`, `/run cards` to test right away.

Config is mounted read-only, so after you edit it run `docker compose restart`.

## Deployment (OCI)

One command, adapted from zcare's `deploy.sh`:

```bash
./deploy.sh                # tests → package → scp → build on VM → recreate container → wait for heartbeat
./deploy.sh --push-env     # also replace the server's .env with your local one
./deploy.sh --package-only # just build/list the tarball, no SSH
```

- The target comes from the git-ignored `.deploy.env` (`NARI_REMOTE=opc@<vm-ip>`, optional `NARI_SSH_KEY=~/.ssh/<key>`).
- Each deploy unpacks into a fresh `~/nari`. The previous release is kept as `~/nari.prev` for rollback: `ssh … 'cd ~/nari.prev && docker compose up -d --build'`.
- The server's `.env` is kept unless you pass `--push-env`.
- Data (registrations, ticker lists, seen listings, card quarters) lives in the external Docker volume `nari-data`, so deploys never touch it.
- **Health check:** the bot updates `/tmp/nari-heartbeat` every 30s once Telegram is connected and the scheduler is running. Docker's HEALTHCHECK and the deploy script both check it. The deploy fails loudly if the bot isn't healthy within 90s.
- The script warns if a local bot is running, because two copies polling the same token make Telegram reject one of them.

```bash
ssh opc@<vm-ip> 'cd ~/nari && docker compose logs -f nari'
ssh opc@<vm-ip> 'docker run --rm -v nari-data:/d alpine cat /d/nari.db' > nari-backup.db
```

## Bot commands

`/help` lists the commands available to you, one per line, and Telegram's "/" menu shows the same ones.

| Command | What it does |
|---|---|
| `/features`, `/pause <f>`, `/resume <f>` | see your features; pause or resume scheduled messages |
| `/brief`, `/brief fresh` | your market brief now |
| `/portfolio`, `/watch` (`add` / `del` tickers) | your stock lists |
| `/cards` | this quarter's 5% categories, cap, activation status, and next quarter if announced |
| `/best dining` | which of your cards to use for a category |
| `/setcats <card_id> A, B` | correct a quarter's categories by hand (rarely needed) |
| `/status`, `/run <job>` | primary user only |

## Adding a card

Copy the example block from `config/cards.yaml` into `config/cards.local.yaml`, then set a new id, `name`, `owner` (a profile id from your settings) and `rates` (for example `{dining: 4, groceries: 4, other: 1}`). Leave out `rotating` for fixed-rate cards.

## Known limitations

- **Redfin** has no API, and it blocks scrapers with a bot check, so listings come from Redfin's own alert emails. The parser handles Redfin's "homecard" layout. If Redfin changes the template, Gemini extracts the same fields, and its output is checked before use. Listing links are Redfin's click-tracking links, which still open the listing.
- **Delivered per person.** Each alert is saved once as an event and delivery is recorded per person. A failed send to one person is retried on the next check for that person only, so nobody gets duplicates. Someone who registers or resumes `realestate` later gets the alerts from the last `catch_up_days` (3) that they haven't seen, never older than their registration.
- **Single-family check**: the saved search does the main filtering. Addresses that contain Apt, Unit or # are also dropped.
- **Mortgage rates** come from the weekly Freddie Mac survey, published on Thursdays. Monday's report repeats the previous Thursday's number.
- **yfinance** is unofficial and can break when Yahoo makes changes. Pin and upgrade it deliberately.
- **Brief quality depends on headlines.** News comes from Google News search (company name + "stock", last 30h) plus Yahoo per-ticker feeds, with clickbait filtered out. Gemini may only use those headlines and must cite them. Stocks whose only story is "moved with the market" are listed as Quiet, and the sector move appears once under Market stories. Your portfolio and watchlist live in the bot's database. `market.seed_lists` in `settings.local.yaml` only provides each person's starting lists.
- **Gemini free tier**: models are often overloaded (503) or out of quota (429). Each task has a preferred model list (`llm.brief_models`, `llm.extraction_models`). A **circuit breaker** stored in the DB remembers failures:
  - A model that fails is cooled down for 15 min, doubling with each further failure up to 6h. A 429 cools down for 2 min, and a 404 for 7 days.
  - While cooling down, the model is skipped, so calls go straight to a working one. If every model is cooling, each gets one probe as a last resort.
  - After the cooldown, the model gets one probe and is restored on success.
  - `/status` shows the current state.
  - `/brief` reuses a brief built in the last 30 min (`/brief fresh` forces a new one). Stocks with no fresh headlines aren't sent to Gemini. If every model fails, the digest falls back to plain headlines. Google Search grounding isn't available on the free tier. Free-tier prompts may be used by Google for training, but only public headlines and Redfin email text are sent.
- **5% categories come from issuer emails.** Discover and Chase email every cardholder before each quarter ("activate your 5%") and after activation. Forward those to the bot's Gmail, for example with an Outlook rule (*from* discover.com / chase.com, *subject contains* `5%`). The bot checks Inbox **and Spam** during the 2-hourly Gmail check, because forwarded bank mail often lands in Spam. Exact parsers read the categories, cap, dates, activation status and Chase's next-quarter preview. A Gemini fallback (with names, email addresses and account numbers removed) handles layout changes. Senders must be the banks or the `cards.email.forward_from` address. Chase sends an "activated" email, so its status updates on its own. Discover never sends one (`confirms_activation: false`), so its messages carry a **✅ I activated** button: one tap records it for the quarter and stops the mid-month reminder.

## Data storage & retention

All state lives in one SQLite file in the `nari-data` volume, currently under 100 KB. Prices, news, rates, photos and email bodies are always fetched fresh and never stored. A nightly cleanup (03:00) applies:

| Data | Kept |
|---|---|
| Processed Gmail IDs | 180 days (always longer than the 120-day look-back, checked at startup) |
| Seen listings | 180 days after last seen |
| Real-estate alerts and per-person deliveries | 30 days |
| 5% quarters, `/setcats` corrections, activations | previous, current and next quarter |
| Error-alert timestamps, AI model health, cached company names | 30 days |

VACUUM runs on Sundays. A **weekly backup** (Sun 03:30) goes into the separate `nari-backups` volume, keeping the newest 8. `deploy.sh` also copies the newest backup into the git-ignored `backups/` folder on your Mac. `/run cleanup` and `/run backup` run them on demand.

## VM health (admin only)

- **Alarms, every 10 min:** you get one message when a threshold is crossed and one when it recovers, with nothing in between. Thresholds (in `vm_health.thresholds`):
  - disk ≥ 85%, memory ≥ 90%, 15-min load ≥ 2.0 per core, swap ≥ 50%;
  - ≥ 3 bot restarts in 24h, newest backup older than 9 days;
  - the same job failing ≥ 3 times in 6h;
  - OCI instance not RUNNING.
- **`/health`:** shows everything on demand, plus OCI's last-hour CPU, memory and network.
- **Watchdog:** a dead bot can't report itself, so `deploy/watchdog.sh` runs on the VM every 5 minutes as a systemd timer that `deploy.sh` installs. It messages the admin directly through the Telegram API if the container is down or unhealthy for two checks in a row, and again when it's back. The token is passed to curl through stdin, never on the command line.

**OCI metrics setup (one time, no API key):** the bot authenticates as the VM itself (an *instance principal*), so no key is stored anywhere.
1. OCI Console → **Identity & Security → Domains → Default → Dynamic groups → Create**. Name it `nari-vm`, with the rule `Any {instance.id = '<your instance OCID>'}`. The OCID is on the instance's page.
2. **Identity & Security → Policies** (root compartment) → **Create policy**:
   ```
   Allow dynamic-group nari-vm to read metrics in tenancy
   Allow dynamic-group nari-vm to read instances in tenancy
   ```
3. Wait about a minute, then run `/health`. The OCI line changes from "not permitted" to state, CPU, memory and network.

## Gmail access

The bot uses IMAP over SSL to `imap.gmail.com` with an app password, not the Gmail REST API. Each scheduled check (every 2 hours, 7am–9pm, so 8 a day) is **one login**. Inside that login:
- It runs a Redfin search in Inbox and a "5%" search in Inbox and Spam.
- It fetches only the Message-ID headers of matching mail, then downloads bodies only for emails it hasn't processed before. A quiet check transfers a few hundred bytes.
- Mailboxes are opened read-only and bodies are read with `BODY.PEEK`, so nothing is ever marked read, moved or deleted.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q tests
```

## Sources

- [FRED MORTGAGE30US](https://fred.stlouisfed.org/series/MORTGAGE30US): ⚠️ External link, accessed 2026-10-13
- [Discover 5% cashback calendar](https://www.discover.com/credit-cards/cash-back/cashback-calendar.html): ⚠️ External link, accessed 2026-10-13
- [python-telegram-bot](https://docs.python-telegram-bot.org/): ⚠️ External link, accessed 2026-10-13
- [APScheduler 3.x CronTrigger](https://apscheduler.readthedocs.io/en/3.x/modules/triggers/cron.html): ⚠️ External link, accessed 2026-10-13
