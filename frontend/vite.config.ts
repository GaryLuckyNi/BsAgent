import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('oidc-client-ts')) return 'vendor-auth'
          if (id.includes('react-router')) return 'vendor-router'
          if (
            /\/(streamdown|react-markdown|remark-|rehype-|unified|micromark|mdast|hast|unist|vfile|property-information|html-url-attributes|web-namespaces)/.test(id) ||
            id.includes('/@streamdown/')
          ) return 'vendor-markdown'
          if (
            id.includes('/node_modules/react/') ||
            id.includes('/node_modules/react-dom/') ||
            id.includes('/node_modules/scheduler/')
          ) return 'vendor-react'
          if (id.includes('/radix-ui/') || id.includes('/@radix-ui/') || id.includes('/cmdk/')) return 'vendor-primitives'
          if (id.includes('/lucide-react/')) return 'vendor-icons'
          if (id.includes('/node_modules/ai/') || id.includes('/@ai-sdk/')) return 'vendor-ai'
          return 'vendor-ui'
        }
      }
    }
  },
  server: {
    host: '127.0.0.1',
    port: 5175,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true
      },
      '/health': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true
      }
    }
  }
})
