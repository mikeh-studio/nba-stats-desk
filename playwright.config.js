import fs from 'node:fs';
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests_browser',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30000,
  outputDir: 'reports/ask-harness/browser-artifacts',
  reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:8129', headless: true, trace: 'retain-on-failure' },
  webServer: {
    command: `${fs.existsSync('.venv-app/bin/python') ? '.venv-app/bin/python' : 'python'} -m uvicorn scripts.ask_eval.server:app --host 127.0.0.1 --port 8129 --no-proxy-headers`,
    url: 'http://127.0.0.1:8129/ask',
    reuseExistingServer: false,
    env: { ASK_EVAL_LIVE: '0' },
    timeout: 30000,
  },
});
