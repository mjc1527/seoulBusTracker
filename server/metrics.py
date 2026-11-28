"""서버가 남긴 기록(bus_log.db)을 읽어 측정 지표를 계산한다.

사용법 (server 폴더에서):

    python metrics.py                                   # bus_log.db를 읽어 표를 화면에 출력
    python metrics.py bus_log.db --out ../results/결과.md  # 표를 파일로 저장

읽기 전용으로 열기 때문에 서버가 켜져 있어도 기록을 건드리지 않는다.
새 라이브러리는 쓰지 않는다. (파이썬 기본 sqlite3·statistics와 geometry.py)
"""
import argparse
import math
import sqlite3
import statistics
from collections import Counter, defaultdict, namedtuple
from datetime import datetime

from geometry import distance_m, is_stopped

# server/main.py가 만드는 두 표의 한 줄
Fetch = namedtuple("Fetch", "ts route_id kind ok latency_ms error")
Obs = namedtuple("Obs", "ts route_id veh_id data_ts lng lat off_line_m stopped")

# server/main.py가 좌표를 미터로 바꿀 때 쓰는 평면 근사 상수 (비교용으로 같은 값을 둔다)
_LAT0 = 37.55
_KX = 111_320 * math.cos(math.radians(_LAT0))
_KY = 110_540


def planar_distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """서버와 같은 평면 근사로 잰 두 좌표 사이의 거리(m). 구면 공식과 비교할 때만 쓴다."""
    return math.hypot((lng2 - lng1) * _KX, (lat2 - lat1) * _KY)


def percentile(values: list[float], p: float) -> float:
    """작은 값부터 세어 p%째에 있는 값. 사이에 걸리면 두 값 사이를 비례로 나눈다."""
    ordered = sorted(values)
    k = (len(ordered) - 1) * p / 100
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def summarize(values: list[float]) -> dict | None:
    """개수, 평균, 중앙값, 95%째 값, 최소, 최대. 값이 없으면 None."""
    if not values:
        return None
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "p95": percentile(values, 95),
        "min": min(values),
        "max": max(values),
    }


