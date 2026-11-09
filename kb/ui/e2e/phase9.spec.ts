import { expect, test } from "@playwright/test";
import { as, ask } from "./helpers";

// Phase 9 in the browser: governed data layer.

test("data quality: counts for granted tables only; the stale-collateral rule fails; ops sees every table", async ({
  browser,
}) => {
  const page = await as(browser, "ret.user1");
  await page.getByRole("link", { name: "Data & Knowledge Quality" }).click();
  await expect(
    page.getByTestId("dq-table-mart_credit.collateral"),
  ).toContainText("collateral_revalued_24m");
  await expect(
    page.getByTestId("dq-table-mart_credit.collateral"),
  ).toContainText("fail");
  await expect(page.getByTestId("dq-table-mart_wealth.portfolio")).toHaveCount(
    0,
  );
  await expect(page.getByTestId("dq-data")).toContainText(
    "checked but not shown",
  );
  await expect(page.getByTestId("dq-bu-retail")).toBeVisible();
  await expect(page.getByTestId("dq-bu-wealth")).toHaveCount(0);

  const ops = await as(browser, "ops.analyst");
  await ops.goto("/quality");
  await expect(ops.getByTestId("dq-table-mart_wealth.portfolio")).toBeVisible();
  await expect(ops.getByTestId("dq-bu-wealth")).toBeVisible();
});

test("data product: checker masks a column at once; maker proposes unmasking; checker approves; the user sees it", async ({
  browser,
}) => {
  const valuation = (p: import("@playwright/test").Page) =>
    p
      .getByTestId("mart-mart_credit.collateral")
      .locator("tr", { hasText: "valuation" })
      .first();
  const answers = (p: import("@playwright/test").Page, list: string[]) => {
    p.removeAllListeners("dialog");
    p.on("dialog", (d) => d.accept(list.shift()));
  };

  // 1. single actor: the checker restricts, no approval needed, effective on the next query
  const checker = await as(browser, "data.checker");
  await expect(checker.getByTestId("group-data-management")).toBeVisible(); // the domain's governors see it
  await expect(
    checker.getByRole("link", { name: "Data Source Registration" }),
  ).toHaveCount(0); // not a source maker
  await checker
    .getByRole("link", { name: "Data Product Registration" })
    .click();
  const v0 = Number(await checker.getByTestId("policy-version").textContent());
  answers(checker, [
    "ADD_MASK",
    "valuation",
    "hash",
    "e2e: valuation under review",
  ]);
  await checker
    .getByTestId("pp-product-mart_credit.collateral")
    .getByTestId("pp-restrict")
    .click();
  await expect(checker.getByTestId("pp-msg")).toContainText("restricted");
  await expect(checker.getByTestId("policy-version")).toHaveText(
    String(v0 + 1),
  );

  const user = await as(browser, "ret.user1");
  await user.getByRole("link", { name: "Data Products" }).click();
  await expect(valuation(user)).toContainText("masked (hash)");

  // 2. widening needs four eyes: the maker drafts the unmasking, submits; the checker sees "widens" and approves
  const maker = await as(browser, "data.maker");
  await maker.getByRole("link", { name: "Data Product Registration" }).click();
  await maker
    .getByTestId("pp-product-mart_credit.collateral")
    .getByTestId("pp-change")
    .click();
  await maker.getByTestId("pe-masks").fill("");
  await maker.getByTestId("pe-save").click();
  await maker
    .getByTestId("pp-draft-mart_credit.collateral")
    .getByTestId("pp-submit")
    .click();
  await expect(maker.getByTestId("pp-msg")).toContainText("Submitted");
  await expect(maker.getByTestId("pp-requests")).toHaveCount(0); // a maker has no checker queue

  await checker.reload();
  const req = checker.getByTestId("pp-req-mart_credit.collateral");
  await expect(req).toContainText("widens access");
  await expect(req).toContainText("mask removed");
  await req.getByTestId("pp-approve").click();
  await expect(checker.getByTestId("pp-msg")).toContainText("Approved");

  await user.reload();
  await expect(valuation(user)).toContainText("visible");
});

test("semantic metrics: catalogue scoped to grants; chat answers from the governed metric; maker -> checker publishes a new one", async ({
  browser,
}) => {
  test.setTimeout(300_000);
  const user = await as(browser, "ret.user1");
  await user.getByRole("link", { name: "Semantic Products" }).click();
  await expect(user.getByTestId("metric-npl_ratio")).toContainText("NPL ratio");
  await expect(user.getByTestId("metric-aum")).toHaveCount(0); // wealth table: not granted, not shown
  await expect(user.getByText("other metric exists")).toBeVisible();

  const answer = await ask(user, "What is our NPL ratio?");
  await expect(answer).toContainText("6.58");
  await expect(answer.getByTestId("citation-metric")).toContainText(
    "NPL ratio",
  );

  // a new metric: drafted by the data maker, approved by the data checker, then visible to the user
  const maker = await as(browser, "data.maker");
  await maker.goto("/semantic/model");
  await maker.getByTestId("mx-save").click();
  await expect(maker.getByTestId("mx-msg")).toContainText("Draft saved");
  await maker
    .getByTestId("mx-draft-avg_interest_rate")
    .getByTestId("mx-submit")
    .click();
  await expect(maker.getByTestId("mx-msg")).toContainText("Submitted");

  const checker = await as(browser, "data.checker");
  await checker.getByRole("link", { name: "Semantic Model & Metrics" }).click();
  await checker
    .getByTestId("mx-req-avg_interest_rate")
    .getByTestId("mx-approve")
    .click();
  await expect(checker.getByTestId("mx-msg")).toContainText("published");

  await user.goto("/semantic");
  await expect(user.getByTestId("metric-avg_interest_rate")).toBeVisible();

  // retire (single actor) so the scenario can run again
  checker.removeAllListeners("dialog");
  checker.on("dialog", (d) => d.accept("e2e cleanup"));
  await checker
    .getByTestId("mx-list")
    .locator("tr", { hasText: "avg_interest_rate" })
    .getByRole("button", { name: "Retire" })
    .click();
  await expect(checker.getByTestId("mx-msg")).toContainText("Retired");
});
