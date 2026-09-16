import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { spawn, type ChildProcess } from 'node:child_process';
import { resolve } from 'node:path';

function localDataService() {
  let child: ChildProcess | undefined;
  return {
    name: 'local-data-service',
    apply: 'serve' as const,
    async configureServer(server: { httpServer: { once: (event: string, listener: () => void) => void } | null }) {
      if (process.env.VITEST) return;
      let response: Response | undefined;
      try {
        response = await fetch('http://127.0.0.1:8000/health', { signal: AbortSignal.timeout(500) });
      } catch {
        // Start the API below only when no service responds.
      }
      if (response) {
        const health = await response.json().catch(() => null);
        if (!response.ok || health?.api_contract !== 'market-workbench-v2') {
          throw new Error('8000端口的后端版本不兼容。请停止旧项目服务，再运行 scripts/start_local.py；不会自动终止其他进程。');
        }
        return;
      }
      child = spawn(
        'uv',
        ['run', '--locked', '--offline', 'uvicorn', 'backend.api.main:app', '--host', '127.0.0.1', '--port', '8000'],
        { cwd: resolve(import.meta.dirname, '..'), stdio: 'inherit' },
      );
      const stop = () => {
        if (child && child.exitCode === null) child.kill('SIGTERM');
      };
      server.httpServer?.once('close', stop);
      process.once('exit', stop);
    },
  };
}

export default defineConfig({
  plugins: [react(), localDataService()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
