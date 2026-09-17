import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// In development the dashboard calls /api on its own origin and Vite forwards
// it to the FastAPI server, so no CORS setup is needed locally. In a deployed
// build, VITE_API_URL points the client at the API directly.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const proxyTarget = env.VITE_DEV_API_PROXY || "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": { target: proxyTarget, changeOrigin: true },
      },
    },
  };
});
