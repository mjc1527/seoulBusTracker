import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 개발 서버 설정
// 브라우저가 /api/... 로 요청하면 Vite가 우리 FastAPI 서버(8000번 포트)로 넘겨 준다.
// 그래서 화면 코드에는 서버 주소를 쓰지 않고 "/api/buses/..."처럼만 쓰면 된다.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
