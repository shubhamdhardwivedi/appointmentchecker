"""
Aachen Ausländeramt appointment watcher for:
"Aushändigung Einbürgerungsurkunde" (naturalization certificate collection)

Adapted from the pattern used in noworneverev/aachen-termin-bot's
termin.py -> abholung_termin(). Same multi-step booking-flow simulation,
pointed at a different "Anliegen" (concern) that the original bot doesn't watch.

Sends one separate Telegram message per available (date, time) slot that
falls inside your configured date window, and only re-notifies about a slot
if it disappears and later comes back (e.g. someone books it, then cancels).
"""

import hashlib
import json
import logging
import os
import re
import time
from datetime import date
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

# ============================= CONFIG — edit these yourself ==================

ANLIEGEN_SECTION = "Abholung"
ANLIEGEN_POSITION = 2  # Aushändigung Einbürgerungsurkunde

SELECT_LOCATION_TEXT = "Ausländeramt Aachen - Aachen Arkaden, Trierer Straße 1, Aachen auswählen"


def ddmmyyyy(day: int, month: int, year: int) -> date:
    """Lets you write dates as day, month, year below instead of Python's
    usual year-first order."""
    return date(year, month, day)


# Only notify about appointments inside this date window (both dates included).
# Format is ddmmyyyy(DAY, MONTH, YEAR). Example: to only hear about
# appointments between 8th August 2026 and 15th September 2026:
#   TARGET_WINDOW_START = ddmmyyyy(8, 8, 2026)
#   TARGET_WINDOW_END   = ddmmyyyy(15, 9, 2026)
TARGET_WINDOW_START = ddmmyyyy(8, 8, 2026)    # <-- EDIT ME (day, month, year)
TARGET_WINDOW_END = ddmmyyyy(9, 9, 2026)     # <-- EDIT ME (day, month, year)

# If the automatic time-slot detection below ever seems wrong (reports a time
# that's actually greyed-out on the real site, or misses one that IS bookable),
# set this to True, re-run the workflow once manually, then copy the whole
# printed HTML block from the Actions log and send it to me — I'll correct the
# parsing logic precisely from that instead of guessing.
DEBUG_DUMP_HTML = False

STATE_FILE = Path(__file__).parent / "state.json"

# ===============================================================================

DATE_PATTERN = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
TIME_PATTERN = re.compile(r"^\d{1,2}:\d{2}$")


def parse_date_label(label: str):
    """Extract a date object from a label like 'Dienstag, 08.09.2026'."""
    match = DATE_PATTERN.search(label)
    if not match:
        return None
    day, month, year = match.groups()
    return date(int(year), int(month), int(day))


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


def extract_times_for_date_block(content) -> list:
    """
    Given the HTML block that follows a date heading in the suggestion
    accordion, return the list of bookable (non-greyed-out) time strings
    found in it, e.g. ["13:40", "14:40"].

    Best-effort heuristic: treats a time as available if its element isn't
    marked disabled/inactive/muted and is a real clickable link/button.
    See DEBUG_DUMP_HTML above if this needs correcting.
    """
    available = []
    if content is None:
        return available

    for text_node in content.find_all(string=TIME_PATTERN):
        time_text = text_node.strip()
        el = text_node.parent

        classes = " ".join(el.get("class", [])).lower()
        is_disabled = (
            el.has_attr("disabled")
            or "disabled" in classes
            or "inactive" in classes
            or "unavailable" in classes
            or "muted" in classes
            or "not-available" in classes
        )

        href = el.get("href", "")
        is_real_link = el.name == "a" and href and href not in ("#", "javascript:void(0)", "")
        is_clickable_tag = el.name in ("a", "button")

        if not is_disabled and (is_real_link or is_clickable_tag):
            available.append(time_text)

    return available


