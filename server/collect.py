"""우리 서버를 정해진 간격으로 불러 bus_log.db에 기록이 쌓이게 한다.

서버(main.py)는 요청이 올 때만 서울시 API를 부른다. 그래서 일정한 간격의 기록을
얻으려면 누군가 일정한 간격으로 서버를 불러야 한다. 이 스크립트가 그 일을 한다.
서울시 API를 직접 부르지 않는다. 기록은 서버가 남긴다.

사용법 (서버를 먼저 켠 뒤, 다른 터미널의 server 폴더에서):

    python collect.py 100100118                           # 15초 간격으로 120번 (30분)
    python collect.py 100100118 --count 90 --interval 20  # 20초 간격으로 90번 (30분)

주의: 서버의 캐시 시간보다 긴 간격으로 부르면 부를 때마다 서울시 API가 한 번 호출된다.
서버의 하루 호출 상한(DAILY_LIMIT, 기본 900)을 넘지 않게 횟수를 정한다.
중간에 멈추려면 Ctrl+C.
"""
import argparse
import json
import time
import urllib.error
import urllib.request


def call_once(url: str) -> str:
    """서버를 한 번 부르고, 결과를 한 줄로 요약한다."""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
        note = " (오래된 데이터)" if data.get("stale") else ""
        return f"버스 {len(data.get('buses', []))}대{note}"
    except urllib.error.HTTPError as e:
        return f"서버 오류 {e.code}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return f"실패: {type(e).__name__}"


def main() -> None:
    parser = argparse.ArgumentParser(description="우리 서버를 일정한 간격으로 불러 기록을 쌓는다.")
    parser.add_argument("route_id", help="노선 ID (숫자 9자리, 예: 100100118)")
    parser.add_argument("--count", type=int, default=120, help="부를 횟수 (기본 120)")
    parser.add_argument("--interval", type=float, default=15.0, help="부르는 간격(초) (기본 15)")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="서버 주소")
    args = parser.parse_args()

    url = f"{args.base}/api/buses/{args.route_id}"
    minutes = args.count * args.interval / 60
    print(f"{url} 을 {args.interval:g}초 간격으로 {args.count}번 부른다. (약 {minutes:.0f}분)")
    print(f"서울시 API는 최대 {args.count}번 호출된다.")

    start = time.monotonic()
    try:
        for i in range(args.count):
            print(f"[{i + 1}/{args.count}] {time.strftime('%H:%M:%S')} {call_once(url)}")
            # 부르는 데 걸린 시간만큼 덜 쉬어서 간격이 밀리지 않게 한다
            wait = start + (i + 1) * args.interval - time.monotonic()
            if i + 1 < args.count and wait > 0:
                time.sleep(wait)
    except KeyboardInterrupt:
        print("\n멈춤.")


if __name__ == "__main__":
    main()
