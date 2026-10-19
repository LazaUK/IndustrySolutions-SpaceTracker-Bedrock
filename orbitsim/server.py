"""OrbitSim AI MCP server (Streamable HTTP) with a 3D globe MCP App."""

from __future__ import annotations

import asyncio
import functools
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.apps import Apps
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__
from . import orbit
from .data import DataClient, DataError, Place

GLOBE_URI = "ui://orbitsim/globe.html"
UI_DIR = Path(os.getenv("ORBITSIM_UI_DIR", Path(__file__).parent / "static"))
BRIGHT = ["ISS", "Tiangong", "Hubble"]
PALETTE = ["#4fc3f7", "#ffb74d", "#ba68c8", "#81c784", "#f06292", "#fff176"]
DISCLAIMER = (
    "Educational visualisation from public CelesTrak SOCRATES screening data. "
    "Not for operational collision-avoidance decisions."
)
LATLON_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

INSTRUCTIONS = """OrbitSim AI is a voice-first sky companion. Replies are spoken aloud on an Alexa+ device, so:
- keep answers to one or two short sentences, use local times, compass words (\"look west-southwest\") and degrees;
- show_orbit_view answers "where is it" (mode=globe), "what's in my sky" (mode=sky, needs a location) and close
  approaches (mode=conjunction); its text reply is complete, so it also works on devices without a screen;
- find_passes answers "when can I see it" (name a satellite) and "what's worth seeing tonight" (omit it);
- if the user has not given a location, use the device location or ask for a town;
- conjunction data is educational only — always mention that it is not for operational decisions;
- satellites are identified by their NORAD catalogue number (e.g. 25544 = ISS). Every result includes it; pass the
  number back in follow-up calls, and accept it from the user, to avoid ambiguous names.
"""

data = DataClient()
apps = Apps()

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

LocationArg = Annotated[
    str | None,
    Field(default=None, description="Town/city name or 'lat,lon' of the user (device location when available).", max_length=100),
]


