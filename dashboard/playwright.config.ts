import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:8790", trace: "retain-on-failure" },
  projects: [
    {
      name: "desktop",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 1000 },
      },
    },
    {
      name: "mobile",
      use: { ...devices["iPhone 13"], browserName: "chromium" },
    },
  ],
  webServer: {
    command: `${process.env.DASHBOARD_TEST_PYTHON || ".venv/bin/python"} tests/dashboard_preview.py`,
    cwd: fileURLToPath(new URL("..", import.meta.url)),
    env: { PYTHONPATH: "src", PYTHONDONTWRITEBYTECODE: "1" },
    url: "http://127.0.0.1:8790/api/health",
    reuseExistingServer: false,
  },
});
