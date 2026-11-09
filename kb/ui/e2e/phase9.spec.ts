import { expect, test } from "@playwright/test";
import { as } from "./helpers";

// Phase 9 in the browser: governed data layer.

test("data quality: counts for granted tables only; the stale-collateral rule fails; ops sees every table", async ({ browser }) => {
  const page = await as(browser, "ret.user1");
  await page.getByRole("link", { name: "Data & Knowledge Quality" }).click();
  await expect(page.getByTestId("dq-table-mart_credit.collateral")).toContainText("collateral_revalued_24m");
  await expect(page.getByTestId("dq-table-mart_credit.collateral")).toContainText("fail");
  await expect(page.getByTestId("dq-table-mart_wealth.portfolio")).toHaveCount(0);
  await expect(page.getByTestId("dq-data")).toContainText("checked but not shown");
  await expect(page.getByTestId("dq-bu-retail")).toBeVisible();
  await expect(page.getByTestId("dq-bu-wealth")).toHaveCount(0);

  const ops = await as(browser, "ops.analyst");
  await ops.goto("/quality");
  await expect(ops.getByTestId("dq-table-mart_wealth.portfolio")).toBeVisible();
  await expect(ops.getByTestId("dq-bu-wealth")).toBeVisible();
});
