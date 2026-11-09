import { defineConfig } from "@playwright/test";

// Browser acceptance (Phase 6) against the live local stack: gateway (serving ui/dist), Dagster sensors,
// Ollama. `kb.ps1 up` first; the gateway is started here if it is not already running.
export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  timeout: 15 * 60 * 1000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "../docs/e2e-report" }]],
  use: {
    baseURL: "http://127.0.0.1:8000",
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "uv run uvicorn kb.gateway.app:create_app --factory --host 127.0.0.1 --port 8000",
    cwd: "..",
    url: "http://127.0.0.1:8000/healthz",
    reuseExistingServer: true,
    timeout: 180_000,
  },
});
