import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API serves index.html at "/" and the bundle at "/assets/*".
// In development, proxy API calls to a local backend (override with COCKPIT_API).
const api = process.env.COCKPIT_API ?? "http://localhost:8080";

export default defineConfig({
  base: "/",
  plugins: [react()],
  build: { outDir: "dist", assetsDir: "assets", sourcemap: false },
  server: {
    proxy: process.env.VITE_MOCK
      ? undefined
      : {
          "/api": api,
          "/auth": api,
          "/healthz": api,
        },
  },
});
