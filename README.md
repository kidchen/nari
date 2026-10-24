# Nari — personal automation hub

A Telegram bot for a household, run as one Docker container on a small VM. It sends each person only the features they've turned on, using their own lists, cards and topics.

| Feature | What you get | When |
|---|---|---|
| `market` | **Morning brief:** what's moving your portfolio and watchlist today, and why, with sources. **Evening brief:** the close, what moved, and a 2-week look-ahead (key US data ⭐, your earnings, IPOs) | Mon–Fri 08:30 and 16:30 PT; holidays skipped |
| `realestate` | Redfin saved-search alerts as photo messages linked to the real listing (new, price change, back on market); sold and suggested homes in one summary; weekly mortgage rates | Gmail checked every 2h, 7am–9pm, silent if nothing new; rates Thu 18:00 |
| `cards` | This quarter's 5% categories for your rotating cards, activation status, next quarter's preview, and reminders | Read from your forwarded bank emails; plan on the 1st of each quarter; reminder on the 15th if not activated |
| `expenses` | Card spending from statement PDFs (Chase, Discover, Bank of America, American Express): send a PDF, check the preview, tap Import. Each import must add up to the statement's own balances. Categories are learned (your fixes, common merchants, bank categories, then Gemini with names only). Reports include a category chart, changes vs earlier periods, top merchants, subscriptions (price increases flagged), a best-card check (quarterly/yearly) and short AI takeaways from category totals only. Between statements, bank transaction alert emails (BoA, Chase) add charges as they happen. Reports say when a card isn't covered yet | Weekly Sat 08:00 (the 7 days to Friday), monthly on the 1st, quarterly on Jan/Apr/Jul/Oct 1st, year-end on Jan 2 |
| `biorxiv` | New bioRxiv preprints matching your keywords and context terms, each summarized in 3 bullets (goal, method, finding). After 7 quiet days, a recap of the past 14 days: papers sent plus near misses (a keyword but no context term), or "nothing matched" | Your own cron, e.g. Mon–Fri 08:00 |
| `placeholder` *(admin)* | Monthly parking placeholder image (watermarked, not a receipt) | 1st of the month, 08:00 |
| `vmhealth` *(admin)* | `/health` on demand; alarms when disk, memory, load, backups or the OCI instance cross a threshold | Checked every 10 min |

Everything personal is kept per person: lists, cards, activation status, spending, merchant fixes, paper keywords, deliveries and pauses. Only public data is shared: prices, news, calendars, 5% category lists and mortgage rates.

---

## Using the bot

Register once by sending `/start <INVITE_CODE>`, then tap your name. `/help` and the "/" menu show only the commands for features you have **and** have set up.

| Command | What it does |
|---|---|
| `/features` | Your features, with a pause/resume button for each. Pausing stops scheduled messages; commands still work. |
| `/brief` | Market brief now. You get the morning version before the 1pm PT close and the evening version after. Add `morning`, `evening` or `fresh` (skips the 30-min cache). |
| `/portfolio`, `/watch` | Show your lists; `add IBM KO` or `del KO` to change them. Tickers are checked against Yahoo. |
| `/cards` | This quarter's 5% categories and activation status. `/cards dining` says which card to use. `/cards fix <card> A, B` is a rare manual correction. |
| `/spend` | Your card spending for the last 30 days by category and merchant (`/spend 90` for 90). `/spend report week\|month\|quarter\|year [now]` sends a report now. `/spend subs` lists detected subscriptions. `/spend fix costco` corrects a category, `/spend name costco = Costco` renames a merchant (both remembered). `/spend inbox` re-offers saved statements. Send a statement PDF to import it. |
| `/papers` | New papers now. `/papers topics` shows your keywords and context terms. `/papers add kw\|alt`, `/papers del 2`, `/papers context add …` and `/papers reset` edit them. |
| `/status` *(admin)* | App status: firing alarms (and those resolved in 24h), every job's last run with what it did and its next run, people and their features, AI model health |
| `/health` *(admin)* | VM and OCI health: disk, memory, load, uptime, restarts, backups |
| `/run <job>` *(admin)* | Run a job now: `mail realestate cardmail mortgage market evening cards activation spendmail spendremind spendcat spendweek spendmonth spendquarter spendyear papers alarms cleanup backup placeholder` |

Older commands (`/pause`, `/resume`, `/best`, `/setcats`, `/topics`) still work and reply with their new form.

---

## Setup

