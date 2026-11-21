"""The routing interface behind ``coach route plan``, and openrouteservice as its first provider.

Nothing outside this module knows the provider. A provider turns a :class:`RouteQuery` (the
points to pass, or one point and a length for a round trip) into :class:`RoutedPath` objects:
the geometry with elevation, the provider's own distance and ascent, and surface and way type
shares where the provider has them.

openrouteservice (https://giscience.github.io/openrouteservice/api-reference/): one request is
``POST <base>/v2/directions/<profile>/geojson`` with a JSON body; the key goes in the
``Authorization`` header (never in the URL). The hosted service is
``https://api.heigit.org/openrouteservice``; a self-hosted instance is reached by its own base
URL and needs no key. Used from the API:

* profiles ``foot-walking`` (default) and ``foot-hiking`` (prefers paths and trails);
* ``elevation: true``: coordinates come as ``[lon, lat, ele]``;
* ``extra_info: ["surface", "waytype"]``: ``properties.extras.<name>.summary`` is a list of
  ``{value, distance, amount}`` with ``amount`` in percent of the route;
* ``options.round_trip {length, points, seed}`` with exactly one coordinate; the length is "a
  preferred value, but results may be different", so the planner checks and retries;
* ``alternative_routes {target_count, share_factor, weight_factor}`` for two coordinates;
* ``options.avoid_features: ["steps"]``;
* ``radiuses``: how far (m) a point may be from the network; the service default is 350 m.

Hosted limits (docs, "Hosted API restrictions"): 50 waypoints, 100 km for round trips and
alternative routes, at most 3 alternatives. HTTP 403 means the daily quota is used up (or the
key is refused), 429 the per-minute limit. Errors come as ``{"error": {"code", "message"}}``
(routing errors such as 2010 "point not found") or ``{"error": "<text>"}`` (the gateway).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import numpy as np

from thresy import __version__, logs
from thresy.tools.route import RouteError, RouteSettings, Track

log = logging.getLogger(__name__)

TIMEOUT_S = 30.0
USER_AGENT = f"Thresy/{__version__} (self-hosted running coach)"
ORS_ATTRIBUTION = "© openrouteservice.org by HeiGIT | Map data © OpenStreetMap contributors"
ORS_MAX_WAYPOINTS = 50
ORS_MAX_ROUND_TRIP_M = 100_000.0
ORS_MAX_ALTERNATIVES = 3
ORS_DEFAULT_RADIUS_M = 350.0

SURFACE = {
    0: "unknown",
    1: "paved",
    2: "unpaved",
    3: "asphalt",
    4: "concrete",
    5: "cobblestone",
    6: "metal",
    7: "wood",
    8: "compacted gravel",
    9: "fine gravel",
    10: "gravel",
    11: "dirt",
    12: "ground",
    13: "ice",
    14: "paving stones",
    15: "sand",
    16: "woodchips",
    17: "grass",
    18: "grass paver",
}
PAVED = frozenset({1, 3, 4, 5, 6, 7, 14, 18})
UNPAVED = frozenset({2, 8, 9, 10, 11, 12, 13, 15, 16, 17})
WAYTYPE = {
    0: "unknown",
    1: "main road",
    2: "road",
    3: "street",
    4: "path",
    5: "track",
    6: "cycleway",
    7: "footway",
    8: "steps",
    9: "ferry",
    10: "construction",
}


class RoutingError(RouteError):
    """The routing service could not give a route; ``kind`` says why."""

    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind
        """``quota``, ``rate_limit``, ``auth``, ``point``, ``no_route``, ``limit``,
        ``unavailable`` or ``other``."""


@dataclass(frozen=True)
class RouteQuery:
    points: tuple[tuple[float, float], ...]
    """(lat, lon) in order. One point with ``round_trip_m`` makes a round trip."""
    round_trip_m: float | None = None
    seed: int | None = None
    round_trip_points: int | None = None
    alternatives: int = 0
    """Extra routes wanted besides the best one (two points only)."""
    radiuses_m: tuple[float, ...] | None = None
    """Per point: how far it may be from the path network (``None``: the service default)."""
    trails: bool = False
    avoid_steps: bool = False


@dataclass
class RoutedPath:
    track: Track
    distance_m: float
    """The provider's own distance."""
    ascent_m: float | None = None
    descent_m: float | None = None
    surface: dict[str, float] | None = None
    """Share of the distance (0..1) by surface name; ``None``: not given."""
    waytype: dict[str, float] | None = None
    way_points: list[int] = field(default_factory=list)
    """Indices into the geometry of the points the route was asked to pass."""
    query: RouteQuery | None = None


