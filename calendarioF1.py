# requirements: pip install requests google-auth google-auth-oauthlib google-api-python-client

import requests
from datetime import datetime, timedelta
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import os, pickle, base64

# ─── CONFIG ───────────────────────────────────────────────
CALENDAR_NAME = "Formula 1 2026"
F1_SEASON = 2026

CREATE_SESSIONS = {
    "Race":        True,
    "Qualifying":  False,
    "Sprint":      False,
    "FP1":         False,
    "FP2":         False,
    "FP3":         False,
}

SESSION_DURATION = {
    "Race":       120,
    "Qualifying":  60,
    "Sprint":      45,
    "FP1":         60,
    "FP2":         60,
    "FP3":         60,
}

SESSION_EMOJI = {
    "Race":       "🏁",
    "Qualifying": "⏱",
    "Sprint":     "💨",
    "FP1":        "🔧",
    "FP2":        "🔧",
    "FP3":        "🔧",
}
# ──────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/calendar"]
JOLPICA_BASE = "https://api.jolpi.ca/ergast/f1"


def get_f1_schedule():
    url = f"{JOLPICA_BASE}/{F1_SEASON}.json?limit=30"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()["MRData"]["RaceTable"]["Races"]


def parse_dt(date_str, time_str):
    if not date_str:
        return None
    time_str = time_str or "13:00:00Z"
    dt_str = f"{date_str}T{time_str}".replace("Z", "+00:00")
    return datetime.fromisoformat(dt_str)


def extract_sessions(race):
    sessions = []
    session_map = {
        "FP1":        "FirstPractice",
        "FP2":        "SecondPractice",
        "FP3":        "ThirdPractice",
        "Qualifying": "Qualifying",
        "Sprint":     "Sprint",
        "Race":       None,
    }

    for session_name, block_key in session_map.items():
        if not CREATE_SESSIONS.get(session_name):
            continue
        if session_name == "Race":
            dt = parse_dt(race.get("date"), race.get("time"))
        else:
            block = race.get(block_key)
            if not block:
                continue
            dt = parse_dt(block.get("date"), block.get("time"))
        if dt:
            sessions.append((session_name, dt))

    return sessions


def build_event(race, session_name, start_dt):
    gp_name = race["raceName"]
    circuit = race["Circuit"]["circuitName"]
    locality = race["Circuit"]["Location"]["locality"]
    country = race["Circuit"]["Location"]["country"]
    round_n = race["round"]
    emoji = SESSION_EMOJI[session_name]
    duration = SESSION_DURATION[session_name]
    end_dt = start_dt + timedelta(minutes=duration)
    summary = f"{emoji} F1 – {gp_name} | {session_name}"
    description = (
        f"Round {round_n} — {gp_name}\n"
        f"Sessione: {session_name}\n"
        f"Circuito: {circuit}\n"
        f"Luogo: {locality}, {country}"
    )
    return {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Rome"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Europe/Rome"},
    }, summary, start_dt.date().isoformat()


def google_auth():
    creds = None

    # Se siamo in GitHub Actions, leggi il token dal secret (base64)
    token_b64 = os.environ.get("GOOGLE_TOKEN_PICKLE_B64")
    if token_b64:
        token_bytes = base64.b64decode(token_b64)
        creds = pickle.loads(token_bytes)

    # Altrimenti leggi dal file locale
    elif os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.pickle", "wb") as f:
            pickle.dump(creds, f)

    return build("calendar", "v3", credentials=creds)


def get_or_create_calendar(service):
    for cal in service.calendarList().list().execute().get("items", []):
        if cal["summary"] == CALENDAR_NAME:
            return cal["id"]
    new_cal = service.calendars().insert(
        body={"summary": CALENDAR_NAME, "timeZone": "Europe/Rome"}
    ).execute()
    print(f"Calendario creato: {CALENDAR_NAME}")
    return new_cal["id"]


def fetch_existing_events(service, cal_id):
    existing = set()
    page_token = None
    while True:
        result = service.events().list(
            calendarId=cal_id,
            maxResults=2500,
            singleEvents=True,
            pageToken=page_token,
        ).execute()
        for ev in result.get("items", []):
            summary = ev.get("summary", "")
            start = ev.get("start", {}).get("dateTime", "")
            if summary and start:
                existing.add((summary, start[:10]))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return existing


def main():
    print(f"Fetching calendario F1 {F1_SEASON} da Jolpica...")
    races = get_f1_schedule()
    print(f"  → {len(races)} GP trovati")

    print("Autenticazione Google Calendar...")
    service = google_auth()
    cal_id = get_or_create_calendar(service)

    print("Carico eventi esistenti...")
    existing = fetch_existing_events(service, cal_id)
    print(f"  → {len(existing)} eventi già presenti")

    inserted = skipped = 0

    for race in races:
        for session_name, start_dt in extract_sessions(race):
            event, summary, date_key = build_event(race, session_name, start_dt)
            if (summary, date_key) in existing:
                print(f"  ↷ già presente: {summary}")
                skipped += 1
                continue
            service.events().insert(calendarId=cal_id, body=event).execute()
            print(f"  ✓ aggiunto: {summary} ({date_key})")
            inserted += 1

    print(f"""
─────────────────────────────
✓ Aggiunti:       {inserted}
↷ Già presenti:   {skipped}
─────────────────────────────
Calendario: '{CALENDAR_NAME}'
""")


if __name__ == "__main__":
    main()
