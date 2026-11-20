"""``coach weather``: forecasts for planning runs, from Open-Meteo (free, no key).

Two requests at most: the geocoding API turns a place name into coordinates (skipped when
coordinates are given), the forecast API returns hourly and daily values in the place's own
local time. The same forecast endpoint also serves up to 92 past days, so the weather of a
recent run can be looked up too.

Sources: https://open-meteo.com/en/docs and https://open-meteo.com/en/docs/geocoding-api.
Weather codes are WMO codes, turned into short English words here (the agent translates).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

import httpx
from pydantic_settings import BaseSettings

from thresy.config import SETTINGS_CONFIG

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
TIMEOUT_S = 15.0
MAX_FORECAST_DAYS = 16
MAX_PAST_DAYS = 92

HOURLY_FIELDS = (
    "temperature_2m",
    "apparent_temperature",
    "precipitation",
    "precipitation_probability",
    "weather_code",
    "wind_speed_10m",
    "wind_gusts_10m",
)
DAILY_FIELDS = (
    "weather_code",
    "temperature_2m_min",
    "temperature_2m_max",
    "precipitation_sum",
    "precipitation_probability_max",
    "precipitation_hours",
    "wind_speed_10m_max",
    "wind_gusts_10m_max",
    "sunrise",
    "sunset",
)

WMO_CODES = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "dense drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "heavy showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm, hail",
    99: "thunderstorm, hail",
}


class WeatherSettings(BaseSettings):
    """``COACH_HOME_LOCATION``: where the athlete usually runs, as a place name
    (``Springfield`` or ``Springfield, Illinois``) or as ``lat,lon``."""

    model_config = SETTINGS_CONFIG

    home_location: str | None = None


class WeatherError(RuntimeError):
    """The place or the forecast could not be fetched; the message says what to try."""


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float
    region: str | None = None
    country: str | None = None
    elevation_m: float | None = None
    query: str | None = None
    alternatives: tuple[str, ...] = ()
    """Other places with the same name (for the agent to notice a wrong match)."""

    def label(self) -> str:
        where = ", ".join(p for p in (self.region, self.country) if p)
        coords = f"{self.latitude:.3f}, {self.longitude:.3f}"
        return f"{self.name} ({where + '; ' if where else ''}{coords})"


@dataclass
class Forecast:
    place: Place
    timezone: str
    timezone_abbreviation: str
    elevation_m: float | None
    daily: list[dict[str, Any]] = field(default_factory=list)
    hourly: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_COORDS = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """``"48.1, 11.5"`` -> ``(48.1, 11.5)``; ``None`` if the text is not a coordinate pair."""
    match = _COORDS.match(text)
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise WeatherError(f"coordinates out of range: {text!r} (latitude first, then longitude)")
    return lat, lon


def describe(code: Any) -> str:
    if code is None:
        return "-"
    try:
        return WMO_CODES.get(int(code), f"code {int(code)}")
    except (TypeError, ValueError):
        return "-"


def _fold(text: str | None) -> str:
    """Lower case without accents: ``Württemberg`` and ``Wurttemberg`` compare equal."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _get(client: httpx.Client, url: str, params: dict[str, Any], what: str) -> dict[str, Any]:
    try:
        response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise WeatherError(f"the weather service is unreachable ({what}): {exc}") from exc
    if response.status_code != 200:
        reason = ""
        try:
            reason = str(response.json().get("reason") or "")
        except ValueError:
            reason = response.text[:200]
        raise WeatherError(
            f"the weather service answered HTTP {response.status_code} ({what})"
            + (f": {reason}" if reason else "")
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise WeatherError(f"the weather service sent something that is not JSON ({what})") from exc
    if not isinstance(data, dict):
        raise WeatherError(f"unexpected answer from the weather service ({what})")
    return data


def _region(result: dict[str, Any]) -> str | None:
    for key in ("admin1", "admin2", "admin3"):
        if result.get(key):
            return str(result[key])
    return None


def _place_line(result: dict[str, Any]) -> str:
    bits = [str(result.get("name", "?"))]
    for key in ("admin4", "admin3", "admin1", "country_code"):
        if result.get(key):
            bits.append(str(result[key]))
    return ", ".join(bits)


def geocode(client: httpx.Client, text: str, *, country: str | None = None) -> Place:
    """Find a place by name. ``"Name, Qualifier"`` prefers a match whose region or country
    contains the qualifier (``"Springfield, Illinois"``)."""
    name, _, qualifier = text.partition(",")
    name, qualifier = name.strip(), qualifier.strip()
    if not name:
        raise WeatherError("empty place name")
    params: dict[str, Any] = {"name": name, "count": 10, "language": "en", "format": "json"}
    if country:
        params["countryCode"] = country.strip().upper()
    data = _get(client, GEOCODING_URL, params, "place search")
    results = [r for r in data.get("results") or [] if isinstance(r, dict)]
    if not results:
        hint = " or another --country" if country else ", add --country"
        raise WeatherError(
            f"no place called {name!r} found; try a larger town nearby{hint}, or pass"
            " coordinates as 'lat,lon'"
        )
    chosen = results[0]
    if qualifier:
        wanted = _fold(qualifier)
        keys = ("admin1", "admin2", "admin3", "admin4", "country", "country_code")
        matching = [r for r in results if any(wanted in _fold(str(r.get(k) or "")) for k in keys)]
        if matching:
            chosen = matching[0]
    others = tuple(_place_line(r) for r in results if r is not chosen)[:4]
    return Place(
        name=str(chosen.get("name", name)),
        latitude=float(chosen["latitude"]),
        longitude=float(chosen["longitude"]),
        region=_region(chosen),
        country=chosen.get("country_code") or chosen.get("country"),
        elevation_m=chosen.get("elevation"),
        query=text,
        alternatives=others,
    )


def resolve_place(client: httpx.Client, text: str, *, country: str | None = None) -> Place:
    coords = parse_coordinates(text)
    if coords:
        name = f"{coords[0]:.4f}, {coords[1]:.4f}"
        return Place(name=name, latitude=coords[0], longitude=coords[1], query=text)
    return geocode(client, text, country=country)


def _rows(block: dict[str, Any] | None, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    if not block or not isinstance(block.get("time"), list):
        return []
    rows = []
    for i, stamp in enumerate(block["time"]):
        row: dict[str, Any] = {"time": stamp}
        for name in fields:
            values = block.get(name)
            row[name] = values[i] if isinstance(values, list) and i < len(values) else None
        row["weather"] = describe(row.get("weather_code"))
        rows.append(row)
    return rows


def fetch_forecast(
    client: httpx.Client, place: Place, *, forecast_days: int, past_days: int = 0
) -> Forecast:
    """Hourly and daily values for ``past_days`` before today up to ``forecast_days`` (today
    included), in the place's local time."""
    if not 1 <= forecast_days <= MAX_FORECAST_DAYS:
        raise WeatherError(f"the forecast covers at most {MAX_FORECAST_DAYS} days ahead")
    if not 0 <= past_days <= MAX_PAST_DAYS:
        raise WeatherError(f"past weather goes back at most {MAX_PAST_DAYS} days")
    params: dict[str, Any] = {
        "latitude": round(place.latitude, 4),
        "longitude": round(place.longitude, 4),
        "timezone": "auto",
        "forecast_days": forecast_days,
        "hourly": ",".join(HOURLY_FIELDS),
        "daily": ",".join(DAILY_FIELDS),
        "wind_speed_unit": "kmh",
    }
    if past_days:
        params["past_days"] = past_days
    data = _get(client, FORECAST_URL, params, "forecast")
    return Forecast(
        place=place,
        timezone=str(data.get("timezone") or "UTC"),
        timezone_abbreviation=str(data.get("timezone_abbreviation") or ""),
        elevation_m=data.get("elevation"),
        daily=_rows(data.get("daily"), DAILY_FIELDS),
        hourly=_rows(data.get("hourly"), HOURLY_FIELDS),
    )


def make_client() -> httpx.Client:
    """The HTTP client the command uses (tests replace this with a mocked transport)."""
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": "thresy-coach"})


# Text output ----------------------------------------------------------------------------------


def _n(value: Any, digits: int = 0) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(header)]
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(header)).rstrip()]
    for r in rows:
        cells = [v.ljust(widths[i]) if i <= 1 else v.rjust(widths[i]) for i, v in enumerate(r)]
        out.append("  ".join(cells).rstrip())
    return out


