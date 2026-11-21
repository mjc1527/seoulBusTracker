# main.py
import asyncio
import json
import logging
import math
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

load_dotenv()

# ---------------------------------------------------------------- logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx/httpcore log full request URLs at INFO, and our URL contains the serviceKey.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("bus-proxy")


# ---------------------------------------------------------------- config
def env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


SERVICE_KEY = os.getenv("SEOUL_BUS_KEY")
if not SERVICE_KEY:
    raise RuntimeError("SEOUL_BUS_KEY is not set. Add it to server/.env")

# Comma-separated lists in .env. In production set ALLOWED_ROUTES, CORS_ORIGINS and ALLOWED_HOSTS.
ALLOWED_ROUTES = set(env_list("ALLOWED_ROUTES"))
CORS_ORIGINS = env_list("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000")
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
IS_PROD = os.getenv("ENV", "dev").lower() == "prod"
PRINT_BUSES = os.getenv("PRINT_BUSES", "1") == "1"  # print a bus table to the terminal on each request

# Use https:// if the upstream supports it (override in .env) so the key isn't sent in plaintext.
BASE = os.getenv("BUS_API_BASE", "http://ws.bus.go.kr/api/rest/buspos")

TTL = 15               # seconds a result is considered fresh
ERROR_TTL = 15         # seconds to remember a failure
NOT_FOUND_TTL = 600    # a nonexistent route won't appear later, so remember it longer
STALE_MAX = 120        # if the API fails, serve old data for up to this many seconds
MAX_ROUTES = 200       # cap on cached routes

MAX_UPSTREAM_BYTES = 2_000_000   # refuse absurdly large upstream responses
MAX_UPSTREAM_CONCURRENCY = 5     # simultaneous upstream calls across all routes
MAX_ITEMS_PER_ROUTE = 500        # sanity cap on buses per route

RATE_LIMIT = 30        # requests per client IP per window
RATE_WINDOW = 60.0     # seconds
MAX_TRACKED_IPS = 10_000

ROUTE_ID_RE = re.compile(r"[0-9]{9}")  # [0-9], not \d: \d also matches non-ASCII digits in Python


# ---------------------------------------------------------------- app setup
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await client.aclose()


