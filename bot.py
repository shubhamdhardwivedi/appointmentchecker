"""
Aachen Ausländeramt appointment watcher for:
"Aushändigung Einbürgerungsurkunde" (naturalization certificate collection)

Adapted from the pattern used in noworneverev/aachen-termin-bot's
termin.py -> abholung_termin(). Same multi-step booking-flow simulation,
pointed at a different "Anliegen" (concern) that the original bot doesn't watch.

Flow:
  1. GET the "Auswahl des Anliegens" page, find the cnc-id for our Anliegen
  2. GET the location page for that cnc-id, read the hidden `loc` value
  3. POST the location confirmation (this is the step that needs verifying,
     see SELECT_LOCATION_TEXT below)
  4. GET the suggestion page and parse whether slots exist
"""

import hashlib
import json
import logging
import os
from pathlib import Path

import bs4
import requests

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
)

STEP1_URL = "https://termine.staedteregion-aachen.de/auslaenderamt/select2?md=1"
SUGGEST_URL = "https://termine.staedteregion-aachen.de/auslaenderamt/suggest"
LOCATION_URL_BASE = "https://termine.staedteregion-aachen.de/auslaenderamt/location?mdt=89&select_cnc=1"

# --- CONFIG you should verify once before relying on this -------------------
#
# Order of items under the "Abholung" heading on the site (0-indexed), as of
# writing:
#   0 = Abholung Aufenthaltserlaubnis        (already covered by the original bot)
#   1 = Abholung Reiseausweis
#   2 = Aushändigung Einbürgerungsurkunde    <-- this is the one we want
#   3 = Abholung Verpflichtungserklärung/Einladung (vorheriger Onlineantrag)
#
# If the site reorders this section, this index will silently point at the
# wrong Anliegen, so worth a periodic sanity check.
ANLIEGEN_SECTION = "Abholung"
ANLIEGEN_POSITION = 2

# The site expects a human-readable "select_location" string alongside the
# hidden `loc` id when you confirm the pickup location. The existing bot's
# values (e.g. "Ausländeramt Aachen - Aachen Arkaden, Trierer Straße 1, Aachen
# auswählen") were almost certainly captured by watching the real POST
# request in browser DevTools (Network tab) while manually clicking through
# the booking flow for that specific Anliegen. Do that once for
# Einbürgerungsurkunde: go to the site, pick "Aushändigung
# Einbürgerungsurkunde", get to the "choose location" step, open DevTools ->
# Network, click the location option, and find the POST request to a `/location...`
# URL. Copy the exact `select_location` form field value from that request
# into the line below. Left as the Aachen Arkaden value as a starting guess
# since it's the address currently listed for most "Abholung" pickups — but
# confirm it, don't trust it.
SELECT_LOCATION_TEXT = "Ausländeramt Aachen - Aachen Arkaden, Trierer Straße 1, Aachen auswählen"

STATE_FILE = Path(__file__).parent / "state.json"
# -----------------------------------------------------------------------------


def find_cnc_url(soup: bs4.BeautifulSoup, section_heading: str, position: int):
    """Find the booking URL for a specific Anliegen listed under a section heading."""
    header = soup.find("h3", string=lambda s: section_heading in s if s else False)
    if not header:
        return False, f"Section heading '{section_heading}' not found on page — site may have changed."

    sibling = header.find_next_sibling()
    if not sibling:
        return False, f"No content block found under heading '{section_heading}'."

    li_elements = sibling.find_all("li")
    if position >= len(li_elements):
        return False, (
            f"Only {len(li_elements)} items found under '{section_heading}', "
            f"position {position} is out of range — site may have reordered items."
        )

    cnc_id = li_elements[position].get("id").split("-")[-1]
    logging.info(f"{section_heading} item #{position} -> cnc id: {cnc_id}")
    return True, f"{LOCATION_URL_BASE}&cnc-{cnc_id}=1"


def check_einbuergerungsurkunde():
    """Check for available 'Aushändigung Einbürgerungsurkunde' pickup appointments."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    res1 = session.get(STEP1_URL)
    soup = bs4.BeautifulSoup(res1.content, "html.parser")

    success, url_2_or_error = find_cnc_url(soup, ANLIEGEN_SECTION, ANLIEGEN_POSITION)
    if not success:
        logging.error(url_2_or_error)
        return False, url_2_or_error

    url_2 = url_2_or_error
    res2 = session.get(url_2)
    soup = bs4.BeautifulSoup(res2.content, "html.parser")

    loc_input = soup.find("input", {"name": "loc"})
    if not loc_input:
        return False, "Could not find the location id field — site structure may have changed."
    loc = loc_input.get("value")
    logging.info(f"Einbürgerungsurkunde loc id: {loc}")

    payload = {
        "loc": str(loc),
        "gps_lat": "50.7753",
        "gps_long": "6.0839",
        "select_location": SELECT_LOCATION_TEXT,
    }
    session.post(url_2, data=payload)
    res4 = session.get(SUGGEST_URL)

    if "Kein freier Termin verfügbar" in res4.text:
        msg = "No appointment currently available for Einbürgerungsurkunde pickup."
        logging.info(msg)
        return False, msg

    soup = bs4.BeautifulSoup(res4.text, "html.parser")
    div = soup.find("div", {"id": "sugg_accordion"})
    summary_tag = soup.find("summary", id="suggest_details_summary")

    if div:
        dates = [h.text for h in div.find_all("h3")]
        message = "New Einbürgerungsurkunde pickup appointments available:\n" + "\n".join(dates)
        logging.info(message)
        return True, message
    elif summary_tag:
        message = "Einbürgerungsurkunde pickup appointment available now:\n" + summary_tag.get_text(strip=True)
        logging.info(message)
        return True, message
    else:
        message = "Slots may be available, but the page structure wasn't recognized — check manually."
        logging.warning(message)
        return False, message


def send_telegram_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logging.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping notification.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text})
    if resp.status_code != 200:
        logging.error(f"Telegram send failed: {resp.status_code} {resp.text}")
        return False
    logging.info("Telegram notification sent.")
    return True


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def main():
    available, message = check_einbuergerungsurkunde()
    print(message)

    if not available:
        return

    # Dedup: only notify if the message content actually changed since last run,
    # so we don't spam the same "slots available" alert every cron cycle.
    state = load_state()
    message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()

    if state.get("last_message_hash") == message_hash:
        logging.info("Same availability as last check — not re-notifying.")
        return

    sent = send_telegram_message(message)
    if sent:
        state["last_message_hash"] = message_hash
        save_state(state)
    else:
        logging.warning("Notification not sent — will retry next run instead of marking as notified.")


if __name__ == "__main__":
    main()
