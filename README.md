# Aachen Einbürgerungsurkunde Pickup Watcher

Watches the Aachen Ausländeramt booking system for **"Aushändigung
Einbürgerungsurkunde"** (naturalization certificate handover/collection)
appointments and pings you on Telegram when a slot opens up. Adapted from the
`abholung_termin()` pattern in
[noworneverev/aachen-termin-bot](https://github.com/noworneverev/aachen-termin-bot),
which watches a sibling Anliegen ("Abholung Aufenthaltserlaubnis") but not
this one.

## ⚠️ One thing to verify before trusting this

The booking flow has a step where you confirm a pickup location, and the
site expects a specific `select_location` text string alongside a hidden
location ID. I set `SELECT_LOCATION_TEXT` in `bot.py` to the Aachen Arkaden
address as a starting guess (since that's what most "Abholung" items use),
but **I couldn't confirm it's correct for Einbürgerungsurkunde specifically**
without actually stepping through a real booking session.

To verify/fix it:

1. Go to https://termine.staedteregion-aachen.de/auslaenderamt/select2?md=1
2. Open your browser's DevTools → **Network** tab, and set it to keep
   recording.
3. Manually click through: select **"Aushändigung Einbürgerungsurkunde"**
   under the "Abholung" section → proceed to the location step.
4. Find the `POST` request made when you confirm the location (URL will look
   like `.../auslaenderamt/location?mdt=89&select_cnc=1&cnc-XXX=1`).
5. Look at that request's form data for the `select_location` field's exact
   value, and paste it into `SELECT_LOCATION_TEXT` in `bot.py`.

Run `python bot.py` locally afterward and check the printed output makes
sense (either a clear "no appointment" message or a real date list — not an
error).

## Setup

1. **Create a Telegram bot** via [@BotFather](https://t.me/BotFather) → get
   a bot token.
2. **Get a chat ID** to notify — either message your bot directly and use
   [@userinfobot](https://t.me/userinfobot) to get your own chat ID, or
   create a channel/group, add your bot as admin, and use that chat's ID.
3. **Push this folder to a GitHub repo.**
4. In the repo, go to **Settings → Secrets and variables → Actions** and add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
5. Also under repo **Settings → Actions → General → Workflow permissions**,
   set to "Read and write permissions" (needed so the workflow can commit
   `state.json` back to the repo for dedup).
6. The workflow in `.github/workflows/check.yml` runs every 10 minutes via
   GitHub Actions cron — no server needed. You can adjust the schedule or
   trigger it manually from the Actions tab.

## Local testing

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=xxx
export TELEGRAM_CHAT_ID=xxx
python bot.py
```

## How it works

Same multi-step simulation as the original repo:
1. GET the "Auswahl des Anliegens" page, locate the Einbürgerungsurkunde
   entry (3rd item under "Abholung"), extract its `cnc-id`.
2. GET the location-selection page for that id, read the hidden `loc` value.
3. POST to confirm the location.
4. GET the suggestion page — if it doesn't say "Kein freier Termin
   verfügbar" (no free appointment available), parse out the actual dates.

`state.json` stores a hash of the last notification sent, so you don't get
pinged repeatedly for the same open slots — only when availability changes.

## Notes

- Be considerate with the polling interval — hitting a government site every
  minute is unnecessary and unfriendly; every 10–15 minutes is plenty for
  appointment watching.
- If the site's HTML structure changes, `find_cnc_url()` will fail loudly
  with a descriptive error rather than silently pointing at the wrong
  Anliegen.