def check_einbuergerungsurkunde():
    """
    Check for available 'Aushändigung Einbürgerungsurkunde' pickup slots.
    Returns a list of dicts: [{"date_label": ..., "date_iso": ..., "time": ... or None}, ...]
    already filtered to TARGET_WINDOW_START..TARGET_WINDOW_END.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    res1 = session.get(STEP1_URL)
    soup = bs4.BeautifulSoup(res1.content, "html.parser")

    success, url_2_or_error = find_cnc_url(soup, ANLIEGEN_SECTION, ANLIEGEN_POSITION)
    if not success:
        logging.error(url_2_or_error)
        return []

    url_2 = url_2_or_error
    res2 = session.get(url_2)
    soup = bs4.BeautifulSoup(res2.content, "html.parser")

    loc_input = soup.find("input", {"name": "loc"})
    if not loc_input:
        logging.error("Could not find the location id field — site structure may have changed.")
        return []
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

    if DEBUG_DUMP_HTML:
        logging.info("----- DEBUG: raw suggestion page HTML below -----")
        logging.info(res4.text)
        logging.info("----- DEBUG: end of raw HTML -----")

    if "Kein freier Termin verfügbar" in res4.text:
        logging.info("No appointment currently available for Einbürgerungsurkunde pickup.")
        return []

    soup = bs4.BeautifulSoup(res4.text, "html.parser")
    div = soup.find("div", {"id": "sugg_accordion"})
    summary_tag = soup.find("summary", id="suggest_details_summary")

    slots = []

    if div:
        for h3 in div.find_all("h3"):
            date_label = h3.get_text(strip=True)
            date_iso = parse_date_label(date_label)
            if date_iso is None:
                continue
            content = h3.find_next_sibling()
            times = extract_times_for_date_block(content)
            if times:
                for t in times:
                    slots.append({"date_label": date_label, "date_iso": date_iso, "time": t})
            else:
                # Couldn't confidently detect individual times — still report the
                # date so you don't miss it, just without a specific time attached.
                slots.append({"date_label": date_label, "date_iso": date_iso, "time": None})
    elif summary_tag:
        text = summary_tag.get_text(strip=True)
        date_iso = parse_date_label(text)
        time_match = re.search(r"\d{1,2}:\d{2}", text)
        slots.append({
            "date_label": text,
            "date_iso": date_iso if date_iso else date.today(),
            "time": time_match.group() if time_match else None,
        })
    else:
        logging.warning("Slots may be available, but the page structure wasn't recognized — check manually.")
        return []

    filtered = [
        s for s in slots
        if s["date_iso"] is not None and TARGET_WINDOW_START <= s["date_iso"] <= TARGET_WINDOW_END
    ]

    if not filtered:
        logging.info(
            f"Appointments exist but none fall within your window "
            f"({TARGET_WINDOW_START} to {TARGET_WINDOW_END})."
        )
    return filtered


def slot_key(slot: dict) -> str:
    return f"{slot['date_iso'].isoformat()}|{slot['time'] or 'unspecified'}"


def format_slot_message(slot: dict) -> str:
    if slot["time"]:
        return (
            f"New Einbürgerungsurkunde pickup slot available:\n"
            f"{slot['date_label']} at {slot['time']}\n\n"
            f"Book here: {STEP1_URL}"
        )
    return (
        f"New Einbürgerungsurkunde pickup date available (exact time not detected):\n"
        f"{slot['date_label']}\n\n"
        f"Book here: {STEP1_URL}"
    )


def get_chat_ids() -> list:
    raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def send_telegram_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids = get_chat_ids()
    if not token or not chat_ids:
        logging.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping notification.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    all_ok = True
    for chat_id in chat_ids:
        resp = requests.post(url, data={"chat_id": chat_id, "text": text})
        if resp.status_code != 200:
            logging.error(f"Telegram send to {chat_id} failed: {resp.status_code} {resp.text}")
            all_ok = False
        else:
            logging.info(f"Telegram notification sent to {chat_id}.")
    return all_ok


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def main():
    slots = check_einbuergerungsurkunde()

    if not slots:
        print("No matching appointments right now.")
        return

    state = load_state()
    already_notified = set(state.get("notified_slots", []))
    current_keys = {slot_key(s): s for s in slots}

    newly_sent = set()
    for key, slot in current_keys.items():
        if key in already_notified:
            continue
        message = format_slot_message(slot)
        print(message)
        if send_telegram_message(message):
            newly_sent.add(key)
            time.sleep(3)  # gap between messages so your phone doesn't batch them into one alert
        else:
            logging.warning(f"Failed to notify for {key} — will retry next run.")

    # Keep only keys still currently available (whether previously notified or
    # just sent now) — anything that vanished gets dropped, so if it reappears
    # later you'll be notified about it again.
    state["notified_slots"] = list((already_notified & set(current_keys.keys())) | newly_sent)
    save_state(state)

    if not newly_sent:
        print("No new slots since last check — not re-notifying.")


if __name__ == "__main__":
    main()
