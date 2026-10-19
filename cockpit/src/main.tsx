import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "@xyflow/react/dist/style.css";
import "./styles.css";
import App from "./App";

async function start() {
  // Dev-only fixture backend. `import.meta.env.VITE_MOCK` is replaced at build time, so without
  // VITE_MOCK this branch and the whole src/mock module are dropped from the production bundle.
  if (import.meta.env.VITE_MOCK) {
    const { installMock } = await import("./mock");
    installMock();
  }
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </StrictMode>,
  );
}

void start();