1. **Telegram:** create a bot with [@BotFather](https://t.me/BotFather).
2. **Secrets:** run `cp .env.example .env` and fill in `TELEGRAM_BOT_TOKEN`, `BOT_INVITE_CODE` (`openssl rand -hex 16`), `GEMINI_API_KEY` (Google AI Studio, free tier), and `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD`. The app password needs 2-Step Verification: <https://myaccount.google.com/apppasswords>.
3. **Private config:** create `config/settings.local.yaml` and `config/cards.local.yaml` (see [Configuration](#configuration)).
4. **Deploy target:** `echo 'NARI_REMOTE=opc@<vm-ip>' > .deploy.env`, plus `NARI_SSH_KEY=~/.ssh/<key>` if needed. The VM needs Docker with the Compose plugin.
5. **Deploy:** `./deploy.sh`.
6. **Feeds into the bot's Gmail:**
   - **Redfin:** save a search on Redfin while signed in with the bot's Gmail, with email alerts set to Instant or Daily.
   - **Card transaction alerts** (expenses): in each bank's app, turn on purchase alerts (e.g. BoA "transaction exceeds $0.01") sent to the bot's Gmail, or forwarded there (e.g. an Outlook rule: from `no.reply.alerts@chase.com`, subject contains "transaction with"). Supported: BoA, Chase.
   - **5% card emails:** in each person's own mailbox, add a rule that forwards mail from `discover.com` / `chase.com` with subject containing `5%` to the bot's Gmail. Forwards often land in Gmail Spam; the bot checks Spam too.
7. **OCI metrics** (optional; no API key needed): see [VM health](#vm-health-admin-only).
8. **Dev setup:** `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`, then `scripts/install-hooks.sh` to install the privacy pre-commit hook. Put your personal patterns (names, tickers, places, IDs) one regex per line in the git-ignored `.privacy-patterns`.

---

## Admin guide

### Configuration

| File | In git | Holds |
|---|---|---|
| `config/settings.yaml`, `config/cards.yaml` | yes | Generic defaults and examples |
| `config/settings.local.yaml`, `config/cards.local.yaml` | **no** | Real people, features, zips, cards, tickers, topics |
| `.env` / `.deploy.env` | **no** | Secrets / the VM address |

Each `*.local.yaml` is deep-merged over its public file: dicts merge, lists and values replace, `key: null` deletes, and `_replace: true` swaps a whole section. Edit config locally, then run `./deploy.sh`. The server's config is replaced on every deploy.

```yaml
# config/settings.local.yaml (example)
profiles:
  _replace: true
  alex:
    name: Alex
    primary: true                     # the admin: error alerts, /status, /run, admin-only features
    features: all
    email_from: [alex@example.com]    # forwarded 5% emails from here are Alex's
    placeholder: {label: Monthly parking, amount: "$100.00"}
  sam:
    name: Sam
    features: [realestate, biorxiv]
    real_estate: {zips: ["94110"]}    # optional: narrow your own alerts
    biorxiv:
      keywords: [zebrafish|danio, fin regeneration]    # order = priority; "|" = other spellings
      context: [zebrafish]                              # optional: a paper must also mention one of these
      schedule: "0 8 * * 1-5"
real_estate:
  zips: {_replace: true, "94110": San Francisco}
market:
  seed_lists: {alex: {portfolio: [IBM], watch: [KO]}}   # starting lists only; then use /portfolio, /watch
```

### Giving someone a feature

1. **Allow it:** add the feature to their `features:` list in `settings.local.yaml`. Admin-only features (`placeholder`, `vmhealth`) only ever go to the primary profile.
2. **Set it up:**
   - `market`: nothing needed. They add tickers with `/portfolio add …` and `/watch add …`, or you give them `market.seed_lists`.
   - `realestate`: nothing needed (optionally `profiles.<id>.real_estate` filters).
   - `cards`: add their cards to `cards.local.yaml` (see [Adding a card](#adding-a-card)), and set `email_from:` on their profile to the address they forward from.
   - `biorxiv`: add a `biorxiv:` block to their profile (keywords, optional context, schedule).
   - `expenses`: nothing needed. Adding their cards with `last4` (see [Adding a card](#adding-a-card)) gives card names in previews and enables the best-card check.
3. **They do their part:** for `cards`, forward the bank's 5% emails to the bot's Gmail; for `realestate`, saved searches email the bot's Gmail; for `expenses`, send each statement PDF to the bot.
4. **Deploy:** run `./deploy.sh`. Their "/" menu updates on restart.
5. **Check:** run `/status` (People shows their features). They can send `/help` to confirm.

### Adding a person

Add a profile (id, `name`, `features`) to `settings.local.yaml`, deploy, and send them the invite code. They send `/start <code>` and tap their name.

### Adding a card

Add a block to `config/cards.local.yaml`:

```yaml
cards:
  _replace: true
  my_discover:
    name: Discover it Cash Back
    owner: alex                       # profile id
    last4: ["1234"]                   # card ending(s) on statements (Amex: 5 digits); used by expenses
    unit: "%"                         # or "x" for points (compared 1x ≈ 1%)
    rates: {other: 1}                 # fixed categories, e.g. {dining: 3, grocery: 2, online shopping: 3, other: 1}
    rotating:                         # omit for non-rotating cards
      issuer: discover                # discover | chase — which bank's 5% emails apply
      rate: 5
      cap_per_quarter: 1500
      requires_activation: true
      confirms_activation: false      # Discover never emails a confirmation → "✅ I activated" button
```

### Day-to-day operations

```bash
./deploy.sh                  # tests → package → upload → build on VM → recreate → wait for heartbeat
./deploy.sh --push-env       # also replace the server's .env
ssh opc@<vm-ip> 'cd ~/nari && docker compose logs --tail 100 nari'
ssh opc@<vm-ip> 'cd ~/nari.prev && docker compose up -d --build'     # roll back to the previous release
```

- **Deploys** unpack into a fresh `~/nari` and keep the previous release as `~/nari.prev`. The server's `.env` and all data (in the `nari-data` volume) are kept.
- **Disk:** container logs are capped (10 MB × 3) and app data is pruned nightly. Each deploy removes old images and trims Docker's build cache to 1.5 GB, which otherwise grows with every deploy, then prints disk usage.
- **Backups** run weekly into the `nari-backups` volume (newest 8 kept), encrypted, and each deploy copies the newest one to `backups/` on your machine. See [Backups](#backups-encrypted) to restore.
- **Run only one copy of the bot.** Two copies polling the same token make Telegram reject one ("Conflict"). `deploy.sh` warns if a local copy is running.

---

## How it works (short)

- **Market:** prices, volume and earnings dates come from Yahoo (yfinance); headlines from Google News and Yahoo, with clickbait filtered out. Gemini writes "what happened / why it matters / what to watch" only from those headlines, and an item is kept only if a cited headline names the company. Name aliases (`market.aliases`) help, e.g. Google for Alphabet. The economic calendar is queried one day at a time.
- **Real estate:** Redfin has no API and blocks scrapers, so the bot reads Redfin's alert emails. Exact parsers handle both email layouts, with Gemini as a fallback. Tracking links are resolved to the real listing URL. Each alert is stored once and delivered **per person**: a failed send is retried for that person only, and someone joining later catches up on the last 3 days.
- **Cards:** 5% categories are public and stored once per bank. Activation status and Activate links are per person, matched through `email_from`. A bank email sent directly, not forwarded, is attributed only when exactly one person holds that bank's card.
- **bioRxiv:** uses the JSON API (not RSS, which only holds the latest 30 papers). Keeps new preprints (not revisions) that match a keyword and, if set, a context term. Ranked by keyword order; up to 12 are summarized and further matches are listed. Deduped per person.
- **Gemini (free tier):** each task has an ordered model list (`llm.*_models`). A circuit breaker stored in the database skips a failing model: 503 errors cool it down for 15 min doubling up to 6h, 429 for 2 min, 404 for 7 days. After the cooldown it's probed and restored on success. If every model fails, you get plain headlines or abstracts instead.
- **Gmail:** IMAP read-only with an app password, one login per 2-hourly check. Only Message-ID headers are fetched first; bodies are downloaded only for unseen mail. Nothing is ever marked read or moved.
- **Expenses:** alert emails (one parser per bank, `app/features/expenses/alerts.py`) are read in the 2-hourly Gmail check and stored as charges for the card's owner (by `last4`, else the forwarder's `email_from`). Alerts are the main source; statements are optional corrections (tips, refunds, fees) that replace the alerts for their card and period whenever one is imported. Three months after a person's last import, one reminder lists their cards in use with older statements; [Skip 3 months] or any import resets it. Statement PDFs are parsed on the server from positioned words (pdfplumber), one layout per bank. An import is accepted only if previous balance + every parsed line = new balance, to the cent. The PDF is deleted after Import / Cancel; one that can't be parsed is kept 30 days. Merchant categories come from your fixes, a starter list of common US chains, the shared learned map, the bank's own category (Discover), then Gemini (merchant names only), and answers are remembered. Subscriptions are the same merchant with a steady amount, monthly or yearly. The best-card check compares each purchase's card with your best card for it (base rates + the quarter's 5% categories). Gemini sees only category totals for report takeaways.
- **Storage:** one SQLite file. Retention: Gmail IDs 180 days (always longer than the look-back), listings 180 days, alerts 30 days, sent papers 90 days, card quarters for the previous/current/next quarter, job runs and small caches 30 days, unimported statement PDFs 30 days. Spending data and merchant fixes are kept (reports compare with earlier periods and years). A nightly cleanup runs at 03:00, with VACUUM on Sundays.

## Monitoring and alarms (admin only)

Every job run (scheduled or `/run`) is recorded with what it did, e.g. "new mail: realestate 2, cards 0 · 3 message(s) sent", and any error, even one the job caught. `/status` shows the latest run of each job. Every 10 minutes the `alarms` job checks:

- **Job failed:** the last run had an error. 🟡 at first, 🔴 after 3 failed runs in a row.
- **Missed run:** a scheduled time passed 30 minutes ago and the job never started (e.g. the scheduler silently dropped it).
- **VM** (thresholds in `vm_health.thresholds`): 🟡 disk ≥ 70%, 🔴 disk ≥ 85%, memory ≥ 90%, ≥ 3 crash restarts in 24h (deploys don't count), newest backup older than 9 days, OCI instance not running; 🟡 15-min load ≥ 2/core, swap ≥ 50%.

The primary profile gets one message when an alarm fires (🚨/⚠️), escalates or recovers (✅), at any hour, plus one "still firing" reminder a day. Settings are under `monitoring:`. A failed job alarms right when the run ends rather than at the next check. Errors outside jobs (e.g. in a command) still send a direct message, at most once per 6h.

## VM health (admin only)

- **`/health`** shows disk, memory, load, uptime, restarts, data and backup sizes, and OCI metrics. The thresholds above raise alarms.
- **Watchdog:** a systemd timer on the VM, installed by `deploy.sh`, checks the container every 5 min. It messages the admin through the Telegram API if the bot is down for two checks in a row, and again when it recovers.
- **OCI metrics, one-time setup (instance principal, no key stored):**
  1. Console → Identity & Security → Domains → Default → **Dynamic groups** → create `nari-vm` with the rule `Any {instance.id = '<instance OCID>'}`.
  2. **Policies** (root compartment):
     ```
     Allow dynamic-group nari-vm to read metrics in tenancy
     Allow dynamic-group nari-vm to read instances in tenancy
     ```
  3. `/health` should show OCI state, CPU, memory and network within a minute.

## Server hardening (one-time, done by hand on the VM)

SSH stays on the public port, key-only:
- `/etc/ssh/sshd_config.d/10-nari-hardening.conf`: no root, only `opc`, no passwords, 3 tries.
- fail2ban (Oracle EPEL) bans an IP for 1h after 5 failed logins, longer for repeat offenders.
- `dnf-automatic` applies security updates daily, with no automatic reboot.
- rpcbind is masked.

Tailscale (exit node) is untouched. Keep the OCI rule for UDP 41641 so phones in other countries can connect directly.

## Backups (encrypted)

The weekly backups in the `nari-backups` volume, and the copy `deploy.sh` saves to `backups/`, are encrypted with [age](https://age-encryption.org).
- The server has only the public key (`maintenance.backup_age_recipient` in `settings.local.yaml`), so it can write backups but not read them.
- The private key is `~/.config/nari/backup-age-key.txt`, with a copy in Apple Passwords (or another safe place off this machine). **Without it the backups can't be restored.**

Restore (stops the bot, replaces its database):
```bash
age -d -i ~/.config/nari/backup-age-key.txt -o nari.db backups/nari-YYYYMMDD.db.age
sqlite3 nari.db "PRAGMA integrity_check"                     # must print: ok
scp nari.db opc@<vm-ip>:/tmp/nari.db
ssh opc@<vm-ip> 'cd ~/nari && docker compose stop nari && docker run --rm -v nari-data:/d -v /tmp/nari.db:/n.db:ro \
  --entrypoint sh nari:latest -c "cp /n.db /d/nari.db" && docker compose start nari && rm /tmp/nari.db'
rm nari.db
```

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Bot doesn't reply | Check `docker compose logs`. "Conflict" means another copy is polling, so stop it. |
| Briefs say "AI summary unavailable" | All Gemini models overloaded (503) or out of quota (429). `/status` shows each model's cooldown; it recovers on its own. |
| A Redfin email produced nothing | Account and passcode emails are skipped by design. For a new layout, check the logs for "trying LLM extraction". |
| 5% categories missing | The forwarded email may be in Gmail Spam, which is fine since Spam is checked. Make sure the forwarding rule matches "5%". `/run cardmail` checks now. |
| Activation status not updating | The sender isn't linked to a person: set `email_from` on their profile. Discover never confirms, so tap ✅ instead. |
| `/health` says OCI "not permitted" | Dynamic group or policy missing (see above). |
| A statement says "Couldn't import" | "Unknown bank": only Chase, Discover, BoA and Amex layouts exist. "Doesn't add up": a line wasn't parsed (new layout?). The PDF stays on the server for 30 days so the parser can be fixed; then `/spend inbox` offers it again. |
| Spending shows "Uncategorized" | Gemini was unavailable; it's retried daily at 04:15 and on `/spend`. Or fix it yourself: `/spend fix <merchant>`. |
| A report says "⚠️ No alerts from …" | A card that usually sends alerts has been quiet for 30+ days: check that card's alert setting in the bank's app. |
| "Not counted after their last statement" | Those cards have no alerts, so spending after their last statement is unknown. Turn on alerts, or send statements now and then. |

## Project layout

```
app/
  main.py          Telegram wiring: registration, /help, /features, /status, /run (feature-agnostic)
  monitoring.py    job run ledger, missed-run / job / feature alarms, alarm messages, /status sections
  services.py      shared services for features: config, DB, notifier, access, Gemini, errors, Gmail check
  registry.py      Feature / Command / Job / MailQuery types
  access.py        who may use which feature (allowed + paused)
  config.py db.py notify.py maintenance.py
  integrations/    gmail (IMAP), llm (Gemini + circuit breaker)
  features/        one package per feature: feature.py (FEATURE: commands, jobs, mail, ...) + its logic
    market/  realestate/  cards/  expenses/  biorxiv/  placeholder/  vmhealth/
    __init__.py    ALL = [...]  ← the list of enabled features
config/            public defaults (+ your git-ignored *.local.yaml)
deploy.sh deploy/  deploy script, VM watchdog
scripts/           privacy_check.py (pre-commit), install-hooks.sh
tests/             one test file per feature, plus helpers.py
```

## Writing a new feature

1. Create `app/features/<name>/` containing your logic and a `feature.py` that defines `FEATURE = Feature(...)` (see `app/registry.py`):
   - `commands`: each one's name, a one-line description, `handler(bot, update, ctx)` and usage lines;
   - `jobs`: an id (also its `/run` name), `run(services, force=False)`, and a schedule read from settings;
   - optional: `mail` (a Gmail search that shares the 2-hourly login), `profile_jobs` (per-person schedules), `ready(services, pid)` ("is it set up for this person?"), `on_startup`, `status_section`, `alarms` (async, returns `monitoring.Alarm`s checked every 10 min), `documents` (files sent to the bot, by MIME type), `aliases`, `callbacks`, `admin_only`.
2. Add it to `ALL` in `app/features/__init__.py`.
3. Add its defaults to `config/settings.yaml`, then grant it to people in `features:`.

That's all. `/help`, the "/" menu, `/features`, `/run`, `/status`, the schedule and the shared Gmail check pick it up automatically (`tests/test_registry.py` checks this). Use `services.llm(...)`, `services.report_error(...)` and `services.send_isolated(...)` rather than writing your own. Every job is recorded automatically; call `monitoring.note("…")` to say what it did.

## Tests

```bash
.venv/bin/ruff check . && .venv/bin/python -m pytest -q   # deploy.sh runs both; GitHub CI runs them on every push
```

## Sources

- [bioRxiv API](https://api.biorxiv.org/): ⚠️ External link, accessed 2026-10-15
- [age encryption](https://age-encryption.org): ⚠️ External link, accessed 2026-10-18
- [pdfplumber](https://github.com/jsvine/pdfplumber): ⚠️ External link, accessed 2026-10-18
- Ideas for expenses (rules, subscriptions, duplicate matching, category chart), from [Actual Budget](https://github.com/actualbudget/actual) and its [API](https://actualbudget.org/docs/api/) and [bank sync](https://actualbudget.org/docs/advanced/bank-sync/) docs, [Firefly III](https://github.com/firefly-iii/firefly-iii), and [ExpenseOwl](https://github.com/Tanq16/ExpenseOwl): ⚠️ External links, accessed 2026-10-18
- [FRED MORTGAGE30US](https://fred.stlouisfed.org/series/MORTGAGE30US): ⚠️ External link, accessed 2026-10-13
- [python-telegram-bot](https://docs.python-telegram-bot.org/): ⚠️ External link, accessed 2026-10-13
- [APScheduler 3.x CronTrigger](https://apscheduler.readthedocs.io/en/3.x/modules/triggers/cron.html): ⚠️ External link, accessed 2026-10-13
- [OCI instance principals](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/callingservicesfrominstances.htm): ⚠️ External link, accessed 2026-10-14