# --------------------------------------------------------------------------- helpers
def _friendly(fn):
    """Turn expected user-facing problems into ToolError so the message reaches the model verbatim."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (DataError, orbit.PropagationError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _spoken_time(t: datetime, tz: ZoneInfo, ref: datetime | None = None) -> str:
    local = t.astimezone(tz)
    ref_local = (ref or _now()).astimezone(tz)
    clock = local.strftime("%I:%M %p").lstrip("0")
    days = (local.date() - ref_local.date()).days
    if days == 0:
        when = "tonight" if local.hour >= 17 else ("this morning" if local.hour < 12 else "this afternoon")
        if local.hour < 5:
            when = "early this morning"
    elif days == 1:
        when = "tomorrow night" if local.hour >= 17 else ("early tomorrow morning" if local.hour < 9 else "tomorrow")
    else:
        when = local.strftime("%A")
    return f"{when} at {clock}"


def _result(text: str, structured: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=structured)


async def _resolve_place(location: str | None, required: bool = True) -> Place | None:
    if location:
        m = LATLON_RE.match(location)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise DataError("Latitude must be -90..90 and longitude -180..180.")
            return Place(f"{lat:.3f}, {lon:.3f}", lat, lon, "UTC")
        return await data.geocode(location)
    if required:
        raise DataError("Where are you? Tell me a town or city.")
    return None


FRIENDLY_NAMES = {25544: "the ISS", 48274: "Tiangong", 20580: "Hubble"}
FLAG_RE = re.compile(r"\s*\[[^\]]*\]\s*$")


def _display_name(name: str, norad: int | None = None) -> str:
    if norad in FRIENDLY_NAMES:
        return FRIENDLY_NAMES[norad]
    return FLAG_RE.sub("", name).strip()


def _sat_info(rec: dict) -> dict:
    norad = int(rec["NORAD_CAT_ID"])
    return {
        "name": _display_name(rec["OBJECT_NAME"], norad),
        "object_name": rec["OBJECT_NAME"],
        "norad": norad,
        "epoch": rec["EPOCH"] + "Z",
    }


def _cloud_at(clouds: dict | None, t: datetime) -> int | None:
    if not clouds:
        return None
    return clouds.get(t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H"))


def _cloud_words(pct: int | None) -> str:
    if pct is None:
        return ""
    if pct <= 25:
        return f"skies look clear ({pct}% cloud)"
    if pct <= 60:
        return f"partly cloudy ({pct}% cloud)"
    return f"likely cloudy ({pct}% cloud)"


def _pass_sentence(name: str, p: dict, tz: ZoneInfo) -> str:
    words = orbit.COMPASS_WORDS
    if not p.get("visible_window"):
        c = p["culmination"]
        why = "in daylight" if p["sky"] != "dark" else "in Earth's shadow"
        return (
            f"{name} passes {_spoken_time(_parse_iso(c['time']), tz)}, peaking at {round(c['el'])} degrees in the "
            f"{words[c['compass']]}, but {why}, so you won't see it by eye"
        )
    vw = p["visible_window"]
    start = _parse_iso(vw["start"]["time"])
    s = (
        f"{name} {_spoken_time(start, tz)}: look {words[vw['start']['compass']]}, "
        f"it climbs to {round(p['culmination']['el'])} degrees and fades in the {words[vw['end']['compass']]}"
    )
    mins = round(((_parse_iso(vw["end"]["time"]) - start).total_seconds()) / 60)
    if mins >= 1:
        s += f", visible for about {mins} minute{'s' if mins != 1 else ''}"
    return s


def _position_sentence(s: dict, detailed: bool = False) -> str:
    pos = s["position"]
    lat, lon = pos["lat"], pos["lon"]
    hemi = f"{abs(lat):.0f}°{'N' if lat >= 0 else 'S'}, {abs(lon):.0f}°{'E' if lon >= 0 else 'W'}"
    name = s["name"][0].upper() + s["name"][1:]
    text = f"{name} is {pos['alt_km']:.0f} km up over {hemi}"
    if not detailed:
        return text + "."
    return (
        f"{text}, travelling at {pos['speed_km_s'] * 3600:,.0f} km/h — one lap of Earth every {s['period_min']:.0f} "
        f"minutes. It is currently {'in sunlight' if pos['sunlit'] else 'in Earth’s shadow'}."
    )


# --------------------------------------------------------------------------- UI tool + resource
async def _globe_scene(sats: list[str], place: Place | None, minutes: int) -> dict:
    now = _now()
    out = []
    for i, name in enumerate(sats):
        rec = await data.resolve_satellite(name)
        sat = orbit.satrec_from_omm(rec)
        period = orbit.orbital_period_min(sat)
        span = min(max(minutes, 10), 360)
        track = await asyncio.to_thread(orbit.ground_track, sat, now - timedelta(minutes=10), span + 10, 30.0)
        out.append(
            _sat_info(rec)
            | {"color": PALETTE[i % len(PALETTE)], "period_min": round(period, 2), "track": track, "position": orbit.subpoint(sat, now)}
        )
    return {"satellites": out}


async def _sky_scene(sats: list[str], place: Place) -> dict:
    now = _now()
    obs = orbit.Observer(place.lat, place.lon)
    out = []
    for i, name in enumerate(sats):
        rec = await data.resolve_satellite(name)
        sat = orbit.satrec_from_omm(rec)
        passes = await asyncio.to_thread(orbit.find_passes, sat, obs, now - timedelta(minutes=15), 48.0, 10.0, True, 1)
        if not passes:
            passes = await asyncio.to_thread(orbit.find_passes, sat, obs, now - timedelta(minutes=15), 48.0, 10.0, False, 1)
        entry = _sat_info(rec) | {"color": PALETTE[i % len(PALETTE)], "now": orbit.sky_position(sat, obs, now)}
        if passes:
            p = passes[0]
            entry["pass"] = p
            entry["sky_track"] = await asyncio.to_thread(
                orbit.sky_track, sat, obs, _parse_iso(p["rise"]["time"]), _parse_iso(p["set"]["time"]), 10.0
            )
        out.append(entry)
    return {"satellites": out}


async def _conjunction_scene(satellite: str | None) -> tuple[dict, str]:
    norad = None
    if satellite:
        norad = int((await data.resolve_satellite(satellite))["NORAD_CAT_ID"])
    events, source = await data.get_conjunctions(norad, 1)
    if not events:
        raise DataError("No close approaches listed for that satellite right now.")
    ev = events[0]
    recs = []
    for obj in (ev["object_1"], ev["object_2"]):
        recs.append(await data.resolve_satellite(str(obj["norad"])))
    s1, s2 = (orbit.satrec_from_omm(r) for r in recs)
    tca = _parse_iso(ev["tca"])
    sep = await asyncio.to_thread(orbit.separation_track, s1, s2, tca, 600.0, 10.0)
    sats = []
    for i, (rec, s) in enumerate(zip(recs, (s1, s2))):
        track = await asyncio.to_thread(orbit.ground_track, s, tca - timedelta(minutes=50), 100.0, 30.0)
        sats.append(_sat_info(rec) | {"color": ["#4fc3f7", "#ff5252"][i], "track": track})
    return {"satellites": sats, "conjunction": ev | {"approach": sep, "source": source, "disclaimer": DISCLAIMER}}, source


@apps.tool(
    resource_uri=GLOBE_URI,
    visibility=["model", "app"],
    name="show_orbit_view",
    title="Show the 3D orbit view",
    description=(
        "Display an interactive 3D view on the user's screen. mode=globe: Earth with satellites and their ground tracks; "
        "mode=sky: the observer's sky dome with the next pass path (needs a location); "
        "mode=conjunction: the closest approach involving a satellite, highlighted in red."
    ),
    annotations=READ_ONLY,
    structured_output=False,
)
@_friendly
async def show_orbit_view(
    satellites: Annotated[
        list[Annotated[str, Field(max_length=80)]] | None,
        Field(default=None, max_length=6, description="Satellites to show as NORAD numbers (preferred), aliases or names; default ISS. In conjunction mode the first one is used."),
    ] = None,
    mode: Literal["globe", "sky", "conjunction"] = "globe",
    location: LocationArg = None,
    minutes: Annotated[int, Field(ge=10, le=360, description="Minutes of future track to draw (globe mode)")] = 95,
) -> CallToolResult:
    sats = satellites or ["ISS"]
    now = _now()
    place = await _resolve_place(location, required=(mode == "sky"))
    sun_lat, sun_lon = orbit.subsolar_point(now)
    scene: dict = {
        "mode": mode,
        "generated_at": _iso(now),
        "sun": {"lat": round(sun_lat, 3), "lon": round(sun_lon, 3)},
        "observer": place.as_dict() if place else None,
    }
    if mode == "globe":
        scene |= await _globe_scene(sats, place, minutes)
        shown = scene["satellites"]
        if len(shown) == 1:
            text = _position_sentence(shown[0], detailed=True)
        else:
            text = "Showing " + ", ".join(s["name"] for s in shown) + " on the globe. " + " ".join(_position_sentence(s) for s in shown)
    elif mode == "sky":
        scene |= await _sky_scene(sats, place)
        tz = _tz(place.timezone)
        bits = []
        for s in scene["satellites"]:
            if "pass" in s:
                bits.append(_pass_sentence(s["name"], s["pass"], tz))
        summary = "; ".join(bits) + "." if bits else "No passes in the next two days."
        text = f"Showing the sky over {place.name}. " + summary[:1].upper() + summary[1:]
    else:
        conj, _ = await _conjunction_scene(sats[0] if satellites else None)
        scene |= conj
        e = scene["conjunction"]
        dist_m = e["min_range_km"] * 1000
        dist = f"{dist_m:,.0f} metres" if dist_m < 10_000 else f"{e['min_range_km']:,.1f} km"
        text = (
            f"Closest approach: {_display_name(e['object_1']['name'], e['object_1']['norad'])} and "
            f"{_display_name(e['object_2']['name'], e['object_2']['norad'])} pass within {dist} on "
            f"{_parse_iso(e['tca']).strftime('%d %b at %H:%M UTC')}, at {e['relative_speed_km_s']:.1f} km/s. "
            "Educational data only, not for operational decisions."
        )
    return _result(text, scene)


def _load_globe_html() -> str:
    p = UI_DIR / "globe.html"
    if p.exists():
        return p.read_text("utf-8")
    return "<!doctype html><title>OrbitSim</title><p>Globe UI not built. Run the Docker build.</p>"


apps.add_html_resource(
    GLOBE_URI,
    _load_globe_html(),
    name="orbitsim-globe",
    title="OrbitSim 3D globe",
    description="Interactive Three.js globe, sky dome and conjunction viewer.",
    prefers_border=False,
)


# Apps-extension tools/resources must be registered before MCPServer(extensions=[apps]) is built.
mcp = MCPServer(
    name="orbitsim-ai",
    title="OrbitSim AI",
    version=__version__,
    instructions=INSTRUCTIONS,
    website_url="https://github.com/",
    extensions=[apps],
)

# --------------------------------------------------------------------------- tools
@mcp.tool(
    name="find_passes",
    title="When can I see a satellite",
    description=(
        "Passes over the user's location with local times, compass directions, peak elevation and the cloud forecast. "
        "Name a satellite for its next passes; omit it for 'what's worth seeing tonight': the best pass of the bright "
        "satellites (ISS, Tiangong, Hubble) with a go / cloudy / no-go verdict. By default only naked-eye-visible "
        "passes (satellite sunlit, sky dark, >=10° elevation) are returned."
    ),
    annotations=READ_ONLY,
    structured_output=False,
)
@_friendly
async def find_passes(
    location: LocationArg = None,
    satellite: Annotated[
        str | None,
        Field(default=None, description="NORAD number (preferred, e.g. '25544'), alias or name. Omit to check the bright satellites.", max_length=80),
    ] = None,
    hours: Annotated[int, Field(ge=1, le=240, description="Look-ahead window in hours")] = 48,
    visible_only: Annotated[bool, Field(description="Only passes you can see by eye")] = True,
) -> CallToolResult:
    place = await _resolve_place(location)
    recs = []
    for name in [satellite] if satellite else BRIGHT:
        try:
            recs.append(await data.resolve_satellite(name))
        except DataError:
            if satellite:
                raise
    obs = orbit.Observer(place.lat, place.lon)
    start = _now()

    def compute():
        out = []
        for rec in recs:
            try:
                for p in orbit.find_passes(orbit.satrec_from_omm(rec), obs, start, float(hours), 10.0, visible_only, 6):
                    out.append((rec, p))
            except orbit.PropagationError:
                if satellite:
                    raise
        return out

    found, clouds = await asyncio.gather(asyncio.to_thread(compute), data.cloud_cover(place.lat, place.lon))
    tz = _tz(place.timezone)
    passes = []
    for rec, p in found:
        cc = _cloud_at(clouds, _parse_iso(p["culmination"]["time"]))
        passes.append({
            "satellite": _sat_info(rec),
            "pass": p,
            "local_time": _parse_iso(p["rise"]["time"]).astimezone(tz).isoformat(timespec="minutes"),
            "cloud_cover_pct": cc,
            "score": round(p["culmination"]["el"] * (1.0 if cc is None else max(0.05, 1 - cc / 100)), 1),
        })
    passes.sort(key=lambda e: e["pass"]["rise"]["time"])
    best = (passes[0] if passes else None) if satellite else max(passes, key=lambda e: e["score"], default=None)
    window = f"the next {hours} hours" if hours < 72 else f"the next {round(hours / 24)} days"
    if best is None:
        verdict = "no-go"
        if satellite:
            kind = "visible passes" if visible_only else "passes"
            text = f"No {kind} of {_sat_info(recs[0])['name']} from {place.name} in {window}."
        else:
            text = f"Nothing bright passes over {place.name} in {window} — try again tomorrow."
    else:
        cc = best["cloud_cover_pct"]
        verdict = "go" if cc is None or cc <= 60 else "cloudy"
        text = (f"Next from {place.name}: " if satellite else "Best bet: ") + _pass_sentence(best["satellite"]["name"], best["pass"], tz)
        cw = _cloud_words(cc)
        text += f"; {cw}." if cw else "."
        if verdict == "cloudy":
            text += " Clouds may spoil it."
        if len(passes) > 1:
            text += f" I found {len(passes) - 1} more after that." if satellite else f" {len(passes)} passes in total."
    return _result(
        text,
        {
            "location": place.as_dict(),
            "satellite": _sat_info(recs[0]) if satellite else None,
            "verdict": verdict,
            "best": best,
            "passes": passes,
            "weather_source": "Open-Meteo" if clouds else None,
        },
    )


# --------------------------------------------------------------------------- HTTP app
def _csv_env(name: str, default: str) -> list[str]:
    return [h.strip() for h in os.getenv(name, default).split(",") if h.strip()]


def create_app():
    hosts = _csv_env("ORBITSIM_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*")
    origins = _csv_env("ORBITSIM_ALLOWED_ORIGINS", ",".join(f"http://{h}" for h in hosts))
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
        ),
        host="0.0.0.0",
    )

    async def healthz(_: Request):
        return JSONResponse({"ok": True, "name": "orbitsim-ai", "version": __version__, "catalogue": data.catalogue_status()})

    async def root(_: Request):
        return RedirectResponse("/preview/")

    app.router.routes.append(Route("/healthz", healthz))
    app.router.routes.append(Route("/", root))
    preview = UI_DIR / "preview"
    if preview.is_dir():
        app.router.routes.append(Mount("/preview", StaticFiles(directory=preview, html=True), name="preview"))
    return app


def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(create_app(), host=os.getenv("HOST", "0.0.0.0"), port=port, log_level="info", proxy_headers=False)


if __name__ == "__main__":
    main()