app = FastAPI(
    lifespan=lifespan,
    # Hide interactive docs and the schema; nobody but you needs them.
    docs_url=None if IS_PROD else "/docs",
    redoc_url=None,
    openapi_url=None if IS_PROD else "/openapi.json",
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET"],
    allow_headers=[],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


client = httpx.AsyncClient(
    timeout=httpx.Timeout(5.0, connect=3.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
    follow_redirects=False,
)
upstream_sem = asyncio.Semaphore(MAX_UPSTREAM_CONCURRENCY)


# ---------------------------------------------------------------- rate limiting
_hits: dict[str, deque] = {}


async def rate_limit(request: Request) -> None:
    """Sliding-window limiter per client IP (in-memory, single process).

    Behind a reverse proxy, run uvicorn with --proxy-headers and
    --forwarded-allow-ips=<proxy ip> so request.client is the real client.
    """
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()

    q = _hits.get(ip)
    if q is None:
        if len(_hits) >= MAX_TRACKED_IPS:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > RATE_WINDOW]:
                _hits.pop(k, None)
            while len(_hits) >= MAX_TRACKED_IPS:      # still full: drop the oldest-seen IP
                _hits.pop(next(iter(_hits)))
        q = _hits[ip] = deque()

    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        retry = max(1, int(RATE_WINDOW - (now - q[0])))
        raise HTTPException(429, "Too many requests", headers={"Retry-After": str(retry)})
    q.append(now)


# ---------------------------------------------------------------- cache
class UpstreamError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass
class Entry:
    ts: float                    # monotonic time this entry was stored
    ttl: float                   # how long it stays fresh
    buses: list | None = None
    error: UpstreamError | None = None
    fetched_at: float = 0.0      # wall-clock time of the last successful upstream fetch
    stale: bool = False


cache: dict[str, Entry] = {}
locks: dict[str, asyncio.Lock] = {}


def is_fresh(e: Entry) -> bool:
    return time.monotonic() - e.ts < e.ttl


def normalize(item: dict) -> dict:
    lat, lng = float(item["gpsY"]), float(item["gpsX"])
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("coordinates out of range")
    return {
        "vehId": str(item["vehId"]),
        "plate": str(item["plainNo"]),
        "lat": lat,
        "lng": lng,
        "busType": int(item["busType"]),
        "stopFlag": int(item["stopFlag"]),
        "sectOrd": int(item["sectOrd"]),
        "nextStId": item.get("nextStId"),
        "congestion": item.get("congetion"),  # sic: the API spells it "congetion"
        "isRunning": item.get("isrunyn") == "1",
        "dataTm": str(item["dataTm"]),
    }


async def fetch_route(route_id: str) -> list:
    log.info("upstream call route=%s", route_id)
    try:
        async with upstream_sem:
            async with client.stream(
                "GET",
                f"{BASE}/getBusPosByRtid",
                params={"serviceKey": SERVICE_KEY, "busRouteId": route_id, "resultType": "json"},
            ) as r:
                r.raise_for_status()
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_UPSTREAM_BYTES:
                        raise UpstreamError(502, "Upstream response too large")
        data = json.loads(body)
    except UpstreamError:
        raise
    except (httpx.HTTPError, ValueError) as e:
        # Log the exception type only: str(e) from httpx can contain the full URL (with the key).
        log.warning("upstream request failed route=%s error=%s", route_id, type(e).__name__)
        raise UpstreamError(502, "Upstream request failed")

    try:
        header = data["msgHeader"]
        code = str(header["headerCd"])
    except (KeyError, TypeError):
        log.warning("malformed upstream response route=%s (no msgHeader)", route_id)
        raise UpstreamError(502, "Malformed upstream response")

    if code == "0":
        pass
    elif code == "4":
        raise UpstreamError(404, "Route not found")
    elif code == "8":
        return []  # service ended for the day
    elif code == "6":
        raise UpstreamError(503, "Realtime data temporarily unavailable")
    else:
        # Keep upstream's message in our logs only, not in the response.
        log.warning("upstream error route=%s code=%s msg=%s", route_id, code, header.get("headerMsg"))
        raise UpstreamError(502, "Upstream error")

    body_part = data.get("msgBody") if isinstance(data, dict) else None
    items = body_part.get("itemList") if isinstance(body_part, dict) else None
    items = items or []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise UpstreamError(502, "Malformed upstream response")

    buses, skipped = [], 0
    for item in items[:MAX_ITEMS_PER_ROUTE]:
        try:
            buses.append(normalize(item))
        except (KeyError, TypeError, ValueError):
            skipped += 1  # one bad item shouldn't take down the whole route
    if skipped:
        log.warning("route=%s skipped %d malformed items", route_id, skipped)
    return buses


def evict_if_needed() -> None:
    while len(cache) > MAX_ROUTES:
        oldest = min(cache, key=lambda k: cache[k].ts)
        cache.pop(oldest)
        lock = locks.get(oldest)
        if lock and not lock.locked():
            locks.pop(oldest, None)


async def get_route(route_id: str) -> Entry:
    entry = cache.get(route_id)
    if entry and is_fresh(entry):
        return entry

    lock = locks.setdefault(route_id, asyncio.Lock())
    async with lock:  # only one upstream call per route at a time
        old = cache.get(route_id)
        if old and is_fresh(old):  # someone else refreshed while we waited
            return old

        now = time.monotonic()
        try:
            buses = await fetch_route(route_id)
            new = Entry(ts=now, ttl=TTL, buses=buses, fetched_at=time.time())
        except UpstreamError as e:
            if e.status == 404:
                new = Entry(ts=now, ttl=NOT_FOUND_TTL, error=e)
            elif old and old.buses is not None and time.time() - old.fetched_at < STALE_MAX:
                # API failed: keep serving the last good data, retry after ERROR_TTL
                new = Entry(ts=now, ttl=ERROR_TTL, buses=old.buses,
                            fetched_at=old.fetched_at, stale=True)
            else:
                new = Entry(ts=now, ttl=ERROR_TTL, error=e)
        except Exception:
            # Anything unexpected: log it, never leak details to the client.
            log.exception("unexpected error fetching route=%s", route_id)
            new = Entry(ts=now, ttl=ERROR_TTL, error=UpstreamError(502, "Upstream request failed"))

        cache[route_id] = new
        evict_if_needed()
        return new


# ---------------------------------------------------------------- terminal output
CONGESTION = {"0": "-", "3": "calm", "4": "normal", "5": "crowded", "6": "very crowded"}


def clean(value, width: int) -> str:
    """Upstream text goes to a terminal, so strip control characters/escape codes."""
    return re.sub(r"[^\w\- ]", "?", str(value))[:width]


def fmt_time(tm: str) -> str:
    # dataTm looks like 20261121103000 (YYYYMMDDhhmmss)
    if re.fullmatch(r"[0-9]{14}", tm or ""):
        return f"{tm[8:10]}:{tm[10:12]}:{tm[12:14]}"
    return clean(tm, 14)


def print_buses(route_id: str, entry: Entry) -> None:
    if not PRINT_BUSES or entry.buses is None:
        return
    age = round(time.time() - entry.fetched_at)
    note = " (STALE)" if entry.stale else ""
    print(f"\n=== Route {route_id} | {len(entry.buses)} buses | data age {age}s{note} ===")
    if not entry.buses:
        print("  no buses running")
        return
    print(f"  {'PLATE':<12} {'LAT':>10} {'LNG':>11}  {'STATUS':<8} {'NEXT STOP':<10} {'LOAD':<13} TIME")
    for b in entry.buses:
        status = "at stop" if b["stopFlag"] == 1 else "moving"
        if not b["isRunning"]:
            status = "parked"
        load = CONGESTION.get(str(b["congestion"]), clean(b["congestion"] or "-", 12))
        print(
            f"  {clean(b['plate'], 12):<12} {b['lat']:>10.5f} {b['lng']:>11.5f}  "
            f"{status:<8} {clean(b['nextStId'] or '-', 10):<10} {load:<13} {fmt_time(b['dataTm'])}"
        )
    print(flush=True)


# ---------------------------------------------------------------- routes
@app.get("/api/buses/{route_id}", dependencies=[Depends(rate_limit)])
async def buses(route_id: str, response: Response):
    # Validate before anything touches the cache, locks or upstream.
    if not ROUTE_ID_RE.fullmatch(route_id):
        raise HTTPException(400, "Invalid route id")
    if ALLOWED_ROUTES and route_id not in ALLOWED_ROUTES:
        raise HTTPException(403, "Route not allowed")

    entry = await get_route(route_id)
    if entry.error:
        raise HTTPException(entry.error.status, entry.error.detail)

    print_buses(route_id, entry)

    remaining = max(0, int(entry.ttl - (time.monotonic() - entry.ts)))
    response.headers["Cache-Control"] = f"public, max-age={remaining}"
    return {
        "routeId": route_id,
        "buses": entry.buses,
        "stale": entry.stale,
        "ageSeconds": round(time.time() - entry.fetched_at),
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)