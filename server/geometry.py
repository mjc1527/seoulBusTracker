import math

EARTH_RADIUS_M = 6_371_000  # 지구 평균 반지름(m)


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """위도·경도(도 단위) 두 쌍 사이의 거리를 미터로 반환하는 함수

        haversine 공식을 쓴다.

        a = sin²(Δφ/2) + cos(φ1) · cos(φ2) · sin²(Δλ/2)

        a                                 두 점이 벌어진 정도 (0 = 같은 점, 1 = 지구 정반대편)
        sin²(Δφ/2)                        남북으로 벌어진 몫
        cos(φ1) · cos(φ2) · sin²(Δλ/2)    동서로 벌어진 몫

    φ(phi)는 위도, λ(lambda)는 경도, Δ는 두 점의 차이이다.
    지구를 공으로 가정한다. (오차는 최대 0.5%)
    """
    # 1. 도 → 라디안 (lat은 도, phi는 라디안)
    phi1 = math.radians(lat1)  # 첫째 점의 위도 φ1
    phi2 = math.radians(lat2)  # 둘째 점의 위도 φ2
    d_phi = math.radians(lat2 - lat1)  # 위도 차이 Δφ
    d_lambda = math.radians(lng2 - lng1)  # 경도 차이 Δλ

    # 2. haversine 공식
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )

    # 3. a → θ → 거리(m). θ는 두 점이 지구 중심에서 벌어진 각도(라디안)다.
    #    √a = sin(θ/2)이므로 θ = 2 · asin(√a)다.
    #    호의 길이 공식(반지름 · θ)으로 지표면을 따라간 거리(m)를 반환한다.
    return EARTH_RADIUS_M * (2 * math.asin(math.sqrt(a)))


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """첫째 점에서 둘째 점을 바라보는 방향(방위각)을 도 단위로 반환하는 함수

        북쪽이 0, 동쪽이 90, 남쪽이 180, 서쪽이 270으로 시계 방향이다.
        결과는 0 이상 360 미만이다.

        x = sin(Δλ) · cos(φ2)
        y = cos(φ1) · sin(φ2) - sin(φ1) · cos(φ2) · cos(Δλ)
        θ = atan2(x, y)

        x    둘째 점이 첫째 점에서 볼 때 동쪽으로 치우친 정도 (서쪽이면 음수)
        y    둘째 점이 첫째 점에서 볼 때 북쪽으로 치우친 정도 (남쪽이면 음수)
        θ    북쪽에서 시계 방향으로 잰 각도

    φ(phi)는 위도, λ(lambda)는 경도, Δ는 두 점의 차이이다.
    두 점이 같으면 0을 반환한다. 이때 0은 방향의 의미가 없다.
    """
    # 1. 도 → 라디안 (lat은 도, phi는 라디안)
    phi1 = math.radians(lat1)  # 첫째 점의 위도 φ1
    phi2 = math.radians(lat2)  # 둘째 점의 위도 φ2
    d_lambda = math.radians(lng2 - lng1)  # 경도 차이 Δλ

    # 2. 방위각 공식
    x = math.sin(d_lambda) * math.cos(phi2)
    y = (
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda)
    )

    # 3. x, y → θ → 방위각(도). θ는 북쪽에서 시계 방향으로 잰 각도(라디안)다.
    #    동쪽으로 x, 북쪽으로 y만큼 간 곳을 바라보는 각도 θ는 tan(θ) = x / y를 만족한다.
    #    그러므로 θ = atan2(x, y)다. θ는 -π ~ π로 나온다. (북쪽 0, 동쪽 π/2, 서쪽 -π/2)
    #    도로 바꾼 뒤 음수를 없애 0 이상 360 미만의 방위각(도)을 반환한다. (-90 → 270)
    theta = math.atan2(x, y)
    return (math.degrees(theta) + 360) % 360
