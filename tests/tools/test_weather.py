"""coach weather: place lookup, forecast request and output, with a mocked HTTP transport.

No test reaches the network: every request goes to the handler below, which answers with
synthetic data for the requested days.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import pytest

from thresy.tools import weather
from thresy.tools.dates import local_today

from .test_send import coach

PLACES = [
    {
        "name": "Springfield",
        "latitude": 10.5,
        "longitude": 20.25,
        "elevation": 180.0,
        "country_code": "XA",
        "country": "Exampleland",
        "admin1": "North Province",
        "admin4": "Lakeside",
    },
    {
        "name": "Springfield",
        "latitude": 11.0,
        "longitude": 21.0,
        "elevation": 300.0,
        "country_code": "XA",
        "country": "Exampleland",
        "admin1": "Südmark",
        "admin4": "Hillside",
    },
]


class FakeOpenMeteo:
    """Answers the geocoding and forecast endpoints like Open-Meteo, with made-up values."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.places = PLACES
        self.forecast_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host.startswith("geocoding"):
            name = request.url.params["name"]
            results = [p for p in self.places if p["name"] == name]
            return httpx.Response(200, json={"results": results} if results else {})
        if self.forecast_status != 200:
            return httpx.Response(self.forecast_status, json={"error": True, "reason": "nope"})
        return httpx.Response(200, json=self.forecast(request))

    def forecast(self, request: httpx.Request) -> dict[str, Any]:
        params = request.url.params
        days = int(params["forecast_days"])
        past = int(params.get("past_days", 0))
        first = local_today() - timedelta(days=past)
        dates = [first + timedelta(days=i) for i in range(past + days)]
        hours = [datetime(d.year, d.month, d.day, h) for d in dates for h in range(24)]
        return {
            "latitude": float(params["latitude"]),
            "longitude": float(params["longitude"]),
            "timezone": "UTC",
            "timezone_abbreviation": "GMT",
            "elevation": 181.0,
            "hourly": {
                "time": [h.strftime("%Y-%m-%dT%H:%M") for h in hours],
                "temperature_2m": [float(h.hour) for h in hours],
                "apparent_temperature": [float(h.hour) - 2 for h in hours],
                "precipitation": [0.5 if h.hour == 12 else 0.0 for h in hours],
                "precipitation_probability": [40 if h.hour == 12 else 5 for h in hours],
                "weather_code": [61 if h.hour == 12 else 3 for h in hours],
                "wind_speed_10m": [12.0 for _ in hours],
                "wind_gusts_10m": [30.0 for _ in hours],
            },
            "daily": {
                "time": [d.isoformat() for d in dates],
                "weather_code": [80 for _ in dates],
                "temperature_2m_min": [1.0 for _ in dates],
                "temperature_2m_max": [9.0 for _ in dates],
                "precipitation_sum": [2.5 for _ in dates],
                "precipitation_probability_max": [60 for _ in dates],
                "precipitation_hours": [3.0 for _ in dates],
                "wind_speed_10m_max": [20.0 for _ in dates],
                "wind_gusts_10m_max": [45.0 for _ in dates],
                "sunrise": [f"{d.isoformat()}T07:30" for d in dates],
                "sunset": [f"{d.isoformat()}T16:40" for d in dates],
            },
        }

    def forecast_params(self) -> httpx.QueryParams:
        return next(r.url.params for r in self.requests if "forecast" in r.url.path)


@pytest.fixture
def fake(monkeypatch) -> FakeOpenMeteo:
    service = FakeOpenMeteo()
    monkeypatch.setattr(
        weather, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(service))
    )
    return service


@pytest.fixture
def env(tmp_path, monkeypatch) -> dict[str, str]:
    monkeypatch.chdir(tmp_path)  # no .env file
    return {"COACH_HOME_LOCATION": "Springfield", "COACH_TZ": "UTC"}


# Library ---------------------------------------------------------------------------------------


def test_parse_coordinates() -> None:
    assert weather.parse_coordinates("10.5, 20.25") == (10.5, 20.25)
    assert weather.parse_coordinates("-33.9 18.4") == (-33.9, 18.4)
    assert weather.parse_coordinates("Springfield") is None
    with pytest.raises(weather.WeatherError, match="out of range"):
        weather.parse_coordinates("100, 20")


def test_geocode_prefers_the_qualifier_ignoring_accents(fake: FakeOpenMeteo) -> None:
    with weather.make_client() as client:
        first = weather.geocode(client, "Springfield")
        south = weather.geocode(client, "Springfield, Sudmark")
        hill = weather.geocode(client, "Springfield, hillside")
    assert (first.latitude, first.region) == (10.5, "North Province")
    assert "Hillside" in first.alternatives[0]
    assert south.latitude == 11.0 and hill.latitude == 11.0
    assert fake.requests[0].url.params["name"] == "Springfield"