def load(db_path: str) -> tuple[list[Fetch], list[Obs]]:
    """bus_log.db를 읽기 전용으로 열어 두 표를 통째로 읽는다."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        fetches = [Fetch(*row) for row in db.execute(
            "select ts, route_id, kind, ok, latency_ms, error from fetch_log order by ts")]
        observations = [Obs(*row) for row in db.execute(
            "select ts, route_id, veh_id, data_ts, lng, lat, off_line_m, stopped"
            " from observations order by ts")]
    finally:
        db.close()
    return fetches, observations


def pairs_by_vehicle(observations: list[Obs]) -> list[tuple[Obs, Obs]]:
    """같은 차량의 관측을 시간순으로 놓고, 이웃한 두 관측을 (이전, 지금) 쌍으로 묶는다."""
    by_vehicle = defaultdict(list)
    for o in observations:
        by_vehicle[o.veh_id].append(o)
    pairs = []
    for rows in by_vehicle.values():
        rows.sort(key=lambda o: o.ts)
        pairs.extend(zip(rows, rows[1:]))
    return pairs


def new_data_share(poll_times: list[float], observations: list[Obs]) -> float | None:
    """차량이 노선에 있던 동안의 호출 가운데, 그 차량의 새 좌표가 온 호출의 비율(0~1).

    서버는 좌표를 잰 시각(dataTm)이 바뀐 때에만 observations에 한 줄을 남긴다.
    그래서 (관측 수) / (그 차량이 보이던 동안의 호출 수)가 새 좌표가 온 비율이다.
    """
    by_vehicle = defaultdict(list)
    for o in observations:
        by_vehicle[o.veh_id].append(o.ts)
    polls = seen = 0
    for times in by_vehicle.values():
        first, last = min(times) - 1, max(times) + 1  # 기록 시각이 조금 어긋나는 것을 감안해 1초 여유
        polls += sum(first <= t <= last for t in poll_times)
        seen += len(times)
    return seen / polls if polls else None


def compute(fetches: list[Fetch], observations: list[Obs], threshold_m: float = 15.0) -> dict:
    """지표를 모두 계산해 사전 하나로 돌려준다. threshold_m은 서버와 비교할 정차 기준(m)."""
    pos = [f for f in fetches if f.kind == "pos"]
    poll_times = [f.ts for f in pos]
    poll_gaps = [b - a for a, b in zip(poll_times, poll_times[1:])]
    pairs = pairs_by_vehicle(observations)

    # 연속한 두 관측 사이의 거리: geometry.py(구면 공식)와 서버식 평면 근사
    moved = [distance_m(a.lat, a.lng, b.lat, b.lng) for a, b in pairs]
    planar = [planar_distance_m(a.lat, a.lng, b.lat, b.lng) for a, b in pairs]

    # 서버의 stopped와 geometry.is_stopped 비교 (서버가 판정을 남긴 쌍만)
    judged = [(a, b) for a, b in pairs if b.stopped is not None]
    agree = sum(bool(b.stopped) == is_stopped(a.lat, a.lng, b.lat, b.lng, threshold_m) for a, b in judged)

    # 한 차량의 위치가 바뀌는 간격을 "호출 몇 번 만인지"로 센다
    typical_gap = statistics.median(poll_gaps) if poll_gaps else None
    update_every = Counter(round((b.ts - a.ts) / typical_gap) for a, b in pairs) if typical_gap else Counter()

    return {
        "routes": sorted({f.route_id for f in fetches}),
        "start": min(poll_times) if poll_times else None,
        "end": max(poll_times) if poll_times else None,
        "calls": len(pos),
        "calls_ok": sum(f.ok for f in pos),
        "errors": Counter(f.error for f in pos if not f.ok),
        "poll_gap": summarize(poll_gaps),
        "latency_ms": summarize([f.latency_ms for f in pos if f.ok]),
        "observations": len(observations),
        "vehicles": len({o.veh_id for o in observations}),
        "update_interval_s": summarize(
            [b.data_ts - a.data_ts for a, b in pairs if a.data_ts is not None and b.data_ts is not None]),
        "new_data_share": new_data_share(poll_times, observations),
        "update_every": dict(sorted(update_every.items())),
        "delay_s": summarize([o.ts - o.data_ts for o in observations if o.data_ts is not None]),
        "off_line_m": summarize([o.off_line_m for o in observations if o.off_line_m is not None]),
        "moved_m": summarize(moved),
        "pairs": len(pairs),
        "stopped_under": {t: sum(d < t for d in moved) for t in (10, 15)},
        "threshold_m": threshold_m,
        "judged": len(judged),
        "agree": agree,
        "method_same": sum((s < threshold_m) == (p < threshold_m) for s, p in zip(moved, planar)),
        "method_max_diff_pct": max((abs(s - p) / s * 100 for s, p in zip(moved, planar) if s > 1), default=0.0),
    }


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _row(name: str, s: dict | None, unit: str, digits: int = 1) -> str:
    if s is None:
        return f"| {name} | 자료 없음 | | | | |"
    f = f"{{:.{digits}f}}"
    return (f"| {name} | {f.format(s['mean'])} {unit} | {f.format(s['median'])} {unit} |"
            f" {f.format(s['p95'])} {unit} | {f.format(s['max'])} {unit} | {s['n']:,} |")


def format_report(r: dict) -> str:
    """compute()의 결과를 마크다운 표로 만든다."""
    if not r["calls"]:
        return "기록이 없다. 서버를 켜고 collect.py로 먼저 수집한다.\n"
    minutes = (r["end"] - r["start"]) / 60
    gap = r["poll_gap"]["median"] if r["poll_gap"] else 0
    lines = [
        "## 측정 조건",
        "",
        f"- 노선: {', '.join(r['routes'])}",
        f"- 기간: {_clock(r['start'])} ~ {_clock(r['end'])} ({minutes:.1f}분)",
        f"- 호출: {r['calls']:,}회, 간격 중앙값 {gap:.1f}초",
        f"- 관측: {r['observations']:,}건, 차량 {r['vehicles']}대",
        "",
        "## 호출",
        "",
        f"- 성공률: {r['calls_ok']:,} / {r['calls']:,} ({100 * r['calls_ok'] / r['calls']:.1f}%)",
    ]
    if r["errors"]:
        lines.append("- 오류: " + ", ".join(f"{e} {n}건" for e, n in r["errors"].most_common()))
    lines += [
        "",
        "## 지표",
        "",
        "| 지표 | 평균 | 중앙값 | 95% | 최대 | 개수 |",
        "|---|---|---|---|---|---|",
        _row("응답 시간", r["latency_ms"], "ms", 0),
        _row("갱신 간격 (같은 차량의 좌표 시각 차이)", r["update_interval_s"], "초"),
        _row("지연 (좌표 시각 → 서버가 받은 시각)", r["delay_s"], "초"),
        _row("노선 이탈 거리", r["off_line_m"], "m"),
        _row("이동 거리 (연속한 두 관측 사이)", r["moved_m"], "m"),
        "",
    ]
    if r["new_data_share"] is not None:
        lines += [
            "## 새 좌표가 온 호출",
            "",
            f"- 새 좌표가 온 비율: {100 * r['new_data_share']:.1f}%"
            f" (새 좌표가 없던 호출 {100 * (1 - r['new_data_share']):.1f}%)",
            "- 한 차량의 위치가 바뀌는 간격:",
        ]
        total = sum(r["update_every"].values())
        for k, n in r["update_every"].items():
            lines.append(f"  - 호출 {k}번 만에(약 {k * gap:.0f}초): {n:,}건 ({100 * n / total:.1f}%)")
        lines.append("")
    if r["pairs"]:
        t = r["threshold_m"]
        lines += [
            "## 정차 판정",
            "",
            f"- 연속한 두 관측 {r['pairs']:,}쌍 가운데 이동 거리가 기준보다 짧은 비율:",
        ]
        for th, n in r["stopped_under"].items():
            lines.append(f"  - 기준 {th} m: {n:,}쌍 ({100 * n / r['pairs']:.1f}%)")
        if r["judged"]:
            lines.append(
                f"- 서버의 `stopped`와 `geometry.is_stopped`(원좌표, 기준 {t:g} m)가 같은 쌍:"
                f" {r['agree']:,} / {r['judged']:,} ({100 * r['agree'] / r['judged']:.1f}%)")
        lines.append(
            f"- 같은 원좌표에서 구면 공식과 평면 근사의 판정(기준 {t:g} m)이 같은 쌍:"
            f" {r['method_same']:,} / {r['pairs']:,} (거리 차이 최대 {r['method_max_diff_pct']:.2f}%)")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="bus_log.db에서 측정 지표를 계산한다.")
    parser.add_argument("db", nargs="?", default="bus_log.db", help="기록 파일 (기본: bus_log.db)")
    parser.add_argument("--threshold", type=float, default=15.0, help="서버와 비교할 정차 기준(m). 기본 15")
    parser.add_argument("--out", help="표를 저장할 파일. 없으면 화면에 출력한다")
    args = parser.parse_args()

    fetches, observations = load(args.db)
    report = format_report(compute(fetches, observations, args.threshold))
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(report)
        print(f"저장함: {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
