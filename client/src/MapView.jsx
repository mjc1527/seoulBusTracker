import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

// 지도를 처음 보여 줄 위치와 확대 정도 (서울시청 부근)
const SEOUL_CENTER = [37.5665, 126.978];
const START_ZOOM = 12;

// 지도 타일: 브라우저가 CARTO에서 직접 받는다. (우리 서버는 타일을 중계하지 않는다.)
// {z}/{x}/{y}는 확대 정도와 타일 위치이다.
// CARTO 키는 client/.env의 VITE_CARTO_KEY에서 읽는다. (.env는 커밋하지 않는다)
const CARTO_KEY = import.meta.env.VITE_CARTO_KEY;
const TILE_URL = `https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png?key=${CARTO_KEY}`;

// 저작자 표시: 지우면 안 된다. (OpenStreetMap, CARTO 이용 조건)
const TILE_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors ' +
  '&copy; <a href="https://carto.com/attributions">CARTO</a>';

export default function MapView() {
  // useRef: 화면이 다시 그려져도 값이 유지되는 상자
  const containerRef = useRef(null); // 지도가 들어갈 <div>
  const mapRef = useRef(null); // 만든 Leaflet 지도 객체

  // useEffect: 화면에 <div>가 생긴 뒤에 한 번 실행된다.
  useEffect(() => {
    if (mapRef.current) return; // 이미 지도를 만들었으면 또 만들지 않는다.

    const map = L.map(containerRef.current).setView(SEOUL_CENTER, START_ZOOM);
    L.tileLayer(TILE_URL, {
      attribution: TILE_ATTRIBUTION,
      maxZoom: 20,
    }).addTo(map);
    mapRef.current = map;

    // 화면에서 사라질 때 지도를 정리한다.
    // (개발 모드의 StrictMode는 일부러 한 번 만들고 지웠다가 다시 만든다. 이 정리가 없으면 오류가 난다.)
    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  return <div ref={containerRef} className="map" />;
}
