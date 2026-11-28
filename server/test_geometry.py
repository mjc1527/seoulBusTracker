import pytest

from geometry import bearing_deg, distance_m, is_stopped


def test_same_point_is_zero():
    # 같은 좌표를 넣으면 거리는 0이다.
    assert distance_m(37.5, 127.0, 37.5, 127.0) == 0


def test_one_degree_of_latitude_is_about_111km():
    # 위도 1도는 약 111.2 km다. 10 m 안에서 맞으면 통과.
    assert distance_m(37.0, 127.0, 38.0, 127.0) == pytest.approx(111_195, abs=10)


def test_order_does_not_matter():
    # 두 점의 순서를 바꿔도 거리는 같다.
    forward = distance_m(37.0, 127.0, 38.0, 127.5)
    backward = distance_m(38.0, 127.5, 37.0, 127.0)
    assert forward == pytest.approx(backward)


def test_bearing_north_is_0():
    # 위도만 커지면 정북이다.
    assert bearing_deg(37.5, 127.0, 37.6, 127.0) == pytest.approx(0, abs=0.01)


def test_bearing_east_is_about_90():
    # 경도만 커지면 정동이다. 지구가 둥글어서 90에서 아주 조금 어긋난다.
    assert bearing_deg(37.5, 127.0, 37.5, 127.01) == pytest.approx(90, abs=0.1)


def test_bearing_south_is_180():
    # 위도만 작아지면 정남이다.
    assert bearing_deg(37.5, 127.0, 37.4, 127.0) == pytest.approx(180, abs=0.01)


def test_bearing_west_is_about_270():
    # 경도만 작아지면 정서다.
    assert bearing_deg(37.5, 127.0, 37.5, 126.99) == pytest.approx(270, abs=0.1)


def test_bearing_is_between_0_and_360():
    # 어느 방향이든 결과는 0 이상 360 미만이다.
    here = (37.5, 127.0)
    others = [(37.6, 127.1), (37.4, 127.1), (37.4, 126.9), (37.6, 126.9), (37.6, 127.0)]
    for lat, lng in others:
        assert 0 <= bearing_deg(*here, lat, lng) < 360


def test_bearing_same_point_is_0():
    # 두 점이 같으면 0을 반환한다. (방향의 의미는 없다)
    assert bearing_deg(37.5, 127.0, 37.5, 127.0) == 0


def test_stopped_same_point():
    # 같은 좌표면 거리가 0이므로 정차다.
    assert is_stopped(37.5, 127.0, 37.5, 127.0) is True


def test_stopped_when_moved_less_than_threshold():
    # 위도 0.00005도는 약 5.6 m다. 기준 10 m보다 짧으므로 정차다.
    assert is_stopped(37.5, 127.0, 37.50005, 127.0) is True


def test_not_stopped_when_moved_more_than_threshold():
    # 위도 0.0005도는 약 55.6 m다. 기준 10 m보다 길므로 이동이다.
    assert is_stopped(37.5, 127.0, 37.5005, 127.0) is False


def test_stopped_uses_given_threshold():
    # 같은 55.6 m라도 기준을 100 m로 주면 정차다.
    assert is_stopped(37.5, 127.0, 37.5005, 127.0, threshold_m=100) is True