def test_geocode_without_result(fake: FakeOpenMeteo) -> None:
    with weather.make_client() as client, pytest.raises(weather.WeatherError, match="no place"):
        weather.geocode(client, "Nowhere", country="xa")
    assert fake.requests[0].url.params["countryCode"] == "XA"


def test_forecast_error_is_a_clean_message(fake: FakeOpenMeteo) -> None:
    fake.forecast_status = 400
    place = weather.Place(name="x", latitude=1, longitude=2)
    with weather.make_client() as client, pytest.raises(weather.WeatherError, match="HTTP 400"):
        weather.fetch_forecast(client, place, forecast_days=3)


def test_unreachable_service(monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    with pytest.raises(weather.WeatherError, match="unreachable"):
        weather.geocode(client, "Springfield")


def day(**values: Any) -> dict[str, Any]:
    row = {
        "time": "2030-01-02",
        "weather_code": 1,
        "temperature_2m_min": 3.0,
        "temperature_2m_max": 12.0,
        "precipitation_sum": 0.0,
        "precipitation_probability_max": 10,
        "precipitation_hours": 0.0,
        "wind_speed_10m_max": 8.0,
        "wind_gusts_10m_max": 15.0,
    }
    row.update(values)
    return row


def test_day_summary_names_showers_and_gusts() -> None:
    # Showers and strong gusts in a row of numbers are easy to read past: the words name them.
    stormy = day(
        weather_code=81,
        temperature_2m_min=2.0,
        temperature_2m_max=8.0,
        precipitation_sum=4.2,
        precipitation_probability_max=80,
        precipitation_hours=5.0,
        wind_speed_10m_max=30.0,
        wind_gusts_10m_max=55.0,
    )
    assert weather.day_summary(stormy) == (
        "showers; rain (4.2 mm in 5 h, chance 80%); windy (wind up to 30, gusts up to 55 km/h);"
        " 2 to 8 °C"
    )
    assert weather.day_summary(day()) == (
        "mainly clear; dry (0.0 mm, chance 10%); little wind (wind up to 8, gusts up to 15 km/h);"
        " 3 to 12 °C"
    )


@pytest.mark.parametrize(
    ("values", "words"),
    [
        ({"precipitation_sum": 0.1, "precipitation_probability_max": 40}, "rain possible"),
        ({"precipitation_sum": 0.1, "weather_code": 80}, "rain possible"),
        ({"precipitation_sum": 0.5}, "a little rain"),
        ({"precipitation_sum": 1.0}, "rain"),
        ({"precipitation_sum": 12.0}, "heavy rain"),
        ({"precipitation_sum": 3.0, "weather_code": 73}, "snow"),
        ({"precipitation_sum": None, "precipitation_probability_max": 5}, "probably dry"),
        ({"precipitation_sum": None, "precipitation_probability_max": None}, "rain: no data"),
    ],
)
def test_rain_thresholds(values: dict[str, Any], words: str) -> None:
    assert weather.rain_words(day(**values)).split(" (")[0] == words


@pytest.mark.parametrize(
    ("wind", "gusts", "words"),
    [
        (19.0, 29.0, "little wind"),
        (20.0, 25.0, "breezy"),
        (10.0, 30.0, "breezy"),
        (25.0, 50.0, "windy"),
        (40.0, 45.0, "windy"),
        (30.0, 75.0, "stormy"),
        (None, None, "wind: no data"),
    ],
)
def test_wind_thresholds(wind: float | None, gusts: float | None, words: str) -> None:
    row = day(wind_speed_10m_max=wind, wind_gusts_10m_max=gusts)
    assert weather.wind_words(row).split(" (")[0] == words


def test_temperature_words() -> None:
    assert weather.temperature_words(day(temperature_2m_min=-2.0)) == "-2 to 12 °C, frost"
    assert weather.temperature_words(day(temperature_2m_max=26.0)) == "3 to 26 °C, warm"
    assert weather.temperature_words(day(temperature_2m_max=31.0)) == "3 to 31 °C, hot"


def test_describe_codes() -> None:
    assert weather.describe(0) == "clear"
    assert weather.describe(65) == "heavy rain"
    assert weather.describe(None) == "-"
    assert weather.describe(42) == "code 42"


# Command ---------------------------------------------------------------------------------------


def test_default_uses_home_location(fake: FakeOpenMeteo, env: dict[str, str]) -> None:
    result = coach(["weather", "--days", "3"], env)
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.startswith("Weather for Springfield (North Province, XA; 10.500, 20.250)")
    assert "Other places with this name: Springfield, Hillside" in out
    assert "Daily" in out and "light showers" in out and "1/9" in out
    # A summary in words for every day, ahead of the table it is built from.
    summary = out.index("Daily summary")
    assert summary < out.index("\nDaily (whole days")
    today = local_today()
    line = (
        f"{today.strftime('%a %m-%d')}: light showers; rain (2.5 mm in 3 h, chance 60%); "
        "breezy (wind up to 20, gusts up to 45 km/h); 1 to 9 °C"
    )
    assert line in out[summary : out.index("\nDaily (whole days")]
    tomorrow = local_today() + timedelta(days=1)
    assert f"Hourly, {tomorrow.strftime('%a %Y-%m-%d')}" in out
    params = fake.forecast_params()
    assert params["forecast_days"] == "3" and "past_days" not in params
    assert params["timezone"] == "auto"
    assert params["latitude"] == "10.5"


def test_hourly_window_and_explicit_dates(fake: FakeOpenMeteo, env: dict[str, str]) -> None:
    day = local_today() + timedelta(days=9)
    result = coach(["weather", "--date", day.isoformat(), "--hours", "11-13", "--days", "2"], env)
    assert result.exit_code == 0, result.output
    heading = f"Hourly, {day.strftime('%a %Y-%m-%d')}, 11:00 to 13:00"
    assert heading in result.output
    hourly = result.output.split(heading)[1].splitlines()
    rows = [line for line in hourly if line[:2].isdigit()]
    assert [r[:5] for r in rows] == ["11:00", "12:00", "13:00"]
    assert "light rain" in rows[1] and "0.5" in rows[1]
    assert fake.forecast_params()["forecast_days"] == "10"


def test_past_day_uses_past_days(fake: FakeOpenMeteo, env: dict[str, str]) -> None:
    day = local_today() - timedelta(days=5)
    result = coach(["weather", "--date", day.isoformat(), "--json"], env)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert fake.forecast_params()["past_days"] == "5"
    assert len(data["hourly"][day.isoformat()]) == 17  # 06:00-22:00
    assert data["daily"][0]["time"] == local_today().isoformat()
    assert data["daily"][0]["summary"].startswith("light showers; rain (2.5 mm")
    assert data["daily"][0]["precipitation_sum"] == 2.5  # the numbers are unchanged
    assert len(data["daily"]) == 7
    assert data["place"]["name"] == "Springfield"


def test_place_argument_and_coordinates(fake: FakeOpenMeteo, env: dict[str, str]) -> None:
    result = coach(["weather", "11.25,22.5", "--daily-only"], env)
    assert result.exit_code == 0, result.output
    assert "Hourly" not in result.output
    assert all("geocoding" not in r.url.host for r in fake.requests)
    assert fake.forecast_params()["longitude"] == "22.5"


def test_missing_place(fake: FakeOpenMeteo, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = coach(["weather"], {"COACH_TZ": "UTC"})
    assert result.exit_code == 1
    assert "COACH_HOME_LOCATION is not set" in result.output
    assert fake.requests == []


def test_bad_options(fake: FakeOpenMeteo, env: dict[str, str]) -> None:
    assert "--hours takes a range" in coach(["weather", "--hours", "evening"], env).output
    assert "not a date" in coach(["weather", "--date", "someday"], env).output
    far = (local_today() + timedelta(days=30)).isoformat()
    result = coach(["weather", "--date", far, "--json"], env)
    assert result.exit_code == 1 and "at most 16 days" in json.loads(result.output)["error"]


def test_unknown_place_json(fake: FakeOpenMeteo, env: dict[str, str]) -> None:
    result = coach(["weather", "Atlantis", "--json"], env)
    assert result.exit_code == 1
    assert "no place called 'Atlantis'" in json.loads(result.output)["error"]


def test_hourly_rows_filters_by_day_and_hour() -> None:
    today = date(2030, 1, 2)
    rows = [{"time": f"2030-01-0{d}T{h:02d}:00"} for d in (1, 2, 3) for h in range(24)]
    forecast = weather.Forecast(
        place=weather.Place(name="x", latitude=0, longitude=0),
        timezone="UTC",
        timezone_abbreviation="UTC",
        elevation_m=None,
        hourly=rows,
    )
    picked = weather.hourly_rows(forecast, today, (6, 8))
    assert [r["time"][-5:] for r in picked] == ["06:00", "07:00", "08:00"]
    later = weather.hourly_rows(forecast, today, (6, 22), not_before=datetime(2030, 1, 2, 20, 30))
    assert [r["time"][-5:] for r in later] == ["20:00", "21:00", "22:00"]
