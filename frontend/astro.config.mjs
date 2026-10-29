import { defineConfig } from 'astro/config';

export default defineConfig({
  devToolbar: { enabled: false },
  vite: {
    server: {
      proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false } },
    },
  },
});
