# main.py
import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from shapely.geometry import LineString, Point

load_dotenv()

# ---------------------------------------------------------------- 로깅
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx/httpcore는 INFO 레벨에서 요청 URL 전체를 로그로 남기는데, 우리 URL에는 serviceKey가 들어 있음.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("bus-proxy")


# ---------------------------------------------------------------- 설정
def env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


SERVICE_KEY = os.getenv("SEOUL_BUS_KEY")
if not SERVICE_KEY:
    raise RuntimeError("SEOUL_BUS_KEY is not set. Add it to server/.env")

# .env의 쉼표 구분 목록. 운영 환경에서는 ALLOWED_ROUTES, CORS_ORIGINS, ALLOWED_HOSTS를 반드시 설정할 것.
ALLOWED_ROUTES = set(env_list("ALLOWED_ROUTES"))
CORS_ORIGINS = env_list("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000")
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
IS_PROD = os.getenv("ENV", "dev").lower() == "prod"
PRINT_BUSES = os.getenv("PRINT_BUSES", "1") == "1"  # 요청이 올 때마다 터미널에 버스 표를 출력
COUNTDOWN = os.getenv("COUNTDOWN", "1") == "1"      # 터미널에 "다음 API 호출까지 N초" 줄을 실시간 표시

# 서울시 API가 https를 지원하면 .env에서 https://로 바꿔 서비스 키가 평문으로 전송되지 않게 할 것.
BASE = os.getenv("BUS_API_BASE", "http://ws.bus.go.kr/api/rest/buspos")
PATH_BASE = os.getenv("BUS_PATH_API_BASE", "http://ws.bus.go.kr/api/rest/busRouteInfo")

TTL = 15               # 결과를 신선한 것으로 보는 시간(초)
ERROR_TTL = 15         # 실패 결과를 기억해 두는 시간(초)
NOT_FOUND_TTL = 600    # 없는 노선은 나중에 생기지 않으므로 더 오래 기억
STALE_MAX = 120        # API가 실패하면 최대 이 시간(초)까지 오래된 데이터를 대신 제공
MAX_ROUTES = 200       # 캐시에 보관할 노선 수 상한

PATH_TTL = 24 * 3600   # 노선 좌표열은 거의 바뀌지 않음
PATH_ERROR_TTL = 300   # 노선 경로 호출이 실패하면 보정 없이 동작하고 이 시간(초) 뒤에 재시도

MAX_UPSTREAM_BYTES = 2_000_000   # 비정상적으로 큰 상위 API 응답은 거부
MAX_UPSTREAM_CONCURRENCY = 5     # 모든 노선을 합쳐 동시에 보낼 수 있는 상위 API 호출 수
MAX_ITEMS_PER_ROUTE = 500        # 노선 하나당 버스 수에 대한 안전 상한
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "900"))  # 개발 계정은 하루 약 1,000회; 0이면 자체 상한 없음

RATE_LIMIT = 30        # 구간당 클라이언트 IP별 요청 수
RATE_WINDOW = 60.0     # 구간 길이(초)
MAX_TRACKED_IPS = 10_000

# 위치 데이터 가공 (프로젝트 제안서 내용)
SNAP_MAX_M = float(os.getenv("SNAP_MAX_M", "60"))        # 노선에서 이보다 멀면 보정하지 않음
STOP_THRESHOLD_M = float(os.getenv("STOP_THRESHOLD_M", "15"))  # 연속 두 보고 사이 이동이 이보다 작으면 정차로 판정
MAX_JUMP_M = 2000      # 두 보고 사이 거리가 이보다 크면 이동이 아니라 초기화로 취급

DB_PATH = os.getenv("DB_PATH", "bus_log.db")  # SQLite 관측/호출 로그 파일; 비워 두면 기록 안 함

KST = timezone(timedelta(hours=9))
ROUTE_ID_RE = re.compile(r"[0-9]{9}")  # \d가 아니라 [0-9]: 파이썬의 \d는 ASCII 아닌 숫자도 매칭함


