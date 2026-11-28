# seoulBusTracker
# seoulBusTracker

## 화면(client) 실행 방법

Ubuntu(WSL 포함) 기준이다. Node.js 22 이상을 권장한다(`node -v`로 확인). Vite가 요구하는 정확한 버전은 설치한 Vite 버전의 공식 문서에서 확인한다.

```bash
cd client
npm install        # 처음 한 번만. package.json에 적힌 라이브러리를 설치한다.
npm run dev        # 개발 서버 실행
```

브라우저에서 http://localhost:5173 을 열면 서울 지도가 보인다.
화면이 `/api/...`로 보내는 요청은 개발 서버가 `http://127.0.0.1:8000`(우리 FastAPI 서버)으로 넘겨 준다(`client/vite.config.js`).

## 사용한 오픈소스와 라이선스

| 이름 | 용도 | 라이선스 | 출처 |
|---|---|---|---|
| React | 화면 구성 | MIT (확인 필요) | https://github.com/facebook/react |
| Vite | 화면 개발 서버·빌드 | MIT | https://github.com/vitejs/vite |
| Leaflet | 지도 표시 | BSD-2-Clause | https://github.com/Leaflet/Leaflet |

## 데이터와 지도 출처

- 버스 위치 데이터: 서울특별시 (공공데이터포털)
- 지도: © OpenStreetMap contributors, © CARTO