class RoutingProvider(Protocol):
    name: str
    profile: str
    attribution: str
    requests: int
    """Requests sent so far."""

    def route(self, query: RouteQuery) -> list[RoutedPath]: ...


def make_client() -> httpx.Client:
    """The HTTP client for the routing service (tests replace this with a mocked transport)."""
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT})


def make_provider(settings: RouteSettings, client: httpx.Client) -> RoutingProvider:
    problem = settings.planning_problem()
    if problem:
        raise RouteError(problem)
    return OpenRouteService(client, settings.base_url, settings.api_key)


# openrouteservice ------------------------------------------------------------------------------


def _shares(extras: dict[str, Any], name: str, names: dict[int, str]) -> dict[str, float] | None:
    block = extras.get(name)
    if not isinstance(block, dict) or not isinstance(block.get("summary"), list):
        return None
    out: dict[str, float] = {}
    for item in block["summary"]:
        try:
            value = int(item["value"])
            amount = float(item["amount"]) / 100.0
        except (KeyError, TypeError, ValueError):
            continue
        label = names.get(value, f"type {value}")
        out[label] = out.get(label, 0.0) + amount
    return out or None


class OpenRouteService:
    name = "openrouteservice"

    def __init__(self, client: httpx.Client, base_url: str, api_key: str | None) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.attribution = ORS_ATTRIBUTION
        self.profile = "foot-walking"
        self.requests = 0
        if api_key:
            logs.register_secret(api_key)

    def body(self, query: RouteQuery) -> dict[str, Any]:
        """The request body for ``query`` (see the module docstring)."""
        body: dict[str, Any] = {
            "coordinates": [[round(lon, 6), round(lat, 6)] for lat, lon in query.points],
            "elevation": True,
            "extra_info": ["surface", "waytype"],
            "instructions": False,
            "preference": "recommended",
            "units": "m",
        }
        options: dict[str, Any] = {}
        if query.round_trip_m is not None:
            trip: dict[str, Any] = {"length": round(query.round_trip_m)}
            if query.round_trip_points:
                trip["points"] = query.round_trip_points
            if query.seed is not None:
                trip["seed"] = query.seed
            options["round_trip"] = trip
        if query.avoid_steps:
            options["avoid_features"] = ["steps"]
        if options:
            body["options"] = options
        if query.alternatives and len(query.points) == 2:
            body["alternative_routes"] = {
                "target_count": min(query.alternatives + 1, ORS_MAX_ALTERNATIVES),
                "share_factor": 0.6,
                "weight_factor": 1.6,
            }
        if query.radiuses_m is not None:
            body["radiuses"] = [round(r) for r in query.radiuses_m]
        return body

    def route(self, query: RouteQuery) -> list[RoutedPath]:
        if not query.points:
            raise RouteError("no points to route")
        if len(query.points) > ORS_MAX_WAYPOINTS:
            raise RoutingError(
                f"at most {ORS_MAX_WAYPOINTS} points per route (openrouteservice)", "limit"
            )
        if query.round_trip_m is not None and query.round_trip_m > ORS_MAX_ROUND_TRIP_M:
            raise RoutingError(
                f"round trips are limited to {ORS_MAX_ROUND_TRIP_M / 1000:.0f} km by"
                " openrouteservice",
                "limit",
            )
        profile = "foot-hiking" if query.trails else "foot-walking"
        self.profile = profile
        url = f"{self.base_url}/v2/directions/{profile}/geojson"
        headers = {"Accept": "application/geo+json, application/json"}
        if self.api_key:
            headers["Authorization"] = self.api_key
        self.requests += 1
        try:
            response = self.client.post(url, json=self.body(query), headers=headers)
        except httpx.HTTPError as exc:
            raise RoutingError(
                f"the routing service is unreachable ({type(exc).__name__}): check the network"
                " or COACH_ROUTE_URL",
                "unavailable",
            ) from exc
        if response.status_code != 200:
            raise self._error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise RoutingError("the routing service sent something that is not JSON") from exc
        paths = self._paths(data, query)
        if not paths:
            raise RoutingError("the routing service returned no route", "no_route")
        return paths

    def _paths(self, data: Any, query: RouteQuery) -> list[RoutedPath]:
        if isinstance(data, dict) and isinstance(data.get("metadata"), dict):
            attribution = data["metadata"].get("attribution")
            if isinstance(attribution, str) and attribution.strip():
                # The service's own line, with HeiGIT's credit kept in front of it.
                if "openrouteservice" in attribution and "HeiGIT" not in attribution:
                    self.attribution = ORS_ATTRIBUTION
                else:
                    self.attribution = attribution.strip()
        features = data.get("features") if isinstance(data, dict) else None
        out = []
        for feature in features or []:
            try:
                coords = feature["geometry"]["coordinates"]
                props = feature.get("properties") or {}
            except (KeyError, TypeError):
                continue
            if not isinstance(coords, list) or len(coords) < 2:
                continue
            lon = np.array([float(c[0]) for c in coords])
            lat = np.array([float(c[1]) for c in coords])
            ele = np.array([float(c[2]) if len(c) > 2 else np.nan for c in coords])
            summary = props.get("summary") or {}
            extras = props.get("extras") or {}
            out.append(
                RoutedPath(
                    track=Track(lat, lon, ele),
                    distance_m=float(summary.get("distance") or 0.0),
                    ascent_m=_float(props.get("ascent")),
                    descent_m=_float(props.get("descent")),
                    surface=_shares(extras, "surface", SURFACE),
                    waytype=_shares(extras, "waytype", WAYTYPE),
                    way_points=[int(i) for i in props.get("way_points") or []],
                    query=query,
                )
            )
        return out

    def _error(self, response: httpx.Response) -> RoutingError:
        status = response.status_code
        code, message = None, ""
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                message = str(error.get("message") or "")
            elif isinstance(error, str):
                message = error
            elif isinstance(data.get("message"), str):
                message = data["message"]
        if not message:
            message = " ".join(response.text.split())[:200]
        message = logs.redact(" ".join(message.split())[:300])
        detail = f" ({message})" if message else ""
        if status == 429:
            return RoutingError(
                "the routing service's per-minute limit is reached: wait a minute, then try"
                " again (fewer --candidates use fewer requests)" + detail,
                "rate_limit",
            )
        if status in (401, 403):
            if "quota" in message.lower() and "unauthori" not in message.lower():
                return RoutingError(
                    "the routing service's daily quota is used up; it renews 24 hours after the"
                    " first request of the day: try again later" + detail,
                    "quota",
                )
            return RoutingError(
                f"the routing service refused the request (HTTP {status}): the daily quota is"
                " used up or COACH_ROUTE_API_KEY is wrong" + detail,
                "auth",
            )
        if code == 2010:
            return RoutingError(
                "a point is too far from any path the routing service knows (it searches within"
                " 350 m): move it onto a path" + detail,
                "point",
            )
        if code in (2009, 2016):
            return RoutingError(
                "no route found between the points (water, private land or a closed area in"
                " between?)" + detail,
                "no_route",
            )
        if code == 2004 or status == 413:
            return RoutingError(
                "a limit of the routing service is exceeded (round trips and alternatives up to"
                " 100 km, 50 points)" + detail,
                "limit",
            )
        if status >= 500:
            return RoutingError(
                f"the routing service failed (HTTP {status}); try again later" + detail,
                "unavailable",
            )
        code_text = f", code {code}" if code is not None else ""
        return RoutingError(f"the routing service answered HTTP {status}{code_text}{detail}")


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
