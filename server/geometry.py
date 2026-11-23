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
