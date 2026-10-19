"""External data: Space-Track.org satellite catalogue, Open-Meteo clouds + geocoding.

The full Space-Track catalogue (free account; SPACETRACK_USER / SPACETRACK_PASS) is downloaded at most
once an hour and searched locally. Missing orbit IDs trigger a daily cached SATCAT batch for object
identification and recorded decay dates; unavailable elements never imply re-entry. Close approaches
come from a bundled CelesTrak SOCRATES snapshot. ORBITSIM_OFFLINE=1 uses only bundled data.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

PKG_DATA = Path(__file__).parent / "data"
USER_AGENT = "OrbitSimAI/0.1 (+https://github.com/; hackathon project)"

SPACETRACK = "https://www.space-track.org"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"

# Space-Track's OMM field types (it returns them as strings); also the shape of the bundled snapshot.
OMM_FIELDS = {
    "OBJECT_NAME": str, "OBJECT_ID": str, "EPOCH": str, "MEAN_MOTION": float, "ECCENTRICITY": float,
    "INCLINATION": float, "RA_OF_ASC_NODE": float, "ARG_OF_PERICENTER": float, "MEAN_ANOMALY": float,
    "EPHEMERIS_TYPE": int, "CLASSIFICATION_TYPE": str, "NORAD_CAT_ID": int, "ELEMENT_SET_NO": int,
    "REV_AT_EPOCH": int, "BSTAR": float, "MEAN_MOTION_DOT": float, "MEAN_MOTION_DDOT": float,
}
# Every on-orbit object with elements from the last 30 days, only the fields SGP4 needs.
CATALOGUE_QUERY = (
    "/basicspacedata/query/class/gp/decay_date/null-val/epoch/%3Enow-30"
    f"/predicates/{','.join(OMM_FIELDS)}/format/json"
)
CATALOGUE_TTL = 3600  # Space-Track asks for GP data at most once per hour
CATALOGUE_RETRY = 10 * 60
LOGIN_RETRY = 30 * 60  # Space-Track locks accounts after repeated failed logins
SATCAT_QUERY = "/basicspacedata/query/class/satcat/format/json"
SATCAT_TTL = 24 * 3600

WEATHER_TTL = 1800
GEOCODE_TTL = 30 * 24 * 3600

ALIASES = {
    "iss": 25544, "international space station": 25544, "space station": 25544, "zarya": 25544,
    "tiangong": 48274, "chinese space station": 48274, "css": 48274, "tianhe": 48274,
    "hubble": 20580, "hubble space telescope": 20580, "hst": 20580,
}

# Small offline gazetteer so the demo works without geocoding.
CITIES = {
    "london": ("London, United Kingdom", 51.5072, -0.1276, "Europe/London"),
    "seattle": ("Seattle, United States", 47.6062, -122.3321, "America/Los_Angeles"),
    "new york": ("New York, United States", 40.7128, -74.0060, "America/New_York"),
    "san francisco": ("San Francisco, United States", 37.7749, -122.4194, "America/Los_Angeles"),
    "los angeles": ("Los Angeles, United States", 34.0522, -118.2437, "America/Los_Angeles"),
    "paris": ("Paris, France", 48.8566, 2.3522, "Europe/Paris"),
    "berlin": ("Berlin, Germany", 52.52, 13.405, "Europe/Berlin"),
    "tokyo": ("Tokyo, Japan", 35.6762, 139.6503, "Asia/Tokyo"),
    "sydney": ("Sydney, Australia", -33.8688, 151.2093, "Australia/Sydney"),
    "bengaluru": ("Bengaluru, India", 12.9716, 77.5946, "Asia/Kolkata"),
    "toronto": ("Toronto, Canada", 43.6532, -79.3832, "America/Toronto"),
}

CATNR_RE = re.compile(r"^\s*(?:norad\s*)?#?\s*(\d{1,9})\s*$", re.I)
STATUS_FLAG_RE = re.compile(r"\s*\[[^\]]*\]\s*$")


def _clean_name(name: str) -> str:
    """SOCRATES appends operational-status flags like ' [+]' to names."""
    return STATUS_FLAG_RE.sub("", name).strip()


class DataError(RuntimeError):
    """User-facing data problem (unknown satellite, place not found...)."""


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    timezone: str = "UTC"

    def as_dict(self) -> dict:
        return {"name": self.name, "lat": self.lat, "lon": self.lon, "timezone": self.timezone}


def _env_flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def omm_from_spacetrack(rec: dict) -> dict:
    """Project a Space-Track `gp` record onto CelesTrak's OMM JSON shape and types."""
    out = {}
    for key, cast in OMM_FIELDS.items():
        val = rec.get(key)
        if val is None or val == "":
            raise ValueError(f"Space-Track record missing {key}")
        out[key] = cast(val)
    if "." not in out["EPOCH"]:
        out["EPOCH"] += ".000000"
    return out


