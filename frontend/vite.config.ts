import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During `npm run dev`, forward API/WS calls to the FastAPI server on :8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/games": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/admin": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
});
