import sqlite3

import pytest

from metrics import (
    Fetch,
    Obs,
    compute,
    format_report,
    load,
    new_data_share,
    pairs_by_vehicle,
    percentile,
    summarize,
)


def obs(ts, veh, data_ts, lat, lng=127.0, off=0.5, stopped=None):
    # 테스트용 관측 한 줄을 짧게 만든다.
    return Obs(ts, "100100118", veh, data_ts, lng, lat, off, stopped)


def test_percentile_picks_values_between():
    # 1~5에서 50%째는 3, 100%째는 5, 25%째는 2다.
    values = [5, 1, 4, 2, 3]
    assert percentile(values, 50) == 3
    assert percentile(values, 100) == 5
    assert percentile(values, 25) == 2


def test_summarize_basic_numbers():
    s = summarize([10, 20, 30])
    assert s["n"] == 3
    assert s["mean"] == 20
    assert s["median"] == 20
    assert s["min"] == 10
    assert s["max"] == 30


def test_summarize_empty_is_none():
    # 값이 없으면 계산하지 않고 None을 돌려준다.
    assert summarize([]) is None


def test_pairs_are_made_within_the_same_vehicle():
    # 차량 A의 관측 3개는 2쌍, 차량 B의 관측 1개는 0쌍이다. 서로 다른 차량은 묶지 않는다.
    rows = [obs(0, "A", 0, 37.5), obs(15, "B", 10, 37.6), obs(15, "A", 14, 37.5001), obs(30, "A", 29, 37.5002)]
    pairs = pairs_by_vehicle(rows)
    assert len(pairs) == 2
    assert all(a.veh_id == b.veh_id == "A" for a, b in pairs)


def test_new_data_share_counts_polls_without_new_position():
    # 호출 4번(0, 15, 30, 45초) 가운데 차량 A의 새 좌표는 3번 왔다 → 75%.
    rows = [obs(0, "A", 0, 37.5), obs(30, "A", 20, 37.5001), obs(45, "A", 40, 37.5002)]
    assert new_data_share([0, 15, 30, 45], rows) == pytest.approx(0.75)


def test_compute_update_interval_delay_and_stop_agreement():
    fetches = [Fetch(t, "100100118", "pos", 1, 100.0, None) for t in (0, 15, 30)]
    rows = [
        obs(0, "A", -10, 37.50000),
        obs(15, "A", 8, 37.50005, stopped=1),   # 약 5.6 m 이동 → 서버도 정차
        obs(30, "A", 26, 37.50055, stopped=0),  # 약 55.6 m 이동 → 서버도 이동
    ]
    r = compute(fetches, rows, threshold_m=15.0)

    assert r["calls"] == 3 and r["calls_ok"] == 3
    assert r["update_interval_s"]["mean"] == pytest.approx(18)  # (18 + 18) / 2
    assert r["delay_s"]["mean"] == pytest.approx((10 + 7 + 4) / 3)
    assert r["stopped_under"] == {10: 1, 15: 1}
    assert (r["agree"], r["judged"]) == (2, 2)
    assert r["method_same"] == 2  # 구면 공식과 평면 근사의 판정이 같다


def test_load_reads_the_two_tables(tmp_path):
    # 서버와 같은 모양의 작은 DB 파일을 만들어 읽어 본다.
    path = tmp_path / "bus_log.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE fetch_log(ts REAL, route_id TEXT, kind TEXT, ok INTEGER, latency_ms REAL, error TEXT);
        CREATE TABLE observations(ts REAL, route_id TEXT, veh_id TEXT, data_ts REAL,
                                  lng REAL, lat REAL, off_line_m REAL, stopped INTEGER);
        INSERT INTO fetch_log VALUES (1.0, '100100118', 'pos', 1, 120.5, NULL);
        INSERT INTO observations VALUES (1.0, '100100118', 'A', 0.5, 127.0, 37.5, 0.7, NULL);
        """
    )
    db.commit()
    db.close()

    fetches, observations = load(str(path))
    assert fetches == [Fetch(1.0, "100100118", "pos", 1, 120.5, None)]
    assert observations == [Obs(1.0, "100100118", "A", 0.5, 127.0, 37.5, 0.7, None)]


def test_report_says_when_there_is_no_data():
    assert "기록이 없다" in format_report(compute([], []))
