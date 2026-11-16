import fs from 'node:fs';
import { defineConfig } from '@playwright/test';

const python = process.env.NBA_TEST_PYTHON || (fs.existsSync('.venv-app/bin/python') ? '.venv-app/bin/python' : 'python');
export default defineConfig({
  testDir: './tests_browser_metric_products',
  workers: 1,
  retries: 0,
  timeout: 30000,
  outputDir: 'reports/metric-product-browser',
  reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:8138', headless: true, trace: 'retain-on-failure' },
  webServer: {
    command: `${python} scripts/serve_synthetic_metric_products.py --port 8138 --unknown-last-game`,
    url: 'http://127.0.0.1:8138/ask',
    reuseExistingServer: false,
    env: { PYTHON_DOTENV_DISABLED: '1' },
    timeout: 30000,
  },
});
