import { expect, test } from "@playwright/test";
import { as } from "./helpers";

// Phase 8 in the browser: the explorer shell. Every menu item is a real gateway-backed page or says "Not built";
// role-only items are hidden; every view is scoped to the caller.

test("explorer shell: nine groups, role items hidden, unbuilt screens say so", async ({
  browser,
}) => {
  const page = await as(browser, "ret.user1");
  // nine groups; "Data Management" holds only role-gated items (Phase 9b), so a plain user sees eight
  await expect(page.locator(".xgroup")).toHaveCount(8);
  await expect(page.getByTestId("group-data-management")).toHaveCount(0);
  await expect(page.getByTestId("group-agent-integration-tools")).toContainText(
    "Not built",
  );
  await expect(
    page.getByRole("link", { name: "Approval Workflows" }),
  ).toHaveCount(0); // not a checker
  await expect(
    page.getByRole("link", { name: "Pipeline & Agent Monitoring" }),
  ).toHaveCount(0); // not ops
  await page.getByRole("link", { name: /MCP Server & Tool Registry/ }).click();
  await expect(page.getByTestId("not-built")).toContainText("Not built");
  await expect(page.getByTestId("not-built")).toContainText("one door");
});

test("search: passages from own spaces only, click-through to the inspector", async ({
  browser,
}) => {
  const page = await as(browser, "ret.user1");
  await page.getByRole("link", { name: "Search", exact: true }).click();
  await page
    .getByTestId("search-input")
    .fill("loan-to-value limits for home loans");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const results = page.getByTestId("search-results");
  await expect(results.locator(".result").first()).toBeVisible();
  const bus = await results.locator(".result .chip").allTextContents();
  expect(bus.length).toBeGreaterThan(0);
  expect(bus.every((b) => b === "retail" || b === "shared")).toBe(true);
  await results.locator(".result a.title").first().click();
  await expect(page).toHaveURL(/\/documents\//);
});

test("catalog: data products show the caller's masks; mapping is read-only; graphs and ontology browse", async ({
  browser,
}) => {
  const page = await as(browser, "ret.user1");
  await page.getByRole("link", { name: "Data Products" }).click();
  await expect(page.getByTestId("mart-mart_credit.loan")).toBeVisible();
  await expect(page.getByTestId("mart-mart_wealth.portfolio")).toHaveCount(0);
  await expect(page.locator(".chip.warn").first()).toContainText("masked");
  await page
    .getByRole("link", { name: "Entity & Relationship Mapping" })
    .click();
  await expect(page.getByTestId("mapping-readonly")).toContainText("Read-only");
  await page.getByRole("link", { name: "Knowledge Graph Management" }).click();
  await expect(page.getByTestId("graphs").locator("tbody tr")).toHaveCount(2); // retail + shared
  await page.getByRole("link", { name: "Ontology Management" }).click();
  await page.getByTestId("onto-find").fill("loan-to-value ratio");
  await page.getByTestId("onto-tree").locator("button.node").first().click();
  await expect(page.getByTestId("onto-detail")).toContainText(
    "Your BU entities mapped here",
  );
});

test("space inventory and access page are the caller's own", async ({
  browser,
}) => {
  const page = await as(browser, "corp.user1");
  await page.getByRole("link", { name: "Space Asset Inventory" }).click();
  await expect(page.getByTestId("space-corporate")).toBeVisible();
  await expect(page.getByTestId("space-retail")).toHaveCount(0);
  await page
    .getByRole("link", { name: "Access Policies & Classification" })
    .click();
  await expect(page.getByTestId("my-groups")).toContainText(
    "GRP_KB_CORPORATE_READ",
  );
  await expect(page.getByTestId("my-groups")).not.toContainText("RETAIL");
});

test("ops: failing questions of the latest evaluation run", async ({
  browser,
}) => {
  const ops = await as(browser, "ops.analyst");
  await ops.getByRole("link", { name: "Retrieval / RAG Evaluation" }).click();
  await expect(ops.getByTestId("ops-failing")).toContainText(
    "Failing questions",
  );
  await expect(
    ops.getByTestId("ops-failing").locator("tbody tr").first(),
  ).toBeVisible();
});
