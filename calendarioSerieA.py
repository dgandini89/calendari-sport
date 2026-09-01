# requirements: pip install requests google-auth google-auth-oauthlib google-api-python-client

import requests
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import os, pickle, base64

# ─── CONFIG ───────────────────────────────────────────────
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "TUA_API_KEY_QUI")
TEAM_FILTER = None          # es. "Inter Milan" per solo Inter — None = tutte le partite
TEAM_NOTIFY = "Inter Milan" # squadra per cui vuoi notifiche e colore evidenziato
CALENDAR_NAME = "Serie A 2026-27"
SERIE_A_CODE = "SA"
SERIE_A_SEASON = 2026
# ──────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_fixtures():
    url = f"https://api.football-data.org/v4/competitions/{SERIE_A_CODE}/matches"
    params = {"season": SERIE_A_SEASON}
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    r = requests.get(url, headers=headers, params=params)

    if r.status_code == 400:
        print(f"Errore 400: stagione probabilmente non ancora disponibile sull'API.")
        print(f"Risposta: {r.json().get('message', r.text)}")
        return []

    r.raise_for_status()
    matches = r.json()["matches"]

    if TEAM_FILTER:
        matches = [
            m for m in matches
            if TEAM_FILTER.lower() in m["homeTeam"]["name"].lower()
            or TEAM_FILTER.lower() in m["awayTeam"]["name"].lower()
        ]
    return matches


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
        body={
            "summary": CALENDAR_NAME,
            "timeZone": "Europe/Rome",
        }
    ).execute()

    # Colore calendario: Salvia
    service.calendarList().patch(
        calendarId=new_cal["id"],
        body={"backgroundColor": "#33B679", "foregroundColor": "#ffffff"}
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


def match_to_event(match):
    home = match["homeTeam"]["name"]
    away = match["awayTeam"]["name"]
    utc_date = match["utcDate"]
    start_dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
    end_dt = start_dt + timedelta(hours=2)
    matchday = match["matchday"]
    status = match["status"]

    summary = f"⚽ {home} vs {away}"
    date_key = start_dt.date().isoformat()

    is_inter = TEAM_NOTIFY.lower() in home.lower() or TEAM_NOTIFY.lower() in away.lower()

    reminders = {
        "useDefault": False,
        "overrides": [
            {"method": "popup", "minutes": 60 * 24},  # 1 giorno prima
            {"method": "popup", "minutes": 60 * 2},   # 2 ore prima
        ] if is_inter else [],
    }

    event = {
        "summary": summary,
        "description": f"Giornata {matchday} — Serie A\nStato: {status}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Rome"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Europe/Rome"},
        "reminders": reminders,
    }

    if is_inter:
        event["colorId"] = "6"

    return event, summary, date_key


def main():
    print("Fetching fixtures da football-data.org...")
    matches = get_fixtures()
    print(f"  → {len(matches)} partite trovate")

    if not matches:
        return

    print("Autenticazione Google Calendar...")
    service = google_auth()
    cal_id = get_or_create_calendar(service)

    print("Carico eventi esistenti nel calendario...")
    existing = fetch_existing_events(service, cal_id)
    print(f"  → {len(existing)} eventi già presenti")

    inserted = skipped = no_date = 0

    for match in matches:
        if not match.get("utcDate"):
            no_date += 1
            continue

        event, summary, date_key = match_to_event(match)

        if (summary, date_key) in existing:
            print(f"  ↷ già presente: {summary} ({date_key})")
            skipped += 1
            continue

        service.events().insert(calendarId=cal_id, body=event).execute()
        print(f"  ✓ aggiunto: {summary} ({date_key})")
        inserted += 1

    print(f"""
─────────────────────────────
✓ Aggiunti:          {inserted}
↷ Già presenti:      {skipped}
⚠ Senza data:        {no_date}
─────────────────────────────
Calendario: '{CALENDAR_NAME}'
""")


if __name__ == "__main__":
    main()
