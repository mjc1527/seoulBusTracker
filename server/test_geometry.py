import pytest

from geometry import distance_m


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
