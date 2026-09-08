import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During `npm run dev` the API runs separately on :8000.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist" },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
