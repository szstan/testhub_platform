import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { resolve } from 'path'

const curlconverterBrowserParser = {
  name: 'curlconverter-browser-parser',
  enforce: 'pre',
  resolveId(source, importer) {
    const normalizedImporter = importer?.replaceAll('\\', '/')
    if (
      source === './Parser.js' &&
      normalizedImporter?.endsWith('/curlconverter/dist/src/shell/tokenizer.js')
    ) {
      return resolve(__dirname, 'node_modules/curlconverter/dist/src/shell/webParser.js')
    }
    return null
  },
}

export default defineConfig({
  envDir: resolve(__dirname, '..'),
  plugins: [
    vue(),
    Components({ resolvers: [ElementPlusResolver()] }),
    curlconverterBrowserParser,
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
      fs: resolve(__dirname, 'src/shims/empty-node-module.js'),
      path: resolve(__dirname, 'src/shims/empty-node-module.js'),
    },
  },
  css: {
    preprocessorOptions: {
      scss: {
        api: 'modern-compiler', // 使用现代 Sass API
        silenceDeprecations: ['legacy-js-api'], // 静默旧警告
      }
    }
  },
  optimizeDeps: {
    esbuildOptions: {
      target: 'es2022'
    },
    force: true,
    exclude: ['tree-sitter', 'web-tree-sitter'],
  },
  build: {
    target: 'es2022',
    rollupOptions: {
      output: {
        manualChunks(id) {
          const normalizedId = id.replaceAll('\\', '/')
          if (normalizedId.includes('/src/locales/')) {
            return 'locales'
          }
          if (normalizedId.includes('/node_modules/zrender/')) {
            return 'vendor-echarts-renderers'
          }
          if (normalizedId.includes('/node_modules/echarts/')) {
            return 'vendor-echarts-core'
          }
          if (normalizedId.includes('/node_modules/vue/') ||
              normalizedId.includes('/node_modules/vue-router/') ||
              normalizedId.includes('/node_modules/pinia/')) {
            return 'vendor-vue'
          }
          if (normalizedId.includes('/node_modules/@element-plus/icons-vue/')) {
            return 'vendor-element'
          }
          if (normalizedId.includes('/node_modules/monaco-editor/')) {
            return 'vendor-monaco'
          }
          if (normalizedId.includes('/node_modules/xlsx/')) {
            return 'vendor-xlsx'
          }
          return undefined
        },
      },
      onwarn(warning, warn) {
        if (
          warning.code === 'EVAL' &&
          warning.id?.replaceAll('\\', '/').includes('/node_modules/web-tree-sitter/')
        ) {
          return
        }
        warn(warning)
      },
    },
  },
  server: {
    port: 3000,
    host: '0.0.0.0',
    headers: {
      'Cache-Control': 'no-cache, no-store, must-revalidate',
      'Pragma': 'no-cache',
      'Expires': '0',
    },
    proxy: {
      '^/api/': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        secure: false,
      },
      '^/media/': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        secure: false,
      },
      '^/app-automation-templates/': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        secure: false,
      },
      '^/app-automation-reports/': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        secure: false,
      },
      '^/ws/': {
        target: 'ws://127.0.0.1:8000',
        ws: true,
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('error', () => {})
          proxy.on('proxyReqWs', (proxyReq, req, socket) => {
            socket.on('error', () => {})
          })
        },
      },
    },
  },
  assetsInclude: ['**/*.wasm'],
})
