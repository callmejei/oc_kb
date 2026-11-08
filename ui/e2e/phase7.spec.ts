import { expect, test } from "@playwright/test";
import { as } from "./helpers";

// Phase 7 in the browser: the operations dashboard (ops group only) and the ontology star map (BU-scoped overlay).

test("operations dashboard: ops analyst sees health, backlog, leak status and the quality trend", async ({ browser }) => {
  const ops = await as(browser, "ops.analyst");
  await ops.getByRole("link", { name: "Operations" }).click();
  await expect(ops.getByRole("heading", { name: "Operations" })).toBeVisible();
  await expect(ops.getByTestId("tile-leak")).toContainText("0 leaks"); // from the recorded `kb.ps1 eval` run
  await expect(ops.getByTestId("tile-eval")).toContainText(/PASS|FAIL/);
  await expect(ops.getByTestId("ops-sources").locator("tbody tr").first()).toBeVisible();
  await expect(ops.getByTestId("ops-runs").locator("tbody tr").first()).toBeVisible();
  await expect(ops.locator(".trend svg")).toBeVisible();
  // no content pages for an ops-only user: no readable BU, no governance role
  await expect(ops.getByRole("link", { name: "Checker inbox" })).toHaveCount(0);
  await expect(ops.getByRole("link", { name: "Sources" })).toHaveCount(0);
});

test("operations dashboard is refused to everyone else", async ({ browser }) => {
  const user = await as(browser, "ret.checker");
  await expect(user.getByRole("link", { name: "Operations" })).toHaveCount(0);
  await user.goto("/ops");
  await expect(user.locator(".error")).toContainText("GRP_KB_OPS");
});

test("ontology star map: FIBO plus the caller's own BU entities, click-through to sources", async ({ browser }) => {
  const page = await as(browser, "ret.user1");
  await page.getByRole("link", { name: "Ontology" }).click();
  await expect(page.getByTestId("starmap-stats")).toContainText("Your BUs (retail, shared)");
  await expect(page.getByTestId("starmap-canvas").locator("canvas")).toBeVisible();
  await page.getByTestId("starmap-search").fill("loan-to-value");
  const first = page.locator(".search-box .results button").first();
  await expect(first).toContainText("of your entities"); // concepts the user's BUs use rank first
  await first.click();
  const drawer = page.getByTestId("starmap-drawer");
  await expect(drawer.locator("h2")).toContainText(/loan-to-value/i);
  const own = drawer.locator("text=Your BU entities here").locator("xpath=following-sibling::div[1]//button");
  await expect(own).toBeVisible();
  await own.click();
  await expect(page.getByTestId("starmap-entity")).toContainText("Source documents");
});

test("ontology overlay for a corporate user never shows retail entities", async ({ browser }) => {
  const page = await as(browser, "corp.user1");
  const body = await page.evaluate(async () => {
    const r = await fetch("/api/v1/ontology/overlay", {
      headers: { Authorization: `Bearer ${sessionStorage.getItem("kb.token")}` },
    });
    return r.json();
  });
  expect(body.bus).toEqual(["corporate", "shared"]);
  expect(body.entities.length).toBeGreaterThan(0);
  expect(body.entities.every((e: { bu: string }) => e.bu === "corporate" || e.bu === "shared")).toBe(true);
});