# ---------------------------------------------------------------- 앱 설정
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(countdown_loop()) if COUNTDOWN else None
    yield
    if task:
        task.cancel()
    await client.aclose()
    close_db()


app = FastAPI(
    lifespan=lifespan,
    # 대화형 문서와 스키마는 숨김; 개발자 말고는 필요 없음.
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


# ---------------------------------------------------------------- 요청 횟수 제한
_hits: dict[str, deque] = {}


async def rate_limit(request: Request) -> None:
    """클라이언트 IP별 슬라이딩 윈도 제한기 (메모리 기반, 단일 프로세스).

    리버스 프록시 뒤에서는 uvicorn을 --proxy-headers와
    --forwarded-allow-ips=<프록시 IP>로 실행해야 request.client가 실제 클라이언트가 됨.
    """
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()

    q = _hits.get(ip)
    if q is None:
        if len(_hits) >= MAX_TRACKED_IPS:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > RATE_WINDOW]:
                _hits.pop(k, None)
            while len(_hits) >= MAX_TRACKED_IPS:      # 그래도 가득 차 있으면 가장 먼저 본 IP부터 제거
                _hits.pop(next(iter(_hits)))
        q = _hits[ip] = deque()

    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        retry = max(1, int(RATE_WINDOW - (now - q[0])))
        raise HTTPException(429, "Too many requests", headers={"Retry-After": str(retry)})
    q.append(now)


# ---------------------------------------------------------------- SQLite 로그
# fetch_log:    상위 API 호출 1회당 1행 (성공 여부 / 지연 / 오류)  -> 오류율, 응답 지연 측정
# observations: 버스의 *새* 보고(data_ts가 바뀐 경우)당 1행 -> 갱신 주기(차량별 data_ts 간격)와
#               데이터 지연(ts - data_ts) 측정
SQL_FETCH = "INSERT INTO fetch_log VALUES (?,?,?,?,?,?)"
SQL_OBS = "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)"

db: sqlite3.Connection | None = None
db_lock = threading.Lock()


