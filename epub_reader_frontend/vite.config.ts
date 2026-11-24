import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  base: '/static/epub_reader/',
  build: {
    outDir: '../static/epub_reader',
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/epub-reader.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/epub-reader.[ext]',
      },
    },
  },
});
