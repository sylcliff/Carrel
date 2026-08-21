import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    // Listen on all interfaces so the dev server is reachable from other
    // devices on the LAN (e.g. http://192.168.1.201:5173). API and storage
    // requests are still proxied to 127.0.0.1:8787 server-side below.
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      // Strip the /api prefix so the backend routes (which are /health, /papers,
      // /sync, /subscriptions) work without an /api prefix in the code.
      "/api": {
        target: "http://127.0.0.1:8787",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      // Parsed paper images are served from /storage on the backend.
      "/storage": {
        target: "http://127.0.0.1:8787",
        changeOrigin: true,
      },
    },
  },
});
