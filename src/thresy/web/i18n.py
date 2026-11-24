"""Every text of the dashboard in English and German, and numbers and dates in the language's
format (``COACH_LANGUAGE``; other languages fall back to English).

Keep texts generic: nothing here may be specific to one athlete. A text with a count has the
forms ``key`` (other) and ``key.one``; :meth:`Lang.tn` picks one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

LANGUAGES = ("en", "de")

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        # shell
        "app_tagline": "Your running, at a glance",
        "nav_today": "Today",
        "nav_runs": "Runs",
        "nav_trends": "Trends",
        "nav_races": "Races",
        "nav_segments": "Segments",
        "nav_data": "Data",
        "nav_main": "Main",
        "logout": "Sign out",
        "theme": "Theme",
        "theme_system": "Follow system",
        "theme_light": "Light",
        "theme_dark": "Dark",
        "demo_badge": "Demo",
        "demo_note": "An invented athlete with synthetic data. Nothing here is real.",
        "synced_ago": "Synced {ago}",
        "never_synced": "Not synced yet",
        "table_view": "Show as table",
        # login
        "login_title": "Sign in",
        "login_lead": "This dashboard is private. Enter your password to continue.",
        "password": "Password",
        "login_submit": "Sign in",
        "login_failed": "That password did not work.",
        "login_locked": "Too many attempts. Try again in {wait}.",
        "login_expired": "The form had expired. Please try again.",
        "login_bye": "You are signed out.",
        "login_session_ended": "Your session ended. Please sign in again.",
        "login_demo": "Demo mode: no password needed.",
        "login_demo_link": "Open the dashboard",
        # errors
        "error_title": "Something went wrong",
        "error_text": "The page could not be shown. Try again in a moment.",
        "error_db_title": "The data is out of reach",
        "error_db_text": "The database cannot be reached right now. Try again in a moment.",
        "not_found_title": "Not here",
        "not_found_text": "There is no page at this address.",
        "back_home": "Back to today",
        "forbidden_title": "Not allowed",
        "forbidden_text": "This request was refused. Reload the page and try again.",
        # last night
        "last_night": "Last night",
        "asleep": "asleep",
        "sleep_score": "score {n}",
        "stage_deep": "Deep",
        "stage_light": "Light",
        "stage_rem": "REM",
        "stage_awake": "Awake",
        "slept_from_to": "{start} to {end}",
        "hrv": "HRV",
        "hrv_long": "Heart-rate variability",
        "hrv_usual": "usual {low}–{high} ms",
        "usual_short": "usual {range}",
        "hrv_in": "in your usual range",
        "hrv_below": "below your usual range",
        "hrv_above": "above your usual range",
        "hrv_none": "no reading",
        "resting_hr": "Resting HR",
        "resting_hr_long": "Resting heart rate",
        "vs_usual": "{delta} vs usual",
        "as_usual": "as usual",
        "night_pending_title": "Not synced yet",
        "night_pending_text": (
            "Last night's sleep has not arrived from Garmin yet. It usually does once the"
            " watch syncs in the morning."
        ),
        "night_latest": "Latest night: {date}, {dur}",
        "night_none": "No sleep data yet. Nights appear here once the watch has recorded one.",
        "night_synced_at": "arrived {time}",
        # today
        "today": "Today",
        "today_nothing": "Nothing planned for today.",
        "today_no_plan": "No plan yet. When your coach plans sessions, today's shows up here.",
        "rest_day": "Rest day",
        "ran_today": "Ran today",
        "status_done": "Done",
        "status_skipped": "Skipped",
        "status_moved": "Moved",
        "status_planned": "Planned",
        "status_missed": "Not marked",
        "status_upcoming": "Coming up",
        # coach
        "coach_said": "{coach} said",
        "msg_morning_briefing": "Morning briefing",
        "msg_post_run": "Run debrief",
        "msg_other": "Message",
        "read_all": "Read all",
        "coach_none": (
            "No briefing yet. The coach's latest morning briefing or run debrief appears here."
        ),
        # week
        "this_week": "This week",
        "week_km": "{done} of {planned} km",
        "week_km_noplan": "{done} km",
        "km_so_far": "run so far",
        "planned_km": "planned",
        "week_runs": "{n} runs",
        "week_runs.one": "{n} run",
        "legend_planned": "Planned",
        "legend_ran": "Ran",
        "week_empty": "Nothing planned or run this week yet.",
        # runs
        "recent_runs": "Recent runs",
        "all_runs": "All runs",
        "runs_none": "No runs yet. They appear here after the next sync.",
        "runs_in_year": "Runs in {year}",
        "runs_year_total": "{n} runs · {km} km",
        "runs_year_total.one": "{n} run · {km} km",
        "col_date": "Date",
        "col_run": "Run",
        "col_distance": "Distance",
        "col_time": "Time",
        "col_pace": "Pace",
        "col_hr": "HR",
        "col_ascent": "Ascent",
        "col_km": "km",
        "col_week": "Week",
        "col_runs": "Runs",
        "col_long": "Long run",
        "col_value": "Value",
        "col_range": "Usual range",
        "col_avg7": "7-day average",
        "col_name": "Name",
        "col_result": "Result",
        "col_goal": "Goal",
        "col_place": "Place",
        "col_split": "km",
        "col_climb": "Climb",
        "col_grade": "Grade",
        "col_rep": "Rep",
        "col_effort": "Effort",
        "col_year": "Year",
        "col_same_day": "By {date}",
        "col_full_year": "Whole year",
        # run detail
        "distance": "Distance",
        "moving_time": "Moving time",
        "pace": "Pace",
        "avg_hr": "Avg HR",
        "max_hr": "Max HR",
        "ascent": "Ascent",
        "cadence": "Cadence",
        "power": "Power",
        "training_effect": "Training effect",
        "track": "Track",
        "track_note": "The shape of the route, without a map.",
        "start": "Start",
        "finish": "Finish",
        "profile": "Over the run",
        "profile_pace": "Pace",
        "profile_hr": "Heart rate",
        "profile_alt": "Altitude",
        "splits": "Splits",
        "halves": "First and second half",
        "negative_split": "Negative split: the second half was faster.",
        "positive_split": "The second half was {diff} /km slower.",
        "even_split": "Even halves.",
        "drift": "Heart-rate drift",
        "drift_text": "{bpm} bpm drift, {pct} % decoupling",
        "drift_good": "Pace held without the heart rate creeping up.",
        "drift_high": "The heart rate crept up at the same pace.",
        "climbs": "Climbs",
        "reps": "Reps",
        "reps_summary": "{n} reps at {pace} /km on average",
        "plan_for_run": "Planned",
        "segment_efforts": "Segments on this run",
        "no_records": (
            "This activity has no per-second data (no FIT file), so there are no splits, charts"
            " or track."
        ),
        "back_to_runs": "All runs",
        "run_not_found": "There is no activity with this number.",
        "other_activity": "Activity",
        # trends
        "trends": "Trends",
        "weekly_volume": "Weekly volume",
        "weekly_volume_sub": "The last {n} weeks, Monday to Sunday",
        "legend_long": "Longest run",
        "legend_other": "Other runs",
        "this_week_so_far": "this week so far",
        "avg_4w": "{km} km a week over the last 4 weeks",
        "avg_4w_short": "a week, last 4 weeks",
        "year_so_far": "Year to date",
        "ytd_vs": "{delta} vs. the same day last year",
        "ytd_first": "First year on record",
        "year_chart_sub": "Running distance added up since 1 January",
        "recovery": "Recovery",
        "recovery_sub": "The last {n} weeks, by the morning you woke up",
        "sleep": "Sleep",
        "usual_range": "usual range",
        "avg7": "7-day average",
        "nights_below": "{n} nights below the usual range",
        "nights_below.one": "{n} night below the usual range",
        "trends_empty": "Trends need a few weeks of data.",
        "hrv_range_note": "The band is Garmin's balanced range for each night.",
        "rhr_range_note": "The band is the middle 80 % of the period.",
        # races
        "races": "Races",
        "next_race": "Next race",
        "days_to_go": "days to go",
        "days_to_go.one": "day to go",
        "race_today": "Race day",
        "goal": "Goal",
        "priority": "{p} race",
        "upcoming": "Coming up",
        "results": "Results",
        "races_none": "No races yet. Races your coach records show up here.",
        "race_status_maybe": "maybe",
        "race_status_dnf": "did not finish",
        "race_status_dns": "did not start",
        "race_status_planned": "planned",
        "watch_time": "watch",
        # segments
        "segments": "Segments",
        "segments_none": (
            "No segments yet. Segments are climbs or stretches you run often; once your coach"
            " has stored some and matched your runs, their best and latest efforts show here."
        ),
        "best": "Best",
        "latest": "Latest",
        "efforts": "{n} efforts",
        "efforts.one": "{n} effort",
        "segment_chart_sub": "Every effort, by date",
        "segment_no_efforts": "No efforts matched yet.",
        # data
        "data_title": "State of the data",
        "sync": "Garmin sync",
        "last_sync": "Last successful sync",
        "last_attempt": "Last attempt",
        "garmin_login": "Garmin login",
        "login_ok": "working",
        "login_needed": "needs a new login",
        "failures_in_row": "{n} failures in a row",
        "failures_in_row.one": "{n} failure",
        "rate_limited": "Garmin asks to slow down until {time}",
        "backup": "Backup",
        "last_backup": "Last successful backup",
        "restore_check": "Restore check",
        "restore_ok": "passed {when}",
        "restore_failed": "failed {when}",
        "never": "never",
        "no_backup": "No backup recorded yet.",
        "data_counts": "In the database",
        "count_runs": "Runs",
        "count_records": "With per-second data",
        "count_nights": "Nights with sleep",
        "count_span": "From {first} to {last}",
        "count_span_label": "First and last run",
        "all_good": "Everything is up to date.",
        "notice_auth_title": "Garmin needs a new login",
        "notice_auth_text": (
            "No new data arrives until someone logs in again (coach sync login on the server)."
        ),
        "notice_sync_title": "The Garmin sync is failing",
        "notice_sync_text": "No successful sync since {since}. New data may be missing.",
        "notice_backup_title": "The backup needs attention",
        "notice_backup_failing": "The last backup failed. Last good one: {since}.",
        "notice_backup_overdue": "No backup since {since}.",
        "notice_rate_title": "Garmin asked to slow down",
        "notice_rate_text": "The sync waits until {time} and then continues on its own.",
        "details": "Details",
        "last_error": "Last error",
        # formats
        "unit_bpm": "bpm",
        "unit_km": "km",
        "unit_min_km": "/km",
        "unit_ms": "ms",
        "unit_m": "m",
        "unit_spm": "spm",
        "unit_w": "W",
        "unit_h": "h",
        "unit_min": "min",
        "just_now": "just now",
        "min_ago": "{n} min ago",
        "h_ago": "{n} h ago",
        "days_ago": "{n} days ago",
        "days_ago.one": "yesterday",
        "in_days": "in {n} days",
        "in_days.one": "tomorrow",
        "today_rel": "today",
        "wait_s": "{n} seconds",
        "wait_min": "{n} minutes",
        "wait_min.one": "a minute",
        "week_no": "W{n}",
        "page_of": "Page {n}",
        "newer": "Newer",
        "older": "Older",
    },
    "de": {
        "app_tagline": "Dein Laufen auf einen Blick",
        "nav_today": "Heute",
        "nav_runs": "Läufe",
        "nav_trends": "Trends",
        "nav_races": "Rennen",
        "nav_segments": "Segmente",
        "nav_data": "Daten",
        "nav_main": "Hauptmenü",
        "logout": "Abmelden",
        "theme": "Darstellung",
        "theme_system": "Wie das System",
        "theme_light": "Hell",
        "theme_dark": "Dunkel",
        "demo_badge": "Demo",
        "demo_note": "Ausgedachte Person, synthetische Daten. Nichts davon ist echt.",
        "synced_ago": "Synchronisiert {ago}",
        "never_synced": "Noch nicht synchronisiert",
        "table_view": "Als Tabelle",
        "login_title": "Anmelden",
        "login_lead": "Dieses Dashboard ist privat. Gib dein Passwort ein.",
        "password": "Passwort",
        "login_submit": "Anmelden",
        "login_failed": "Das Passwort stimmt nicht.",
        "login_locked": "Zu viele Versuche. Bitte in {wait} erneut versuchen.",
        "login_expired": "Das Formular war abgelaufen. Bitte noch einmal.",
        "login_bye": "Du bist abgemeldet.",
        "login_session_ended": "Deine Sitzung ist abgelaufen. Bitte melde dich neu an.",
        "login_demo": "Demo: kein Passwort nötig.",
        "login_demo_link": "Zum Dashboard",
        "error_title": "Etwas ist schiefgegangen",
        "error_text": "Die Seite konnte nicht angezeigt werden. Versuch es gleich noch einmal.",
        "error_db_title": "Die Daten sind nicht erreichbar",
        "error_db_text": (
            "Die Datenbank ist gerade nicht erreichbar. Versuch es gleich noch einmal. "
        ),
        "not_found_title": "Nicht hier",
        "not_found_text": "Unter dieser Adresse gibt es keine Seite.",
        "back_home": "Zurück zu heute",
        "forbidden_title": "Nicht erlaubt",
        "forbidden_text": (
            "Die Anfrage wurde abgelehnt. Lade die Seite neu und versuch es noch einmal. "
        ),
        "last_night": "Letzte Nacht",
        "asleep": "geschlafen",
        "sleep_score": "Schlafwert {n}",
        "stage_deep": "Tief",
        "stage_light": "Leicht",
        "stage_rem": "REM",
        "stage_awake": "Wach",
        "slept_from_to": "{start} bis {end}",
        "hrv": "HRV",
        "hrv_long": "Herzfrequenzvariabilität",
        "hrv_usual": "üblich {low}–{high} ms",
        "usual_short": "üblich {range}",
        "hrv_in": "in deinem üblichen Bereich",
        "hrv_below": "unter deinem üblichen Bereich",
        "hrv_above": "über deinem üblichen Bereich",
        "hrv_none": "kein Messwert",
        "resting_hr": "Ruhepuls",
        "resting_hr_long": "Ruhepuls",
        "vs_usual": "{delta} gegenüber üblich",
        "as_usual": "wie üblich",
        "night_pending_title": "Noch nicht synchronisiert",
        "night_pending_text": (
            "Die letzte Nacht ist noch nicht von Garmin gekommen. Meist kommt sie, sobald die"
            " Uhr morgens synchronisiert."
        ),
        "night_latest": "Letzte erfasste Nacht: {date}, {dur}",
        "night_none": (
            "Noch keine Schlafdaten. Nächte erscheinen hier, sobald die Uhr eine aufgezeichnet hat."
        ),
        "night_synced_at": "angekommen {time}",
        "today": "Heute",
        "today_nothing": "Für heute ist nichts geplant.",
        "today_no_plan": (
            "Noch kein Plan. Sobald dein Coach Einheiten plant, steht hier die von heute. "
        ),
        "rest_day": "Ruhetag",
        "ran_today": "Heute gelaufen",
        "status_done": "Erledigt",
        "status_skipped": "Ausgelassen",
        "status_moved": "Verschoben",
        "status_planned": "Geplant",
        "status_missed": "Nicht abgehakt",
        "status_upcoming": "Kommt noch",
        "coach_said": "{coach} schrieb",
        "msg_morning_briefing": "Morgenbriefing",
        "msg_post_run": "Laufbesprechung",
        "msg_other": "Nachricht",
        "read_all": "Alles lesen",
        "coach_none": (
            "Noch kein Briefing. Hier steht das letzte Morgenbriefing oder die letzte "
            "Laufbesprechung."
        ),
        "this_week": "Diese Woche",
        "week_km": "{done} von {planned} km",
        "week_km_noplan": "{done} km",
        "km_so_far": "bisher gelaufen",
        "planned_km": "geplant",
        "week_runs": "{n} Läufe",
        "week_runs.one": "{n} Lauf",
        "legend_planned": "Geplant",
        "legend_ran": "Gelaufen",
        "week_empty": "Diese Woche ist noch nichts geplant oder gelaufen.",
        "recent_runs": "Letzte Läufe",
        "all_runs": "Alle Läufe",
        "runs_none": "Noch keine Läufe. Sie erscheinen hier nach der nächsten Synchronisierung.",
        "runs_in_year": "Läufe {year}",
        "runs_year_total": "{n} Läufe · {km} km",
        "runs_year_total.one": "{n} Lauf · {km} km",
        "col_date": "Datum",
        "col_run": "Lauf",
        "col_distance": "Distanz",
        "col_time": "Zeit",
        "col_pace": "Pace",
        "col_hr": "Puls",
        "col_ascent": "Anstieg",
        "col_km": "km",
        "col_week": "Woche",
        "col_runs": "Läufe",
        "col_long": "Längster Lauf",
        "col_value": "Wert",
        "col_range": "Üblicher Bereich",
        "col_avg7": "7-Tage-Schnitt",
        "col_name": "Name",
        "col_result": "Ergebnis",
        "col_goal": "Ziel",
        "col_place": "Platz",
        "col_split": "km",
        "col_climb": "Anstieg",
        "col_grade": "Steigung",
        "col_rep": "Wdh.",
        "col_effort": "Versuch",
        "col_year": "Jahr",
        "col_same_day": "Bis {date}",
        "col_full_year": "Ganzes Jahr",
        "distance": "Distanz",
        "moving_time": "Bewegungszeit",
        "pace": "Pace",
        "avg_hr": "Ø Puls",
        "max_hr": "Max. Puls",
        "ascent": "Anstieg",
        "cadence": "Kadenz",
        "power": "Leistung",
        "training_effect": "Trainingseffekt",
        "track": "Strecke",
        "track_note": "Die Form der Strecke, ohne Karte.",
        "start": "Start",
        "finish": "Ziel",
        "profile": "Im Verlauf",
        "profile_pace": "Pace",
        "profile_hr": "Puls",
        "profile_alt": "Höhe",
        "splits": "Kilometer",
        "halves": "Erste und zweite Hälfte",
        "negative_split": "Negativer Split: die zweite Hälfte war schneller.",
        "positive_split": "Die zweite Hälfte war {diff} /km langsamer.",
        "even_split": "Gleichmäßige Hälften.",
        "drift": "Pulsdrift",
        "drift_text": "{bpm} S/min Drift, {pct} % Entkopplung",
        "drift_good": "Pace gehalten, ohne dass der Puls stieg.",
        "drift_high": "Bei gleicher Pace stieg der Puls.",
        "climbs": "Anstiege",
        "reps": "Wiederholungen",
        "reps_summary": "{n} Wiederholungen, im Schnitt {pace} /km",
        "plan_for_run": "Geplant",
        "segment_efforts": "Segmente in diesem Lauf",
        "no_records": (
            "Für diese Aktivität gibt es keine Sekundendaten (keine FIT-Datei), daher keine"
            " Kilometer, Diagramme oder Strecke."
        ),
        "back_to_runs": "Alle Läufe",
        "run_not_found": "Es gibt keine Aktivität mit dieser Nummer.",
        "other_activity": "Aktivität",
        "trends": "Trends",
        "weekly_volume": "Wochenumfang",
        "weekly_volume_sub": "Die letzten {n} Wochen, Montag bis Sonntag",
        "legend_long": "Längster Lauf",
        "legend_other": "Übrige Läufe",
        "this_week_so_far": "diese Woche bisher",
        "avg_4w": "{km} km pro Woche in den letzten 4 Wochen",
        "avg_4w_short": "pro Woche, letzte 4 Wochen",
        "year_so_far": "Jahr bis heute",
        "ytd_vs": "{delta} gegenüber dem gleichen Tag im Vorjahr",
        "ytd_first": "Erstes Jahr mit Daten",
        "year_chart_sub": "Laufkilometer, aufsummiert ab 1. Januar",
        "recovery": "Erholung",
        "recovery_sub": "Die letzten {n} Wochen, nach dem Morgen des Aufwachens",
        "sleep": "Schlaf",
        "usual_range": "üblicher Bereich",
        "avg7": "7-Tage-Schnitt",
        "nights_below": "{n} Nächte unter dem üblichen Bereich",
        "nights_below.one": "{n} Nacht unter dem üblichen Bereich",
        "trends_empty": "Trends brauchen ein paar Wochen Daten.",
        "hrv_range_note": "Das Band ist Garmins ausgeglichener Bereich für jede Nacht.",
        "rhr_range_note": "Das Band umfasst die mittleren 80 % des Zeitraums.",
        "races": "Rennen",
        "next_race": "Nächstes Rennen",
        "days_to_go": "Tage bis zum Start",
        "days_to_go.one": "Tag bis zum Start",
        "race_today": "Renntag",
        "goal": "Ziel",
        "priority": "{p}-Rennen",
        "upcoming": "Kommende",
        "results": "Ergebnisse",
        "races_none": "Noch keine Rennen. Rennen, die dein Coach einträgt, erscheinen hier.",
        "race_status_maybe": "vielleicht",
        "race_status_dnf": "nicht beendet",
        "race_status_dns": "nicht gestartet",
        "race_status_planned": "geplant",
        "watch_time": "Uhr",
        "segments": "Segmente",
        "segments_none": (
            "Noch keine Segmente. Segmente sind Anstiege oder Abschnitte, die du oft läufst;"
            " sobald dein Coach welche gespeichert und deinen Läufen zugeordnet hat, stehen hier"
            " die beste und die letzte Zeit."
        ),
        "best": "Bestzeit",
        "latest": "Zuletzt",
        "efforts": "{n} Versuche",
        "efforts.one": "{n} Versuch",
        "segment_chart_sub": "Jeder Versuch, nach Datum",
        "segment_no_efforts": "Noch keine Versuche zugeordnet.",
        "data_title": "Datenstand",
        "sync": "Garmin-Synchronisierung",
        "last_sync": "Letzte erfolgreiche Synchronisierung",
        "last_attempt": "Letzter Versuch",
        "garmin_login": "Garmin-Anmeldung",
        "login_ok": "funktioniert",
        "login_needed": "braucht eine neue Anmeldung",
        "failures_in_row": "{n} Fehlschläge in Folge",
        "failures_in_row.one": "{n} Fehlschlag",
        "rate_limited": "Garmin bittet bis {time} um Pause",
        "backup": "Backup",
        "last_backup": "Letztes erfolgreiches Backup",
        "restore_check": "Wiederherstellungstest",
        "restore_ok": "bestanden {when}",
        "restore_failed": "fehlgeschlagen {when}",
        "never": "nie",
        "no_backup": "Noch kein Backup verzeichnet.",
        "data_counts": "In der Datenbank",
        "count_runs": "Läufe",
        "count_records": "Mit Sekundendaten",
        "count_nights": "Nächte mit Schlaf",
        "count_span": "Von {first} bis {last}",
        "count_span_label": "Erster und letzter Lauf",
        "all_good": "Alles ist aktuell.",
        "notice_auth_title": "Garmin braucht eine neue Anmeldung",
        "notice_auth_text": (
            "Bis sich jemand neu anmeldet, kommen keine neuen Daten (coach sync login auf dem"
            " Server)."
        ),
        "notice_sync_title": "Die Garmin-Synchronisierung schlägt fehl",
        "notice_sync_text": (
            "Keine erfolgreiche Synchronisierung seit {since}. Neue Daten fehlen vielleicht. "
        ),
        "notice_backup_title": "Das Backup braucht Aufmerksamkeit",
        "notice_backup_failing": "Das letzte Backup ist fehlgeschlagen. Letztes gutes: {since}.",
        "notice_backup_overdue": "Kein Backup seit {since}.",
        "notice_rate_title": "Garmin bittet um eine Pause",
        "notice_rate_text": (
            "Die Synchronisierung wartet bis {time} und macht dann von selbst weiter. "
        ),
        "details": "Details",
        "last_error": "Letzter Fehler",
        "unit_bpm": "S/min",
        "unit_km": "km",
        "unit_min_km": "/km",
        "unit_ms": "ms",
        "unit_m": "m",
        "unit_spm": "spm",
        "unit_w": "W",
        "unit_h": "h",
        "unit_min": "min",
        "just_now": "gerade eben",
        "min_ago": "vor {n} Min.",
        "h_ago": "vor {n} Std.",
        "days_ago": "vor {n} Tagen",
        "days_ago.one": "gestern",
        "in_days": "in {n} Tagen",
        "in_days.one": "morgen",
        "today_rel": "heute",
        "wait_s": "{n} Sekunden",
        "wait_min": "{n} Minuten",
        "wait_min.one": "einer Minute",
        "week_no": "KW {n}",
        "page_of": "Seite {n}",
        "newer": "Neuer",
        "older": "Älter",
    },
}

WEEKDAYS = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "de": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
}
WEEKDAYS_SHORT = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "de": ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"),
}
MONTHS = {
    "en": (
        "January", "February", "March", "April", "May", "June", "July", "August",
        "September", "October", "November", "December",
    ),
    "de": (
        "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
        "September", "Oktober", "November", "Dezember",
    ),
}  # fmt: skip
MONTHS_SHORT = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "de": ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"),
}


def _missing(value: object) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


@dataclass(frozen=True)
class Lang:
    """Texts and formats of one language."""

    code: str = "en"

    @classmethod
    def get(cls, code: str | None) -> Lang:
        code = (code or "en").strip().lower()[:2]
        return cls(code if code in LANGUAGES else "en")

    # texts -------------------------------------------------------------------------------------

    def t(self, key: str, **values: object) -> str:
        text = TEXTS[self.code].get(key) or TEXTS["en"].get(key) or key
        return text.format(**values) if values else text

    def tn(self, key: str, n: int, **values: object) -> str:
        """A text with a count: ``key.one`` for 1, else ``key``; ``{n}`` is the formatted count."""
        name = f"{key}.one" if n == 1 and f"{key}.one" in TEXTS[self.code] else key
        return self.t(name, n=self.int(n), **values)

    # numbers -----------------------------------------------------------------------------------

    def num(self, value: float | None, digits: int = 0) -> str:
        """``1234.5`` -> ``1,234.5`` (en) / ``1.234,5`` (de); a dash when missing."""
        if _missing(value):
            return "–"
        text = f"{float(value):,.{digits}f}"  # type: ignore[arg-type]
        if self.code == "de":
            text = text.replace(",", " ").replace(".", ",").replace(" ", ".")
        return text.replace("-", "−")

    def int(self, value: float | None) -> str:
        return self.num(value, 0)

    def signed(self, value: float | None, digits: int = 0) -> str:
        if _missing(value):
            return "–"
        if round(float(value), digits) == 0:  # type: ignore[arg-type]
            return "±" + self.num(0, digits)
        return ("+" if value > 0 else "") + self.num(value, digits)  # type: ignore[operator]

    def km(self, metres: float | None, digits: int = 1) -> str:
        return "–" if _missing(metres) else self.num(metres / 1000, digits)  # type: ignore[operator]

    def pace(self, s_per_km: float | None) -> str:
        """``275.4`` -> ``4:35``."""
        if _missing(s_per_km) or not 0 < s_per_km < 3600:  # type: ignore[operator]
            return "–"
        total = round(float(s_per_km))  # type: ignore[arg-type]
        return f"{total // 60}:{total % 60:02d}"

    def clock(self, seconds: float | None) -> str:
        """A duration as a clock: ``48:10``, ``1:02:05``."""
        if _missing(seconds):
            return "–"
        total = round(abs(float(seconds)))  # type: ignore[arg-type]
        h, rest = divmod(total, 3600)
        m, s = divmod(rest, 60)
        sign = "−" if seconds < 0 else ""  # type: ignore[operator]
        return f"{sign}{h}:{m:02d}:{s:02d}" if h else f"{sign}{m}:{s:02d}"

    def hm(self, seconds: float | None) -> str:
        """A longer duration in words: ``7 h 32 min``, ``45 min``."""
        if _missing(seconds):
            return "–"
        minutes = round(float(seconds) / 60)  # type: ignore[arg-type]
        h, m = divmod(minutes, 60)
        if not h:
            return f"{m} min"
        return f"{h} h {m:02d} min" if m else f"{h} h"

    def hm_short(self, seconds: float | None) -> str:
        """``7:32`` (hours and minutes)."""
        if _missing(seconds):
            return "–"
        minutes = round(float(seconds) / 60)  # type: ignore[arg-type]
        return f"{minutes // 60}:{minutes % 60:02d}"

    # dates -------------------------------------------------------------------------------------

    def weekday(self, day: date, short: bool = False) -> str:
        return (WEEKDAYS_SHORT if short else WEEKDAYS)[self.code][day.weekday()]

    def month(self, month: int, short: bool = False) -> str:
        return (MONTHS_SHORT if short else MONTHS)[self.code][month - 1]

    def day_month(self, day: date, *, short: bool = True) -> str:
        """``24 Nov`` / ``24. Nov``."""
        mon = self.month(day.month, short)
        return f"{day.day}. {mon}" if self.code == "de" else f"{day.day} {mon}"

    def date(self, day: date, *, today: date | None = None, weekday: bool = True) -> str:
        """``Tue 24 Nov`` / ``Di 24. Nov``, with the year when it is not ``today``'s."""
        text = self.day_month(day)
        if weekday:
            text = f"{self.weekday(day, short=True)} {text}"
        if today is None or day.year != today.year:
            text += f" {day.year}"
        return text

    def date_long(self, day: date) -> str:
        """``Tuesday, 24 November`` / ``Dienstag, 24. November``."""
        return f"{self.weekday(day)}, {self.day_month(day, short=False)}"

    def date_full(self, day: date) -> str:
        """``24 November 2030`` / ``24. November 2030``."""
        return f"{self.day_month(day, short=False)} {day.year}"

    def time(self, value: datetime | None) -> str:
        return "–" if value is None else value.strftime("%H:%M")

    def rel_day(self, day: date, today: date) -> str:
        delta = (day - today).days
        if delta == 0:
            return self.t("today_rel")
        if delta > 0:
            return self.tn("in_days", delta)
        return self.tn("days_ago", -delta)

    def ago(self, when: datetime | None, now: datetime) -> str:
        if when is None:
            return self.t("never")
        seconds = (now - when).total_seconds()
        if seconds < 90:
            return self.t("just_now")
        if seconds < 3600:
            return self.t("min_ago", n=round(seconds / 60))
        if seconds < 36 * 3600:
            return self.t("h_ago", n=round(seconds / 3600))
        return self.tn("days_ago", round(seconds / 86400))

    def wait(self, seconds: float) -> str:
        if seconds < 60:
            return self.t("wait_s", n=max(1, math.ceil(seconds)))
        return self.tn("wait_min", math.ceil(seconds / 60))