def init_db() -> None:
    global db
    if not DB_PATH:
        return
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    with db_lock:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS fetch_log(
              ts REAL, route_id TEXT, kind TEXT, ok INTEGER, latency_ms REAL, error TEXT);
            CREATE TABLE IF NOT EXISTS observations(
              ts REAL, route_id TEXT, veh_id TEXT, data_ts REAL,
              lng REAL, lat REAL, off_line_m REAL, stopped INTEGER);
            """
        )
        db.commit()


def close_db() -> None:
    global db
    if db is not None:
        with db_lock:
            db.close()
        db = None


def _db_write(sql: str, rows: list[tuple]) -> None:
    try:
        with db_lock:
            if db is not None:
                db.executemany(sql, rows)
                db.commit()
    except sqlite3.Error:
        log.exception("sqlite write failed")


def db_log(sql: str, rows: list[tuple]) -> None:
    """결과를 기다리지 않고 작업 스레드에서 기록해, 디스크 때문에 이벤트 루프가 멈추지 않게 함."""
    if db is not None and rows:
        asyncio.get_running_loop().run_in_executor(None, _db_write, sql, rows)


# ---------------------------------------------------------------- 지오메트리
# 지역 등장방형 투영: 미터 단위, 도시 하나 규모에서는 충분히 정확함.
_LAT0 = 37.55
_KX = 111_320 * math.cos(math.radians(_LAT0))
_KY = 110_540


def to_m(lng: float, lat: float) -> tuple[float, float]:
    return lng * _KX, lat * _KY


def to_ll(x: float, y: float) -> tuple[float, float]:
    return x / _KX, y / _KY


def parse_ts(tm: str) -> float | None:
    """dataTm은 20261121103000 형식(YYYYMMDDhhmmss, KST) -> 에포크 초로 변환."""
    try:
        return datetime.strptime(tm, "%Y%m%d%H%M%S").replace(tzinfo=KST).timestamp()
    except (ValueError, TypeError):
        return None


# 노선ID -> 차량ID -> 마지막으로 받아들인 보고 {x, y, data_ts, bearing, stopped}
route_state: dict[str, dict[str, dict]] = {}


def enrich(route_id: str, buses: list[dict], line: LineString | None, fetched_at: float) -> list[tuple]:
    """각 버스를 노선 위로 보정하고, 연속된 *서로 다른* 두 보고로 방위각과 정차 여부를 계산함.
    `buses`를 직접 수정하고, observations 테이블에 넣을 행들을 반환함.

    - dataTm이 이전과 같으면 API가 이 버스를 아직 갱신하지 않은 것: 이전 방위각/정차 값을 유지.
    - 두 보고 사이 이동이 STOP_THRESHOLD_M 미만이면 정차 (방위각은 유지).
    - 버스의 첫 보고: 두 번째 보고가 올 때까지 방위각/정차는 None.
    """
    state = route_state.setdefault(route_id, {})
    seen: set[str] = set()
    rows: list[tuple] = []

    for b in buses:
        raw_lat, raw_lng = b["lat"], b["lng"]
        x, y = to_m(raw_lng, raw_lat)
        off, snapped = None, False
        if line is not None:
            p = Point(x, y)
            off = line.distance(p)
            if off <= SNAP_MAX_M:
                q = line.interpolate(line.project(p))
                x, y, snapped = q.x, q.y, True

        veh, data_ts = b["vehId"], parse_ts(b["dataTm"])
        seen.add(veh)
        prev = state.get(veh)
        bearing, stopped = (prev["bearing"], prev["stopped"]) if prev else (None, None)

        changed = prev is None or (
            data_ts is not None and (prev["data_ts"] is None or data_ts > prev["data_ts"])
        )
        if prev is not None and changed:
            moved = math.hypot(x - prev["x"], y - prev["y"])
            if moved > MAX_JUMP_M:
                bearing = stopped = None
            else:
                stopped = moved < STOP_THRESHOLD_M
                if not stopped:  # atan2(dx, dy): 0 = 북쪽, 시계 방향 (leaflet-rotatedmarker가 기대하는 방식)
                    bearing = (math.degrees(math.atan2(x - prev["x"], y - prev["y"])) + 360) % 360
        if changed:
            state[veh] = {"x": x, "y": y, "data_ts": data_ts, "bearing": bearing, "stopped": stopped}
            rows.append((fetched_at, route_id, veh, data_ts, raw_lng, raw_lat, off,
                         None if stopped is None else int(stopped)))

        lng, lat = to_ll(x, y)
        b.update(
            lat=lat, lng=lng, rawLat=raw_lat, rawLng=raw_lng, snapped=snapped,
            bearing=None if bearing is None else round(bearing, 1),
            stopped=stopped, dataTs=data_ts,
        )

    for veh in [v for v in state if v not in seen]:  # 노선에서 사라진 버스: 상태 삭제
        del state[veh]
    return rows


# ---------------------------------------------------------------- 캐시
class UpstreamError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass
class Entry:
    ts: float                    # 이 항목을 저장한 시각 (monotonic)
    ttl: float                   # 신선한 상태로 유지되는 시간
    buses: list | None = None
    error: UpstreamError | None = None
    fetched_at: float = 0.0      # 마지막으로 상위 API 호출이 성공한 실제 시각 (wall-clock)
    stale: bool = False


cache: dict[str, Entry] = {}
locks: dict[str, asyncio.Lock] = {}
path_cache: dict[str, tuple[float, LineString | None]] = {}  # 노선ID -> (저장 시각 monotonic, 노선 선)
quota = {"day": None, "n": 0}


def is_fresh(e: Entry) -> bool:
    return time.monotonic() - e.ts < e.ttl


def take_quota() -> None:
    """data.go.kr가 차단하기 전에 멈추기 위한 자체 일일 호출 카운터."""
    if not DAILY_LIMIT:
        return
    today = datetime.now(KST).date()
    if quota["day"] != today:
        quota["day"], quota["n"] = today, 0
    if quota["n"] >= DAILY_LIMIT:
        raise UpstreamError(503, "Daily upstream quota reached")
    quota["n"] += 1


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
        "stopFlag": int(item["stopFlag"]),  # API가 주는 "정류소 정차 중" 원본 플래그
        "sectOrd": int(item["sectOrd"]),
        "nextStId": item.get("nextStId"),
        "congestion": item.get("congetion"),  # 원문 그대로: API는 "congetion"으로 철자함
        "isRunning": item.get("isrunyn") == "1",
        "dataTm": str(item["dataTm"]),
    }


async def get_json(url: str, route_id: str) -> dict:
    """크기 제한과 동시 호출 제한이 걸린 상위 API GET 1회. URL에 키가 있으므로 URL은 절대 로그에 남기지 않음."""
    try:
        async with upstream_sem:
            async with client.stream(
                "GET",
                url,
                params={"serviceKey": SERVICE_KEY, "busRouteId": route_id, "resultType": "json"},
            ) as r:
                r.raise_for_status()
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_UPSTREAM_BYTES:
                        raise UpstreamError(502, "Upstream response too large")
        return json.loads(body)
    except UpstreamError:
        raise
    except (httpx.HTTPError, ValueError) as e:
        # 예외 타입만 로그에 남김: httpx의 str(e)에는 (키가 포함된) 전체 URL이 들어갈 수 있음.
        log.warning("upstream request failed route=%s error=%s", route_id, type(e).__name__)
        raise UpstreamError(502, "Upstream request failed")


def parse_envelope(data: dict, route_id: str) -> list[dict]:
    """msgHeader를 확인하고 원본 itemList를 (항상 리스트로) 반환."""
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
        return []  # 오늘 운행 종료
    elif code == "6":
        raise UpstreamError(503, "Realtime data temporarily unavailable")
    else:
        # 상위 API의 메시지는 서버 로그에만 남기고 응답에는 넣지 않음.
        log.warning("upstream error route=%s code=%s msg=%s", route_id, code, header.get("headerMsg"))
        raise UpstreamError(502, "Upstream error")

    body_part = data.get("msgBody") if isinstance(data, dict) else None
    items = body_part.get("itemList") if isinstance(body_part, dict) else None
    items = items or []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise UpstreamError(502, "Malformed upstream response")
    return items


async def call_api(url: str, route_id: str, kind: str) -> list[dict]:
    """get_json + parse_envelope 를 실행하고 결과(성공 여부, 지연, 오류)를 fetch_log에 기록."""
    take_quota()  # 자체 상한은 상위 API 실패가 아니므로 오류로 기록하지 않음
    log.info("upstream call kind=%s route=%s", kind, route_id)
    t0, err = time.perf_counter(), "aborted"
    try:
        items = parse_envelope(await get_json(url, route_id), route_id)
        err = None
        return items
    except UpstreamError as e:
        err = f"{e.status} {e.detail}"
        raise
    except Exception as e:
        err = type(e).__name__
        raise
    finally:
        db_log(SQL_FETCH, [(time.time(), route_id, kind, int(err is None),
                            (time.perf_counter() - t0) * 1000, err)])


async def fetch_route(route_id: str) -> list:
    items = await call_api(f"{BASE}/getBusPosByRtid", route_id, "pos")
    buses, skipped = [], 0
    for item in items[:MAX_ITEMS_PER_ROUTE]:
        try:
            buses.append(normalize(item))
        except (KeyError, TypeError, ValueError):
            skipped += 1  # 잘못된 항목 하나 때문에 노선 전체가 실패하면 안 됨
    if skipped:
        log.warning("route=%s skipped %d malformed items", route_id, skipped)
    return buses


async def get_line(route_id: str) -> LineString | None:
    """미터 단위 노선 선 (하루 동안 캐시). 실패해도 당분간 보정만 하지 않을 뿐임."""
    hit = path_cache.get(route_id)
    if hit and time.monotonic() - hit[0] < (PATH_TTL if hit[1] is not None else PATH_ERROR_TTL):
        return hit[1]
    line = None
    try:
        items = await call_api(f"{PATH_BASE}/getRoutePath", route_id, "path")
        pts: list[tuple[float, float]] = []
        for it in items[:20_000]:
            try:
                p = to_m(float(it["gpsX"]), float(it["gpsY"]))
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(p[0]) and math.isfinite(p[1]) and (not pts or p != pts[-1]):
                pts.append(p)
        line = LineString(pts) if len(pts) >= 2 else None
    except UpstreamError:
        log.warning("route path unavailable route=%s; running without snapping", route_id)
    path_cache[route_id] = (time.monotonic(), line)
    return line


def evict_if_needed() -> None:
    while len(cache) > MAX_ROUTES:
        oldest = min(cache, key=lambda k: cache[k].ts)
        cache.pop(oldest)
        path_cache.pop(oldest, None)
        route_state.pop(oldest, None)
        lock = locks.get(oldest)
        if lock and not lock.locked():
            locks.pop(oldest, None)


async def get_route(route_id: str) -> Entry:
    entry = cache.get(route_id)
    if entry and is_fresh(entry):
        return entry

    lock = locks.setdefault(route_id, asyncio.Lock())
    async with lock:  # 노선당 한 번에 상위 API 호출은 하나만
        old = cache.get(route_id)
        if old and is_fresh(old):  # 기다리는 사이에 다른 요청이 이미 갱신함
            return old

        now = time.monotonic()
        try:
            buses = await fetch_route(route_id)
            line = await get_line(route_id) if buses else None
            fetched_at = time.time()
            db_log(SQL_OBS, enrich(route_id, buses, line, fetched_at))
            new = Entry(ts=now, ttl=TTL, buses=buses, fetched_at=fetched_at)
        except UpstreamError as e:
            if e.status == 404:
                new = Entry(ts=now, ttl=NOT_FOUND_TTL, error=e)
            elif old and old.buses is not None and time.time() - old.fetched_at < STALE_MAX:
                # API 실패: 마지막으로 성공한 데이터를 계속 제공하고 ERROR_TTL 뒤에 재시도
                new = Entry(ts=now, ttl=ERROR_TTL, buses=old.buses,
                            fetched_at=old.fetched_at, stale=True)
            else:
                new = Entry(ts=now, ttl=ERROR_TTL, error=e)
        except Exception:
            # 예상치 못한 오류: 로그만 남기고 세부 내용은 클라이언트에 절대 노출하지 않음.
            log.exception("unexpected error fetching route=%s", route_id)
            new = Entry(ts=now, ttl=ERROR_TTL, error=UpstreamError(502, "Upstream request failed"))

        cache[route_id] = new
        evict_if_needed()
        return new


# ---------------------------------------------------------------- 터미널 출력
CONGESTION = {"0": "-", "3": "calm", "4": "normal", "5": "crowded", "6": "very crowded"}


def clean(value, width: int) -> str:
    """상위 API의 텍스트가 터미널로 출력되므로 제어 문자/이스케이프 코드를 제거."""
    return re.sub(r"[^\w\- ]", "?", str(value))[:width]


def fmt_time(tm: str) -> str:
    # dataTm은 20261121103000 형식 (YYYYMMDDhhmmss)
    if re.fullmatch(r"[0-9]{14}", tm or ""):
        return f"{tm[8:10]}:{tm[10:12]}:{tm[12:14]}"
    return clean(tm, 14)


last_route: str | None = None  # 가장 최근에 요청된 노선, 카운트다운에 표시됨


async def countdown_loop() -> None:
    """터미널 한 줄을 매초 다시 써서 캐시 만료까지 남은 시간을 보여 줌.

    상위 API는 캐시가 만료된 뒤 요청이 도착할 때만 호출되므로,
    0이 되면 "다음 요청이 API를 호출한다"고 표시함.
    팁: uvicorn을 --no-access-log로 실행하면 uvicorn 로그가 이 줄에 섞이지 않음.
    """
    while True:
        await asyncio.sleep(1)
        entry = cache.get(last_route) if last_route else None
        if entry is None or entry.buses is None:
            continue
        left = entry.ttl - (time.monotonic() - entry.ts)
        if left > 0:
            msg = f"route {last_route}: next API call in {int(left) + 1:>2}s"
        else:
            msg = f"route {last_route}: cache expired - next request calls the API"
        sys.stdout.write(f"\r\033[K  ⏱  {msg}")
        sys.stdout.flush()


def print_buses(route_id: str, entry: Entry) -> None:
    if not PRINT_BUSES or entry.buses is None:
        return
    age = round(time.time() - entry.fetched_at)
    note = " (STALE)" if entry.stale else ""
    left = max(0, int(entry.ttl - (time.monotonic() - entry.ts)) + 1)
    # "\r\033[K"는 표를 출력하기 전에 카운트다운 줄을 지움
    print(f"\r\033[K\n=== Route {route_id} | {len(entry.buses)} buses | data age {age}s{note} "
          f"| next API call in {left}s ===")
    if not entry.buses:
        print("  no buses running")
        return
    print(f"  {'PLATE':<12} {'LAT':>10} {'LNG':>11}  {'STATUS':<8} {'MOVE':<7} {'BRG':>5}  "
          f"{'NEXT STOP':<10} {'LOAD':<13} TIME")
    for b in entry.buses:
        status = "at stop" if b["stopFlag"] == 1 else "moving"
        if not b["isRunning"]:
            status = "parked"
        move = "-" if b["stopped"] is None else ("stopped" if b["stopped"] else "moving")
        brg = "-" if b["bearing"] is None else f"{b['bearing']:.0f}"
        load = CONGESTION.get(str(b["congestion"]), clean(b["congestion"] or "-", 12))
        print(
            f"  {clean(b['plate'], 12):<12} {b['lat']:>10.5f} {b['lng']:>11.5f}  "
            f"{status:<8} {move:<7} {brg:>5}  {clean(b['nextStId'] or '-', 10):<10} "
            f"{load:<13} {fmt_time(b['dataTm'])}"
        )
    print(flush=True)


# ---------------------------------------------------------------- 라우트
@app.get("/api/buses/{route_id}", dependencies=[Depends(rate_limit)])
async def buses(route_id: str, response: Response):
    # 캐시, 락, 상위 API에 손대기 전에 먼저 검증.
    if not ROUTE_ID_RE.fullmatch(route_id):
        raise HTTPException(400, "Invalid route id")
    if ALLOWED_ROUTES and route_id not in ALLOWED_ROUTES:
        raise HTTPException(403, "Route not allowed")

    entry = await get_route(route_id)
    if entry.error:
        raise HTTPException(entry.error.status, entry.error.detail)

    global last_route
    last_route = route_id
    print_buses(route_id, entry)

    now = time.time()
    out = []
    for b in entry.buses:
        ts = b["dataTs"]
        out.append({
            **{k: v for k, v in b.items() if k != "dataTs"},
            "dataAgeSec": None if ts is None else max(0, round(now - ts)),  # "n초 전 위치" 표시에 쓰는 값
        })

    remaining = max(0, int(entry.ttl - (time.monotonic() - entry.ts)))
    response.headers["Cache-Control"] = f"public, max-age={remaining}"
    return {
        "routeId": route_id,
        "buses": out,
        "stale": entry.stale,
        "ageSeconds": round(now - entry.fetched_at),
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)
