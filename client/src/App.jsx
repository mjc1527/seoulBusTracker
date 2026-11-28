import MapView from "./MapView.jsx";

export default function App() {
  return (
    <div className="app">
      <header className="header">
        <h1>서울 실시간 버스 위치</h1>
      </header>
      <MapView />
      <footer className="footer">
        버스 위치 데이터: 서울특별시 (공공데이터포털)
      </footer>
    </div>
  );
}
