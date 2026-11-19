"""What the bridge itself says to the athlete, in English and German (``COACH_LANGUAGE``).

Everything else the athlete reads is written by the agent. Unknown languages fall back to
English. Keep these texts generic: nothing here may be specific to one athlete.
"""

from __future__ import annotations

from typing import Any

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        # Agent run failures. The first two must be recognisable for the owner.
        "rate_limit": (
            "I can't answer right now: the usage limit of the coach's AI subscription has been"
            " reached. Please try again later."
        ),
        "auth": (
            "I can't answer: the login of the coach's AI agent has expired or is invalid. The"
            " owner needs to renew it and restart the bridge."
        ),
        "timeout": (
            "Sorry, that took too long and I stopped without an answer. Please try again,"
            " perhaps with a narrower question."
        ),
        "failed": "Sorry, something went wrong on my side and I have no answer. Please try again.",
        "interrupted": (
            "I was restarted while I was working on your last message, so it has no answer."
            " Please send it again."
        ),
        "no_answer": "Sorry, I ended up without an answer. Please ask again.",
        "interrupted_proactive": "It was interrupted by a restart of the bridge.",
        "detail": "Detail: {detail}",
        "proactive_prefix": "{what}: ",
        "what_morning_briefing": "Morning briefing",
        "what_post_run": "Run debrief",
        "what_proactive": "Scheduled message",
        # Notifications without an agent run.
        "garmin_auth": (
            "Garmin sync needs attention: the login to Garmin Connect is no longer valid, so no"
            " new data arrives. The owner needs to log in again (coach sync login)."
        ),
        "sync_failed": (
            "Garmin sync is failing: no successful sync since {since}. New data may be missing"
            " until it works again."
        ),
        "last_error": "Last error: {error}",
        # Chat commands.
        "help": (
            "Commands:\n"
            "/status – sync, agent and delivery status\n"
            "/sync – fetch new Garmin data now\n"
            "/new – start a fresh conversation\n"
            "/help – this list\n"
            "Everything else goes to the coach."
        ),
        "new": "Fresh start: your next message begins a new conversation.",
        "sync_done": "Sync done: {activities}; last night's sleep: {sleep}.",
        "sync_activities_0": "no new activities",
        "sync_activities_1": "1 new activity",
        "sync_activities_n": "{n} new activities",
        "sync_sleep_yes": "synced",
        "sync_sleep_no": "not synced yet",
        "sync_failed_cmd": "Sync failed: {error}",
        "sync_timeout": (
            "The sync service has not answered within {minutes} minutes. It may not be running."
        ),
        "sync_unavailable": (
            "Sync requests are not available yet (the agent_requests table is missing; run the"
            " database migrations)."
        ),
        "status_title": "Status",
        "status_sync": "Last successful sync: {value}",
        "status_sync_activity": "Last sync activity: {value}",
        "status_data": "Newest data: activity {activity}, sleep {sleep}",
        "status_sync_problem": "Sync failing, passes in a row: {value}",
        "status_garmin_login": "Garmin login: {value}",
        "status_runner": "Agent ({runner}): {value}",
        "status_runs": "Agent runs today: {count}, cost {cost}",
        "status_outbox": "Outbox: {pending} waiting, {failed} failed today",
        "status_busy": "Working on a request right now.",
        "status_quiet": "Quiet hours now: proactive messages wait until {until}.",
        "unknown": "unknown",
        "none": "none",
        "ok": "ok",
        "not_ok": "NOT OK",
        # Numbers-only morning briefing.
        "numbers_title": "Morning numbers (no coach commentary today)",
        "numbers_sleep": "Sleep: {duration}{score}",
        "numbers_score": " · score {score}",
        "numbers_not_synced": "Last night's sleep has not been synced yet.",
        "numbers_hrv": "HRV: {value} ms{range}",
        "numbers_hrv_range": " (balanced range {low}–{high}: {where})",
        "numbers_hrv_below": "below",
        "numbers_hrv_within": "within",
        "numbers_hrv_above": "above",
        "numbers_rhr": "Resting HR: {value} bpm",
        "numbers_run": "Yesterday: {distance} km in {duration}{pace}{hr}",
        "numbers_no_run": "Yesterday: no run.",
        "numbers_hr": " · avg HR {hr}",
    },
    "de": {
        "rate_limit": (
            "Ich kann gerade nicht antworten: Das Nutzungslimit des KI-Abos des Coaches ist"
            " erreicht. Bitte versuch es später noch einmal."
        ),
        "auth": (
            "Ich kann nicht antworten: Die Anmeldung des KI-Agenten des Coaches ist abgelaufen"
            " oder ungültig. Der Betreiber muss sie erneuern und die Bridge neu starten."
        ),
        "timeout": (
            "Entschuldige, das hat zu lange gedauert und ich habe ohne Antwort abgebrochen."
            " Bitte versuch es noch einmal, vielleicht mit einer engeren Frage."
        ),
        "failed": (
            "Entschuldige, bei mir ist etwas schiefgelaufen und ich habe keine Antwort. Bitte"
            " versuch es noch einmal."
        ),
        "interrupted": (
            "Ich wurde neu gestartet, während ich an deiner letzten Nachricht gearbeitet habe;"
            " sie ist deshalb unbeantwortet. Bitte schick sie noch einmal."
        ),
        "no_answer": (
            "Entschuldige, ich habe keine Antwort zustande gebracht. Frag bitte noch einmal."
        ),
        "interrupted_proactive": "Es wurde durch einen Neustart der Bridge unterbrochen.",
        "detail": "Details: {detail}",
        "proactive_prefix": "{what}: ",
        "what_morning_briefing": "Morgen-Briefing",
        "what_post_run": "Lauf-Auswertung",
        "what_proactive": "Geplante Nachricht",
        "garmin_auth": (
            "Der Garmin-Sync braucht Aufmerksamkeit: Die Anmeldung bei Garmin Connect ist nicht"
            " mehr gültig, deshalb kommen keine neuen Daten an. Der Betreiber muss sich neu"
            " anmelden (coach sync login)."
        ),
        "sync_failed": (
            "Der Garmin-Sync schlägt fehl: kein erfolgreicher Sync seit {since}. Neue Daten"
            " können fehlen, bis er wieder läuft."
        ),
        "last_error": "Letzter Fehler: {error}",
        "help": (
            "Befehle:\n"
            "/status – Stand von Sync, Agent und Zustellung\n"
            "/sync – neue Garmin-Daten jetzt abrufen\n"
            "/new – ein neues Gespräch beginnen\n"
            "/help – diese Liste\n"
            "Alles andere geht an den Coach."
        ),
        "new": "Neuer Anfang: Deine nächste Nachricht beginnt ein neues Gespräch.",
        "sync_done": "Sync fertig: {activities}; Schlaf der letzten Nacht: {sleep}.",
        "sync_activities_0": "keine neuen Aktivitäten",
        "sync_activities_1": "1 neue Aktivität",
        "sync_activities_n": "{n} neue Aktivitäten",
        "sync_sleep_yes": "synchronisiert",
        "sync_sleep_no": "noch nicht synchronisiert",
        "sync_failed_cmd": "Sync fehlgeschlagen: {error}",
        "sync_timeout": (
            "Der Sync-Dienst hat nicht innerhalb von {minutes} Minuten geantwortet. Vielleicht"
            " läuft er nicht."
        ),
        "sync_unavailable": (
            "Sync-Anfragen sind noch nicht verfügbar (die Tabelle agent_requests fehlt; bitte"
            " die Datenbank-Migrationen ausführen)."
        ),
        "status_title": "Status",
        "status_sync": "Letzter erfolgreicher Sync: {value}",
        "status_sync_activity": "Letzte Sync-Aktivität: {value}",
        "status_data": "Neueste Daten: Aktivität {activity}, Schlaf {sleep}",
        "status_sync_problem": "Sync schlägt fehl, Durchläufe in Folge: {value}",
        "status_garmin_login": "Garmin-Anmeldung: {value}",
        "status_runner": "Agent ({runner}): {value}",
        "status_runs": "Agent-Läufe heute: {count}, Kosten {cost}",
        "status_outbox": "Ausgang: {pending} wartend, {failed} heute fehlgeschlagen",
        "status_busy": "Arbeite gerade an einer Anfrage.",
        "status_quiet": "Gerade Ruhezeit: Proaktive Nachrichten warten bis {until}.",
        "unknown": "unbekannt",
        "none": "keine",
        "ok": "ok",
        "not_ok": "NICHT OK",
        "numbers_title": "Morgenwerte (heute ohne Kommentar des Coaches)",
        "numbers_sleep": "Schlaf: {duration}{score}",
        "numbers_score": " · Score {score}",
        "numbers_not_synced": "Der Schlaf der letzten Nacht ist noch nicht synchronisiert.",
        "numbers_hrv": "HRV: {value} ms{range}",
        "numbers_hrv_range": " (ausgeglichener Bereich {low}–{high}: {where})",
        "numbers_hrv_below": "darunter",
        "numbers_hrv_within": "im Bereich",
        "numbers_hrv_above": "darüber",
        "numbers_rhr": "Ruhepuls: {value} bpm",
        "numbers_run": "Gestern: {distance} km in {duration}{pace}{hr}",
        "numbers_no_run": "Gestern: kein Lauf.",
        "numbers_hr": " · Ø Puls {hr}",
    },
}


def lang_code(language: str) -> str:
    """``de-AT`` -> ``de``; unknown languages -> ``en``."""
    code = (language or "en").strip().lower().replace("_", "-").split("-")[0]
    return code if code in TEXTS else "en"


def t(language: str, key: str, **kwargs: Any) -> str:
    table = TEXTS[lang_code(language)]
    template = table.get(key) or TEXTS["en"][key]
    return template.format(**kwargs) if kwargs else template
