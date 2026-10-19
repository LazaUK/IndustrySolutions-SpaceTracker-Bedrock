"""Orbital mechanics helpers: SGP4 propagation, frames, sun, visibility and pass prediction.

Accuracy target is "naked-eye sky-watching" (better than ~1 degree / a few seconds), which the
SGP4 + GMST + low-precision-sun approach comfortably meets. Not for operational use.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sgp4 import omm
from sgp4.api import Satrec, jday
from sgp4.propagation import gstime

WGS84_A = 6378.137  # km
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)
EARTH_RADIUS_KM = WGS84_A
AU_KM = 149_597_870.7

COMPASS16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
COMPASS_WORDS = {
    "N": "north", "NNE": "north-northeast", "NE": "northeast", "ENE": "east-northeast",
    "E": "east", "ESE": "east-southeast", "SE": "southeast", "SSE": "south-southeast",
    "S": "south", "SSW": "south-southwest", "SW": "southwest", "WSW": "west-southwest",
    "W": "west", "WNW": "west-northwest", "NW": "northwest", "NNW": "north-northwest",
}


class PropagationError(RuntimeError):
    pass


def satrec_from_omm(record: dict) -> Satrec:
    sat = Satrec()
    omm.initialize(sat, record)
    return sat


def _utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def julian(t: datetime) -> tuple[float, float]:
    t = _utc(t)
    return jday(t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond / 1e6)


def propagate_teme(sat: Satrec, t: datetime) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    jd, fr = julian(t)
    err, r, v = sat.sgp4(jd, fr)
    if err != 0:
        raise PropagationError(f"SGP4 error code {err} (element set may be stale or the object decayed)")
    return tuple(r), tuple(v)


def gmst(t: datetime) -> float:
    jd, fr = julian(t)
    return gstime(jd + fr)


def teme_to_ecef(r: tuple[float, float, float], theta: float) -> tuple[float, float, float]:
    c, s = math.cos(theta), math.sin(theta)
    x, y, z = r
    return (c * x + s * y, -s * x + c * y, z)


def ecef_to_geodetic(r: tuple[float, float, float]) -> tuple[float, float, float]:
    """Returns (lat_deg, lon_deg, alt_km) on WGS84."""
    x, y, z = r
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - WGS84_E2))
    for _ in range(6):
        sin_lat = math.sin(lat)
        n = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
        alt = p / math.cos(lat) - n if abs(lat) < math.radians(89) else abs(z) / abs(sin_lat) - n * (1 - WGS84_E2)
        lat = math.atan2(z, p * (1 - WGS84_E2 * n / (n + alt)))
    sin_lat = math.sin(lat)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
    alt = p / math.cos(lat) - n if abs(lat) < math.radians(89) else abs(z) / abs(sin_lat) - n * (1 - WGS84_E2)
    return math.degrees(lat), (math.degrees(lon) + 540) % 360 - 180, alt


def geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_km: float = 0.0) -> tuple[float, float, float]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    return (
        (n + alt_km) * math.cos(lat) * math.cos(lon),
        (n + alt_km) * math.cos(lat) * math.sin(lon),
        (n * (1 - WGS84_E2) + alt_km) * math.sin(lat),
    )


def look_angles(obs_lat: float, obs_lon: float, obs_ecef, target_ecef) -> tuple[float, float, float]:
    """Azimuth (deg from N, clockwise), elevation (deg), range (km) from observer to target (ECEF km)."""
    lat, lon = math.radians(obs_lat), math.radians(obs_lon)
    dx, dy, dz = (target_ecef[i] - obs_ecef[i] for i in range(3))
    sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    e = -so * dx + co * dy
    n = -sl * co * dx - sl * so * dy + cl * dz
    u = cl * co * dx + cl * so * dy + sl * dz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    az = (math.degrees(math.atan2(e, n)) + 360) % 360
    el = math.degrees(math.asin(max(-1.0, min(1.0, u / rng))))
    return az, el, rng


def sun_eci(t: datetime) -> tuple[float, float, float]:
    """Low-precision solar position (Astronomical Almanac), equatorial frame, km. ~0.01 deg."""
    jd, fr = julian(t)
    n = jd + fr - 2451545.0
    mean_long = math.radians((280.460 + 0.9856474 * n) % 360)
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    ecl_long = mean_long + math.radians(1.915) * math.sin(g) + math.radians(0.020) * math.sin(2 * g)
    eps = math.radians(23.439 - 0.0000004 * n)
    dist = (1.00014 - 0.01671 * math.cos(g) - 0.00014 * math.cos(2 * g)) * AU_KM
    return (
        dist * math.cos(ecl_long),
        dist * math.cos(eps) * math.sin(ecl_long),
        dist * math.sin(eps) * math.sin(ecl_long),
    )


def subsolar_point(t: datetime) -> tuple[float, float]:
    s = teme_to_ecef(sun_eci(t), gmst(t))
    lat = math.degrees(math.atan2(s[2], math.hypot(s[0], s[1])))
    lon = math.degrees(math.atan2(s[1], s[0]))
    return lat, lon


def is_sunlit(r_teme, sun) -> bool:
    """Cylindrical Earth-shadow model."""
    norm = math.sqrt(sum(c * c for c in sun))
    s = tuple(c / norm for c in sun)
    proj = sum(r_teme[i] * s[i] for i in range(3))
    if proj > 0:
        return True
    perp = math.sqrt(max(0.0, sum(c * c for c in r_teme) - proj * proj))
    return perp > EARTH_RADIUS_KM


def compass(az: float) -> str:
    return COMPASS16[int((az % 360) / 22.5 + 0.5) % 16]


@dataclass(frozen=True)
class Observer:
    lat: float
    lon: float
    alt_km: float = 0.0

    @property
    def ecef(self):
        return geodetic_to_ecef(self.lat, self.lon, self.alt_km)


def subpoint(sat: Satrec, t: datetime) -> dict:
    r, v = propagate_teme(sat, t)
    lat, lon, alt = ecef_to_geodetic(teme_to_ecef(r, gmst(t)))
    return {
        "time": _utc(t).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "alt_km": round(alt, 2),
        "speed_km_s": round(math.sqrt(sum(c * c for c in v)), 3),
        "sunlit": is_sunlit(r, sun_eci(t)),
    }


def ground_track(sat: Satrec, start: datetime, minutes: float, step_s: float = 30.0) -> list[list]:
    """[[iso_time, lat, lon, alt_km], ...]"""
    out = []
    steps = int(minutes * 60 / step_s)
    for i in range(steps + 1):
        t = start + timedelta(seconds=i * step_s)
        try:
            r, _ = propagate_teme(sat, t)
        except PropagationError:
            break
        lat, lon, alt = ecef_to_geodetic(teme_to_ecef(r, gmst(t)))
        out.append([_utc(t).isoformat().replace("+00:00", "Z"), round(lat, 3), round(lon, 3), round(alt, 1)])
    return out


def orbital_period_min(sat: Satrec) -> float:
    return 2 * math.pi / sat.no_kozai


def _state(sat: Satrec, obs: Observer, obs_ecef, t: datetime) -> dict:
    r, _ = propagate_teme(sat, t)
    theta = gmst(t)
    az, el, rng = look_angles(obs.lat, obs.lon, obs_ecef, teme_to_ecef(r, theta))
    sun = sun_eci(t)
    _, sun_el, _ = look_angles(obs.lat, obs.lon, obs_ecef, teme_to_ecef(sun, theta))
    return {"t": t, "az": az, "el": el, "range": rng, "sunlit": is_sunlit(r, sun), "sun_el": sun_el}


def _elev(sat, obs, obs_ecef, t) -> float:
    r, _ = propagate_teme(sat, t)
    return look_angles(obs.lat, obs.lon, obs_ecef, teme_to_ecef(r, gmst(t)))[1]


def _bisect(sat, obs, obs_ecef, t0: datetime, t1: datetime, rising: bool, threshold: float = 0.0) -> datetime:
    for _ in range(14):  # 30 s / 2^14 << 1 s
        mid = t0 + (t1 - t0) / 2
        above = _elev(sat, obs, obs_ecef, mid) >= threshold
        if above == rising:
            t1 = mid
        else:
            t0 = mid
    return t0 + (t1 - t0) / 2


def _is_visible_state(s: dict, min_el: float) -> bool:
    return s["el"] >= min_el and s["sunlit"] and s["sun_el"] <= -6.0


def find_passes(
    sat: Satrec,
    obs: Observer,
    start: datetime,
    hours: float = 72.0,
    min_elevation: float = 10.0,
    visible_only: bool = True,
    max_passes: int = 10,
    step_s: float = 30.0,
) -> list[dict]:
    """Find passes above the horizon. A pass is 'visible' to the naked eye when, at some moment with
    elevation >= min_elevation, the satellite is sunlit and the observer's sky is dark (sun <= -6 deg)."""
    start = _utc(start)
    end = start + timedelta(hours=hours)
    obs_ecef = obs.ecef
    passes: list[dict] = []
    t = start
    prev_el = _elev(sat, obs, obs_ecef, t)
    in_pass = prev_el >= 0
    rise_t = start if in_pass else None
    samples: list[dict] = []
    if in_pass:
        samples.append(_state(sat, obs, obs_ecef, t))
    step = timedelta(seconds=step_s)
    while t < end and len(passes) < max_passes:
        t_next = t + step
        try:
            st = _state(sat, obs, obs_ecef, t_next)
        except PropagationError:
            break
        el = st["el"]
        if not in_pass and el >= 0 > prev_el:
            in_pass = True
            rise_t = _bisect(sat, obs, obs_ecef, t, t_next, rising=True)
            samples = [_state(sat, obs, obs_ecef, rise_t), st]
        elif in_pass and el >= 0:
            samples.append(st)
        elif in_pass and el < 0:
            set_t = _bisect(sat, obs, obs_ecef, t, t_next, rising=False)
            samples.append(_state(sat, obs, obs_ecef, set_t))
            p = _summarise_pass(sat, obs, obs_ecef, rise_t, set_t, samples, min_elevation)
            if p and (p["visible"] or not visible_only):
                passes.append(p)
            in_pass, samples = False, []
        prev_el = el
        t = t_next
    return passes


