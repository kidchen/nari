# Nari — personal automation hub

One Python container: a Telegram bot (long polling, no inbound ports) plus APScheduler cron jobs.

| Module | What | Schedule (America/Los_Angeles) | Recipients |
|---|---|---|---|
| A | Monthly parking **placeholder** image (watermarked, not a receipt) | 1st of month 08:00 | primary user |
| B | Mortgage rates (FRED/Freddie Mac) + new listings / price changes / nearby sales from Redfin alert emails (Gmail) | Mon + Thu 18:00 | both |
| C | Market close digest: watchlist, gainers/losers, volume spikes, headlines, Gemini catalysts | Mon–Fri 13:30 (skips holidays) | both |
| D | Quarterly card plan (rotating categories, activation reminder); offer expiry reminders | Jan/Apr/Jul/Oct 1 09:00; daily 09:05 | per user |

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
market: {watchlist: [SPY, QQQ, AAPL], recipients: [alex, sam]}
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
- Data (registrations, offers, seen listings) lives in the external Docker volume `nari-data`, so deploys never touch it.
- **Health check:** the bot updates `/tmp/nari-heartbeat` every 30s once Telegram is connected and the scheduler is running. Docker's HEALTHCHECK and the deploy script both check it. The deploy fails loudly if the bot isn't healthy within 90s.
- The script warns if a local bot is running, because two copies polling the same token make Telegram reject one of them.

```bash
ssh opc@<vm-ip> 'cd ~/nari && docker compose logs -f nari'
ssh opc@<vm-ip> 'docker run --rm -v nari-data:/d alpine cat /d/nari.db' > nari-backup.db
```

## Bot commands

```
/cards                      your cards + this quarter's categories
/best groceries             rank your cards for a category (+ matching offers)
/offers                     list active offers
/offer add discover_it | Dell | 10% back up to $50 | 2026-11-30
/offer done 3
/setcats discover_it Amazon.com, Target   set this quarter's rotating categories
/run <job>                  primary user only
```

## Adding a card

Copy the example block from `config/cards.yaml` into `config/cards.local.yaml`, then set a new id, `name`, `owner` (a profile id from your settings) and `rates` (for example `{dining: 4, groceries: 4, other: 1}`). Leave out `rotating` for fixed-rate cards.

## Known limitations

- **Redfin** has no API, and it blocks scrapers with a bot check, so listings come from Redfin's own alert emails. The parser handles Redfin's "homecard" layout. If Redfin changes the template, Gemini extracts the same fields, and its output is checked before use. Listing links are Redfin's click-tracking links, which still open the listing.
- **Single-family check**: the saved search does the main filtering. Addresses that contain Apt, Unit or # are also dropped.
- **Mortgage rates** come from the weekly Freddie Mac survey, published on Thursdays. Monday's report repeats the previous Thursday's number.
- **yfinance** is unofficial and can break when Yahoo makes changes. Pin and upgrade it deliberately.
- **Gemini free tier**: models are often overloaded (503) or out of quota (429). `llm.models` in settings is a list tried in order. If every model fails, the digest falls back to plain headlines. Google Search grounding isn't available on the free tier. Free-tier prompts may be used by Google for training, but only public headlines and Redfin email text are sent.
- **Discover categories** can't be fetched automatically: the live page sits behind bot protection, and the public JSON file is out of date. Set them each quarter with `/setcats`; the quarterly message reminds you.

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
