import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Bind IPv4 explicitly. Without this, Vite can take :5173 on IPv6 while another
    // project already holds 127.0.0.1:5173 -- two dev servers answering the same port.
    host: "127.0.0.1",
    // If another build already holds :5173, fall forward instead of failing.
    strictPort: false,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:3001",
        changeOrigin: true,
      },
    },
  },
});