def _summarise_pass(sat, obs, obs_ecef, rise_t, set_t, samples, min_elevation) -> dict | None:
    peak = max(samples, key=lambda s: s["el"])
    # refine culmination with a local golden-section-ish search (+/- one step)
    lo, hi = peak["t"] - timedelta(seconds=30), peak["t"] + timedelta(seconds=30)
    lo, hi = max(lo, rise_t), min(hi, set_t)
    for _ in range(20):
        m1 = lo + (hi - lo) / 3
        m2 = hi - (hi - lo) / 3
        if _elev(sat, obs, obs_ecef, m1) < _elev(sat, obs, obs_ecef, m2):
            lo = m1
        else:
            hi = m2
    peak = _state(sat, obs, obs_ecef, lo + (hi - lo) / 2)
    if peak["el"] < min_elevation:
        return None
    vis = [s for s in samples if _is_visible_state(s, min_elevation)]
    if _is_visible_state(peak, min_elevation):
        vis.append(peak)
    vis.sort(key=lambda s: s["t"])
    rise = samples[0]
    sett = samples[-1]

    def pt(s):
        return {
            "time": s["t"].isoformat(timespec="seconds").replace("+00:00", "Z"),
            "az": round(s["az"], 1),
            "el": round(s["el"], 1),
            "compass": compass(s["az"]),
        }

    out = {
        "rise": pt(rise),
        "culmination": pt(peak) | {"range_km": round(peak["range"], 0)},
        "set": pt(sett),
        "duration_s": round((set_t - rise_t).total_seconds()),
        "visible": bool(vis),
        "sky": "dark" if peak["sun_el"] <= -6 else ("twilight" if peak["sun_el"] < 0 else "daylight"),
    }
    if vis:
        out["visible_window"] = {"start": pt(vis[0]), "end": pt(vis[-1])}
    return out