class DataClient:
    def __init__(
        self,
        data_dir: str | os.PathLike | None = None,
        offline: bool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        spacetrack: tuple[str, str] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir or os.getenv("ORBITSIM_DATA_DIR", "./data"))
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.catalogue_path = self.data_dir / "catalogue.json"
        self.offline = _env_flag("ORBITSIM_OFFLINE") if offline is None else offline
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._fallback_gp: dict[int, dict] | None = None
        if spacetrack is None:
            spacetrack = (os.getenv("SPACETRACK_USER", "").strip(), os.getenv("SPACETRACK_PASS", ""))
        self._st_creds = spacetrack if all(spacetrack) else None
        if not self.offline and self._st_creds is None:
            log.warning("SPACETRACK_USER / SPACETRACK_PASS not set: only the bundled satellites are available")
        self._cat: dict[int, dict] | None = None
        self._cat_mtime = 0.0
        self._cat_lock = asyncio.Lock()
        self._cat_task: asyncio.Task | None = None
        self._next_download = 0.0
        self._next_login = 0.0
        self._next_satcat_download = 0.0

    # ------------------------------------------------------------------ plumbing
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=8.0),
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._cat_task is not None and not self._cat_task.done():
            self._cat_task.cancel()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _cache_path(self, key: str, ext: str = "json") -> Path:
        return self.cache_dir / f"{hashlib.sha1(key.encode()).hexdigest()[:20]}.{ext}"

    def _cache_get(self, key: str, ttl: float):
        p = self._cache_path(key)
        if p.exists() and time.time() - p.stat().st_mtime < ttl:
            try:
                return json.loads(p.read_text("utf-8"))
            except (OSError, ValueError):
                return None
        return None

    def _cache_get_stale(self, key: str):
        p = self._cache_path(key)
        if p.exists():
            try:
                return json.loads(p.read_text("utf-8"))
            except (OSError, ValueError):
                return None
        return None

    def _cache_put(self, key: str, value) -> None:
        p = self._cache_path(key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(value), "utf-8")
        os.replace(tmp, p)

    def _fallback(self) -> dict[int, dict]:
        if self._fallback_gp is None:
            raw = json.loads((PKG_DATA / "fallback_gp.json").read_text("utf-8"))
            seen: dict[int, dict] = {}
            for group in ("stations", "visual"):
                for rec in raw.get(group, []):
                    seen.setdefault(int(rec["NORAD_CAT_ID"]), rec)
            for recs in raw.get("catnr", {}).values():
                for rec in recs if isinstance(recs, list) else [recs]:
                    seen.setdefault(int(rec["NORAD_CAT_ID"]), rec)
            self._fallback_gp = seen
        return self._fallback_gp

    # ------------------------------------------------------------------ Space-Track catalogue
    @property
    def catalogue_enabled(self) -> bool:
        return not self.offline and self._st_creds is not None

    def catalogue_status(self) -> dict:
        if not self.catalogue_enabled:
            return {"source": "bundled snapshot", "objects": len(self._fallback())}
        if not self.catalogue_path.exists():
            return {"source": "Space-Track.org", "objects": 0, "downloaded": None}
        cat = self._load_catalogue()
        mtime = self.catalogue_path.stat().st_mtime
        return {
            "source": "Space-Track.org",
            "objects": len(cat or {}),
            "downloaded": datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        }

    async def _login(self) -> bool:
        if time.time() < self._next_login:
            return False
        user, password = self._st_creds
        r = await self._http().post(f"{SPACETRACK}/ajaxauth/login", data={"identity": user, "password": password})
        if r.status_code == 200 and "failed" not in r.text.lower():
            return True
        log.warning("Space-Track login rejected (HTTP %s); check SPACETRACK_USER / SPACETRACK_PASS", r.status_code)
        self._next_download = time.time() + LOGIN_RETRY
        self._next_login = self._next_download
        return False

    async def refresh_catalogue(self) -> None:
        """Download every on-orbit object's current elements in one query (Space-Track asks for <= 1/hour)."""
        if not self.catalogue_enabled:
            return
        async with self._cat_lock:
            p = self.catalogue_path
            if p.exists() and time.time() - p.stat().st_mtime < CATALOGUE_TTL:
                return
            if time.time() < self._next_download:
                return
            self._next_download = time.time() + CATALOGUE_RETRY
            try:
                if not await self._login():
                    return
                r = await self._http().get(SPACETRACK + CATALOGUE_QUERY, timeout=httpx.Timeout(180.0, connect=10.0))
                r.raise_for_status()
                recs = []
                for raw in r.json():
                    try:
                        recs.append(omm_from_spacetrack(raw))
                    except (ValueError, TypeError):
                        continue
                if not recs:
                    raise ValueError("empty catalogue")
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps(recs), "utf-8")
                os.replace(tmp, p)
                self._next_download = time.time() + CATALOGUE_TTL
                log.info("Space-Track catalogue downloaded: %d objects", len(recs))
            except (httpx.HTTPError, ValueError, TypeError) as e:
                log.warning("Space-Track catalogue download failed: %s", type(e).__name__)

    def _load_catalogue(self) -> dict[int, dict] | None:
        p = self.catalogue_path
        if not p.exists():
            return None
        mtime = p.stat().st_mtime
        if self._cat is None or mtime != self._cat_mtime:
            try:
                self._cat = {r["NORAD_CAT_ID"]: r for r in json.loads(p.read_text("utf-8"))}
                self._cat_mtime = mtime
            except (OSError, ValueError, KeyError, TypeError):
                return self._cat
        return self._cat

    async def _catalogue(self, wait: bool) -> dict[int, dict] | None:
        """Current catalogue by NORAD id, refreshed in the background once it is an hour old."""
        if not self.catalogue_enabled:
            return None
        p = self.catalogue_path
        stale = not p.exists() or time.time() - p.stat().st_mtime >= CATALOGUE_TTL
        if stale and time.time() >= self._next_download:
            if wait and not p.exists():
                await self.refresh_catalogue()
            elif self._cat_task is None or self._cat_task.done():
                self._cat_task = asyncio.create_task(self.refresh_catalogue())
        return await asyncio.to_thread(self._load_catalogue)

    async def _satcat(self) -> dict[int, dict] | None:
        """Fetch SATCAT in one daily batch, including objects without current elements."""
        if not self.catalogue_enabled:
            return None
        async with self._cat_lock:
            cached = self._cache_get("satcat", SATCAT_TTL)
            if cached is not None:
                return {int(r["NORAD_CAT_ID"]): r for r in cached}
            if time.time() < self._next_satcat_download:
                return None
            self._next_satcat_download = time.time() + CATALOGUE_RETRY
            try:
                if not await self._login():
                    return None
                response = await self._http().get(
                    SPACETRACK + SATCAT_QUERY, timeout=httpx.Timeout(180.0, connect=10.0)
                )
                response.raise_for_status()
                records = response.json()
                if not isinstance(records, list) or not records:
                    raise ValueError("empty or invalid SATCAT response")
                catalogue = {}
                for record in records:
                    if not isinstance(record, dict):
                        raise ValueError("invalid SATCAT record")
                    norad = int(record["NORAD_CAT_ID"])
                    if not (record.get("SATNAME") or record.get("OBJECT_NAME")):
                        raise ValueError("SATCAT record missing object name")
                    catalogue[norad] = record
                self._cache_put("satcat", records)
                log.info("Space-Track SATCAT downloaded: %d objects", len(catalogue))
                return catalogue
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
                log.warning("Space-Track SATCAT lookup failed: %s", type(error).__name__)
                return None

    async def _missing_orbit(self, norad: int, answered: bool) -> str:
        catalogue = await self._satcat()
        record = (catalogue or {}).get(norad)
        if record:
            name = record.get("SATNAME") or record["OBJECT_NAME"]
            identity = f"NORAD {norad} is {name}."
            decay = record.get("DECAY") or record.get("DECAY_DATE")
            if decay and str(decay).strip().lower() not in {"null", "none"}:
                return (f"{identity} Space-Track records a decay date of {decay}. "
                        "There is no current orbit to display in this Earth-orbit viewer.")
            # Only use an orbit center if the source explicitly supplies it.
            center = str(record.get("ORBIT_CENTER") or "").strip().upper()
            if center and center != "EA":
                centers = {"SU": "the Sun", "EL1": "Sun-Earth L1", "EL2": "Sun-Earth L2", "MO": "the Moon"}
                return (f"{identity} Its recorded orbit center is {centers.get(center, center)}, not Earth. "
                        "This Earth-orbit viewer cannot visualize that trajectory.")
            return (f"{identity} No public Earth-orbit elements from the last 30 days are available "
                    "in our catalogue, so it cannot be visualized. "
                    "The available Space-Track metadata does not establish why.")
        if catalogue is not None:
            return f"No Space-Track catalogue identification or current orbit is available for NORAD {norad}."
        return self._not_found(
            f"NORAD {norad}",
            f"No current orbit data for NORAD {norad}. The Space-Track identification lookup is unavailable; "
            "I cannot confirm its name or status.",
            answered,
        )

    def _not_found(self, what: str, live_msg: str, answered: bool) -> str:
        if answered:
            return live_msg
        return (f"I can't reach the live satellite catalogue right now, and {what} isn't in my offline set. "
                "The ISS, Tiangong and Hubble still work — try again later.")

    async def resolve_satellite(self, query: str | int) -> dict:
        """Accepts a NORAD id, a friendly alias ('ISS', 'Hubble') or an object name fragment."""
        q = str(query).strip()
        if not q:
            raise DataError("Please name a satellite, for example 'ISS' or 'Hubble'.")
        if len(q) > 80:
            raise DataError("Satellite name is too long.")
        catnr = ALIASES.get(q.lower())
        m = CATNR_RE.match(q)
        if catnr is None and m:
            catnr = int(m.group(1))
        fallback = self._fallback()
        if catnr is not None:
            # Bundled objects never wait for the first catalogue download.
            cat = await self._catalogue(wait=catnr not in fallback)
            rec = (cat or {}).get(catnr) or fallback.get(catnr)
            if rec:
                return rec
            raise DataError(await self._missing_orbit(catnr, cat is not None or self.offline))
        if not re.fullmatch(r"[\w\s\-()/.+]+", q):
            raise DataError("Satellite names may only contain letters, numbers, spaces and - ( ) / . +")
        cat = await self._catalogue(wait=True)
        ql = q.lower()
        recs = [r for r in (cat or fallback).values() if ql in r["OBJECT_NAME"].lower()]
        if not recs:
            raise DataError(self._not_found(f"'{q}'", f"I couldn't find a satellite called '{q}'.", cat is not None or self.offline))
        recs.sort(key=lambda r: (r["OBJECT_NAME"].lower() != ql, " DEB" in r["OBJECT_NAME"], " R/B" in r["OBJECT_NAME"], len(r["OBJECT_NAME"])))
        return recs[0]

    # ------------------------------------------------------------------ SOCRATES (bundled snapshot)
    @staticmethod
    def _parse_socrates(text_iter, norad: int | None, limit: int) -> list[dict]:
        out = []
        reader = csv.DictReader(text_iter)
        for row in reader:
            if norad is not None and norad not in (int(row["NORAD_CAT_ID_1"]), int(row["NORAD_CAT_ID_2"])):
                continue
            out.append(
                {
                    "object_1": {"norad": int(row["NORAD_CAT_ID_1"]), "name": _clean_name(row["OBJECT_NAME_1"])},
                    "object_2": {"norad": int(row["NORAD_CAT_ID_2"]), "name": _clean_name(row["OBJECT_NAME_2"])},
                    "tca": row["TCA"].strip().replace(" ", "T") + "Z",
                    "min_range_km": float(row["TCA_RANGE"]),
                    "relative_speed_km_s": float(row["TCA_RELATIVE_SPEED"]),
                    "max_probability": float(row["MAX_PROB"]),
                }
            )
            if len(out) >= limit:
                break
        return out

    async def get_conjunctions(self, norad: int | None, limit: int = 5) -> tuple[list[dict], str]:
        """Returns (events sorted by min range, source label) from the bundled SOCRATES snapshot."""
        with open(PKG_DATA / "fallback_socrates.csv", newline="", encoding="utf-8") as f:
            return self._parse_socrates(f, norad, limit), "CelesTrak SOCRATES snapshot (bundled)"

    # ------------------------------------------------------------------ Open-Meteo
    async def geocode(self, name: str) -> Place:
        q = name.strip()
        if not q or len(q) > 100:
            raise DataError("Please tell me a town or city name.")
        city = CITIES.get(q.lower().split(",")[0].strip())
        key = f"geo:{q.lower()}"
        cached = self._cache_get(key, GEOCODE_TTL)
        if cached:
            return Place(**cached)
        if not self.offline:
            try:
                r = await self._http().get(
                    OPEN_METEO_GEOCODE, params={"name": q.split(",")[0].strip(), "count": 1, "language": "en", "format": "json"}
                )
                if r.status_code == 200 and r.json().get("results"):
                    g = r.json()["results"][0]
                    parts = [g.get("name"), g.get("admin1"), g.get("country")]
                    label = ", ".join(dict.fromkeys(x for x in parts if x))
                    place = Place(label, round(g["latitude"], 4), round(g["longitude"], 4), g.get("timezone") or "UTC")
                    self._cache_put(key, place.as_dict())
                    return place
            except (httpx.HTTPError, ValueError, KeyError):
                pass
        if city:
            return Place(*city)
        raise DataError(f"I couldn't find a place called '{q}'.")

    async def cloud_cover(self, lat: float, lon: float) -> dict[str, int] | None:
        """Hourly cloud cover % keyed by 'YYYY-MM-DDTHH' (UTC) for ~3 days, or None if unavailable."""
        key = f"wx:{lat:.2f},{lon:.2f}"
        cached = self._cache_get(key, WEATHER_TTL)
        if cached is not None:
            return cached
        if self.offline:
            return None
        try:
            r = await self._http().get(
                OPEN_METEO_FORECAST,
                params={"latitude": f"{lat:.3f}", "longitude": f"{lon:.3f}", "hourly": "cloud_cover", "forecast_days": 4, "timezone": "UTC"},
            )
            r.raise_for_status()
            h = r.json()["hourly"]
            data = {t[:13]: c for t, c in zip(h["time"], h["cloud_cover"]) if c is not None}
            self._cache_put(key, data)
            return data
        except (httpx.HTTPError, ValueError, KeyError):
            return self._cache_get_stale(key)


def parse_socrates_text(text: str, norad: int | None = None, limit: int = 5) -> list[dict]:
    """Helper for tests."""
    return DataClient._parse_socrates(io.StringIO(text), norad, limit)
