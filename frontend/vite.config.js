import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  root: "frontend",
  plugins: [vue()],
  optimizeDeps: {
    noDiscovery: true,
    include: []
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:5000",
        changeOrigin: true
      },
      "/callback": {
        target: "http://127.0.0.1:5000",
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: "../frontend_dist",
    emptyOutDir: true,
    sourcemap: false
  }
});