def sky_position(sat: Satrec, obs: Observer, t: datetime) -> dict:
    s = _state(sat, obs, obs.ecef, _utc(t))
    return {"az": round(s["az"], 2), "el": round(s["el"], 2), "range_km": round(s["range"], 1), "sunlit": s["sunlit"]}


def sky_track(sat: Satrec, obs: Observer, start: datetime, end: datetime, step_s: float = 10.0) -> list[list]:
    """[[iso, az, el], ...] in the observer's sky between two times."""
    obs_ecef = obs.ecef
    out = []
    t = _utc(start)
    end = _utc(end)
    while t <= end:
        r, _ = propagate_teme(sat, t)
        az, el, _ = look_angles(obs.lat, obs.lon, obs_ecef, teme_to_ecef(r, gmst(t)))
        out.append([t.isoformat().replace("+00:00", "Z"), round(az, 2), round(el, 2)])
        t += timedelta(seconds=step_s)
    return out


def separation_track(sat1: Satrec, sat2: Satrec, tca: datetime, window_s: float = 600, step_s: float = 20) -> list[list]:
    """Both objects' ECEF-geodetic positions around a time of closest approach: [[iso, lat1, lon1, alt1, lat2, lon2, alt2, dist_km]]."""
    out = []
    tca = _utc(tca)
    n = int(window_s / step_s)
    for i in range(-n, n + 1):
        t = tca + timedelta(seconds=i * step_s)
        theta = gmst(t)
        r1, _ = propagate_teme(sat1, t)
        r2, _ = propagate_teme(sat2, t)
        g1 = ecef_to_geodetic(teme_to_ecef(r1, theta))
        g2 = ecef_to_geodetic(teme_to_ecef(r2, theta))
        d = math.dist(r1, r2)
        out.append([t.isoformat().replace("+00:00", "Z"), *(round(x, 3) for x in g1), *(round(x, 3) for x in g2), round(d, 3)])
    return out