def daily_rows(forecast: Forecast, first: date, last: date) -> list[dict[str, Any]]:
    return [r for r in forecast.daily if first <= date.fromisoformat(r["time"]) <= last]


def hourly_rows(
    forecast: Forecast, day: date, hours: tuple[int, int], *, not_before: datetime | None = None
) -> list[dict[str, Any]]:
    rows = []
    for r in forecast.hourly:
        moment = datetime.fromisoformat(r["time"])
        if moment.date() != day or not hours[0] <= moment.hour <= hours[1]:
            continue
        if not_before is not None and moment < not_before.replace(minute=0, second=0):
            continue
        rows.append(r)
    return rows


def format_text(
    forecast: Forecast,
    daily: list[dict[str, Any]],
    hourly_days: list[tuple[date, list[dict[str, Any]]]],
) -> str:
    place = forecast.place
    elevation = forecast.elevation_m if forecast.elevation_m is not None else place.elevation_m
    head = f"Weather for {place.label()}"
    if elevation is not None:
        head += f", {_n(elevation)} m"
    lines = [head, f"Open-Meteo; times are local ({forecast.timezone})."]
    if place.alternatives:
        lines.append("Other places with this name: " + "; ".join(place.alternatives))
    if daily:
        lines += [
            "",
            "Daily (whole days, today's hours already past included; plan with the hourly rows)",
        ]
        rows = []
        for r in daily:
            day = date.fromisoformat(r["time"])
            sun = "-"
            if r.get("sunrise") and r.get("sunset"):
                sun = f"{r['sunrise'][-5:]}-{r['sunset'][-5:]}"
            rows.append(
                [
                    day.strftime("%a %m-%d"),
                    r["weather"],
                    f"{_n(r.get('temperature_2m_min'))}/{_n(r.get('temperature_2m_max'))}",
                    _n(r.get("precipitation_sum"), 1),
                    _n(r.get("precipitation_probability_max")),
                    _n(r.get("precipitation_hours")),
                    f"{_n(r.get('wind_speed_10m_max'))}/{_n(r.get('wind_gusts_10m_max'))}",
                    sun,
                ]
            )
        header = ["day", "weather", "min/max °C", "rain mm", "rain %", "rain h"]
        lines += _table([*header, "wind/gust km/h", "daylight"], rows)
    for day, rows_of_day in hourly_days:
        title = f"Hourly, {day.strftime('%a %Y-%m-%d')}"
        if not rows_of_day:
            lines += ["", title, "(no hours left in the selected window)"]
            continue
        lines += ["", f"{title}, {rows_of_day[0]['time'][-5:]} to {rows_of_day[-1]['time'][-5:]}"]
        rows = [
            [
                r["time"][-5:],
                r["weather"],
                _n(r.get("temperature_2m")),
                _n(r.get("apparent_temperature")),
                _n(r.get("precipitation"), 1),
                _n(r.get("precipitation_probability")),
                f"{_n(r.get('wind_speed_10m'))}/{_n(r.get('wind_gusts_10m'))}",
            ]
            for r in rows_of_day
        ]
        lines += _table(
            ["time", "weather", "°C", "feels °C", "rain mm", "rain %", "wind/gust km/h"], rows
        )
    return "\n".join(lines)
