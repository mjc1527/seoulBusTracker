# main.py
import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

SERVICE_KEY = os.getenv("SEOUL_BUS_KEY")
if not SERVICE_KEY:
    raise RuntimeError("SEOUL_BUS_KEY is not set. Add it to server/.env")

# Optional: ALLOWED_ROUTES=100100118,100100124 in .env limits which routes can be requested
ALLOWED_ROUTES = {r.strip() for r in os.getenv("ALLOWED_ROUTES", "").split(",") if r.strip()}

BASE = "http://ws.bus.go.kr/api/rest/buspos"
TTL = 15              # seconds a result is considered fresh
ERROR_TTL = 15        # seconds to remember a failure (so errors don't retry on every request)
NOT_FOUND_TTL = 600   # a nonexistent route won't appear later, so remember it longer
STALE_MAX = 120       # if the API fails, serve the old data for up to this many seconds
MAX_ROUTES = 200      # cap on cached routes

log = logging.getLogger("bus-proxy")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await client.aclose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],  # your frontend origin(s)
    allow_methods=["GET"],
)

client = httpx.AsyncClient(timeout=5.0)


class UpstreamError(Exception):
    def __init__(self, status: int, detail: str):
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
    return {
        "vehId": item["vehId"],
        "plate": item["plainNo"],
        "lat": float(item["gpsY"]),
        "lng": float(item["gpsX"]),
        "busType": int(item["busType"]),
        "stopFlag": int(item["stopFlag"]),
        "sectOrd": int(item["sectOrd"]),
        "nextStId": item.get("nextStId"),
        "congestion": item.get("congetion"),  # sic: the API spells it "congetion"
        "isRunning": item.get("isrunyn") == "1",
        "dataTm": item["dataTm"],
    }


async def fetch_route(route_id: str) -> list:
    log.info("UPSTREAM CALL route=%s", route_id)
    try:
        r = await client.get(
            f"{BASE}/getBusPosByRtid",
            params={"serviceKey": SERVICE_KEY, "busRouteId": route_id, "resultType": "json"},
        )
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        raise UpstreamError(502, "Upstream request failed")

    code = str(data["msgHeader"]["headerCd"])
    if code == "0":
        pass
    elif code == "4":
        raise UpstreamError(404, "Route not found")
    elif code == "8":
        return []  # service ended for the day
    elif code == "6":
        raise UpstreamError(503, "Realtime data temporarily unavailable")
    else:
        raise UpstreamError(502, f"Upstream error {code}: {data['msgHeader']['headerMsg']}")

    items = (data.get("msgBody") or {}).get("itemList") or []
    if isinstance(items, dict):
        items = [items]
    return [normalize(i) for i in items]


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

        cache[route_id] = new
        evict_if_needed()
        return new


@app.get("/api/buses/{route_id}")
async def buses(route_id: str, response: Response):
    if not re.fullmatch(r"\d{9}", route_id):
        raise HTTPException(400, "Invalid route id")
    if ALLOWED_ROUTES and route_id not in ALLOWED_ROUTES:
        raise HTTPException(403, "Route not allowed")

    entry = await get_route(route_id)
    if entry.error:
        raise HTTPException(entry.error.status, entry.error.detail)

    response.headers["Cache-Control"] = f"public, max-age={TTL}"
    return {
        "routeId": route_id,
        "buses": entry.buses,
        "stale": entry.stale,
        "ageSeconds": round(time.time() - entry.fetched_at),
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)
