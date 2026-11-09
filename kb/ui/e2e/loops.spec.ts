import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { as, ask, askUntil, PASSWORDS, REPO, run } from "./helpers";

// Phase 6 acceptance, in a real browser against the live stack:
//  1. maker -> checker -> user -> flag -> inspector -> re-parse
//  2. upload -> private answer -> publish request -> checker approval -> BU-wide answer
// Each run uses facts unique to the run (synthetic/e2e.py) and cleans up what it created.

test.describe.configure({ mode: "serial" });

const created: { policyDocId?: string } = {};

// Cleanup even when a test fails, through the one door (the gateway), then the drive backstop.
test.afterAll(async ({ playwright }) => {
  const http = await playwright.request.newContext({
    baseURL: "http://127.0.0.1:8000",
  });
  const auth = async (user: string) => {
    const r = await http.post("/api/v1/auth/login", {
      data: { username: user, password: PASSWORDS[user] },
    });
    return { Authorization: `Bearer ${(await r.json()).access_token}` };
  };
  if (created.policyDocId) {
    await http.post(`/api/v1/documents/${created.policyDocId}/exclude`, {
      headers: await auth("ret.maker"),
      data: { reason: "E2E cleanup" },
    }); // 409 when the test already excluded it: fine
  }
  const owner = await auth("ret.user1");
  const mine: { doc_id: string; title: string; status: string }[] = await (
    await http.get("/api/v1/uploads/mine", { headers: owner })
  ).json();
  for (const f of mine.filter(
    (x) => x.title === run.upload_title && x.status === "ACTIVE",
  ))
    await http.delete(`/api/v1/uploads/${f.doc_id}`, { headers: owner });
  await http.dispose();
  execFileSync(
    "uv",
    ["run", "python", "-m", "synthetic.e2e", "cleanup", "--run", run.run],
    { cwd: REPO },
  );
});

test("maker -> checker -> user -> flag -> inspector -> re-parse", async ({
  browser,
}) => {
  // ---- maker registers a SharePoint source on the run's folder and submits it
  const maker = await as(browser, "ret.maker");
  await maker.click("nav >> text=Data Source Registration");
  const form = maker.getByTestId("register-form");
  await form.locator("input[name=source_id]").fill(run.source_id);
  await form.locator("input[name=display_name]").fill(`E2E ${run.run} library`);
  await form.locator("input[name=sp_folder_path]").fill(run.folder);
  await form.getByRole("button", { name: "Register and submit" }).click();
  await expect(maker.locator(".notice")).toContainText(
    `Submitted ${run.source_id}`,
  );
  // the dry-run sensor reports within a minute; the maker sees it on the request
  await expect(maker.getByTestId("dry-run")).toContainText("would_index: 1", {
    timeout: 180_000,
  });

  // ---- checker reviews: ACL diff + dry-run report, approves
  const checker = await as(browser, "ret.checker");
  await checker.click("nav >> text=Approval Workflows");
  const row = checker.locator("[data-testid^=req-]", {
    hasText: run.source_id,
  });
  await row.getByRole("button", { name: "Review" }).click();
  await expect(checker.getByTestId("acl-diff")).toContainText(
    "+ GRP_KB_RETAIL_READ",
  );
  await expect(checker.getByTestId("dry-run")).toContainText("would_index: 1");
  await checker.getByTestId("approve").click();
  await expect(checker.locator(".notice")).toContainText("approved");

  // the maker could not have approved it: makers have no inbox, and their own requests never show there
  await expect(maker.locator("nav")).not.toContainText("Checker inbox");

  // ---- user asks; the first-load sensor indexes the new source within minutes
  const user = await as(browser, "ret.user1");
  const answer = await askUntil(
    user,
    run.policy_question,
    run.policy_answer,
    8 * 60_000,
  );
  const href = await answer
    .getByTestId("citation-doc")
    .first()
    .locator("a")
    .getAttribute("href");
  created.policyDocId = href?.split("/documents/")[1]?.split("?")[0];
  await expect(answer.getByTestId("citation-doc").first()).toContainText(
    run.policy_title,
  );
  await expect(answer.getByTestId("citation-doc").first()).toContainText("v1");

  // another BU's user cannot open it, even with the exact link
  const corp = await as(browser, "corp.user1");
  await corp.goto(href!);
  await expect(corp.locator(".error")).toContainText("unknown document");

  // ---- user flags the answer
  await answer.getByTestId("thumbs-down").click();
  await answer.getByTestId("problem").selectOption("TABLE_BROKEN");
  await answer
    .getByTestId("reason")
    .fill(`E2E ${run.run}: rate table columns look shifted`);
  await answer.getByTestId("send-feedback").click();
  await expect(answer.getByTestId("feedback-sent")).toBeVisible();

  // ---- maker sees the flag, opens the inspector: pdf.js page + Docling regions + blocks + chunks
  await maker.click("nav >> text=Knowledge Processing & Indexing");
  const flag = maker.getByTestId("flag").filter({ hasText: `E2E ${run.run}` });
  await expect(flag).toBeVisible();
  await flag.getByTestId("flag-doc").click();
  await expect(maker.getByTestId("doc-title")).toContainText(run.policy_title);
  await expect(maker.getByTestId("pdf-page").locator("canvas")).toBeVisible();
  await expect(maker.getByTestId("region").first()).toBeVisible();
  expect(await maker.getByTestId("block").count()).toBeGreaterThan(3);
  await maker.getByTestId("region").first().click();
  await maker.getByTestId("label-problem").selectOption("TABLE_BROKEN");
  await maker.getByTestId("label-note").fill("columns shifted (E2E)");
  await maker.getByTestId("add-label").click();
  await expect(maker.getByTestId("labels")).toContainText("Table broken");

  // ---- re-parse with different options: the pipeline makes v2 current
  await maker.getByTestId("opt-ocr").selectOption("full_page");
  await maker.getByTestId("opt-cells").uncheck();
  await maker
    .getByTestId("action-reason")
    .fill("re-parse with full-page OCR (E2E)");
  await maker.getByTestId("reparse").click();
  await expect(maker.getByTestId("reparse-status").first()).toHaveText(
    "QUEUED",
  );
  await expect(maker.getByTestId("reparse-status").first()).toHaveText("DONE", {
    timeout: 6 * 60_000,
  });
  await expect(maker.getByTestId("version")).toContainText("v2 (current)");

  // ---- the user's answer now cites v2
  const again = await askUntil(
    user,
    run.policy_question,
    run.policy_answer,
    2 * 60_000,
  );
  await expect(again.getByTestId("citation-doc").first()).toContainText("v2");

  // ---- cleanup: exclude (single actor) -> out of search at once
  await maker.reload();
  await maker.getByTestId("action-reason").fill("E2E cleanup");
  await maker.getByTestId("exclude").click();
  await expect(maker.getByTestId("doc-status")).toHaveText("EXCLUDED");
  const gone = await ask(user, run.policy_question);
  await expect(gone).not.toContainText(run.policy_answer);
});

test("upload -> private answer -> publish request -> checker approval -> BU-wide answer", async ({
  browser,
}) => {
  const owner = await as(browser, "ret.user1");
  const colleague = await as(browser, "ret.user2"); // same BU, same groups

  // ---- upload to the retail space
  await owner.click("nav >> text=Knowledge Upload & Ingestion");
  await owner.getByTestId("space").selectOption("retail-uploads");
  await owner.getByTestId("file-input").setInputFiles(run.upload_path);
  await expect(owner.getByTestId("upload-result")).toContainText("RECEIVED");
  const fileRow = owner.getByTestId(`file-${run.upload_title}`);
  await expect(fileRow).toContainText("INDEXED", { timeout: 5 * 60_000 }); // gates + parse in the pipeline
  await expect(fileRow.getByTestId("visibility")).toHaveText("PRIVATE");

  // ---- private: the uploader gets the answer, a colleague in the same BU does not
  const mine = await askUntil(
    owner,
    run.upload_question,
    run.upload_answer,
    2 * 60_000,
  );
  await expect(mine.getByTestId("citation-doc").first()).toContainText(
    run.upload_title,
  );
  const theirs = await ask(colleague, run.upload_question);
  await expect(theirs).not.toContainText(run.upload_answer);

  // ---- publish request -> checker sees the ACL change and approves
  await owner.click("nav >> text=Knowledge Upload & Ingestion");
  await fileRow.getByTestId("publish").click();
  await expect(fileRow).toContainText("publish pending");
  const checker = await as(browser, "ret.checker");
  await checker.click("nav >> text=Approval Workflows");
  const req = checker.locator("[data-testid^=req-]", {
    hasText: run.upload_title,
  });
  await req.getByRole("button", { name: "Review" }).click();
  await expect(checker.getByTestId("acl-diff")).toContainText(
    "− user:ret.user1",
  );
  await expect(checker.getByTestId("acl-diff")).toContainText(
    "+ GRP_KB_RETAIL_READ",
  );
  await checker.getByTestId("approve").click();
  await expect(checker.locator(".notice")).toContainText("approved");

  // ---- BU-wide: the colleague now gets it (index-sync sensor re-stamps the ACL, no re-embedding)
  const now = await askUntil(
    colleague,
    run.upload_question,
    run.upload_answer,
    3 * 60_000,
  );
  await expect(now.getByTestId("citation-doc").first()).toContainText(
    run.upload_title,
  );
  // ...and other BUs still do not
  const corp = await as(browser, "corp.user1");
  await expect(await ask(corp, run.upload_question)).not.toContainText(
    run.upload_answer,
  );

  // ---- cleanup: the uploader deletes it (out of search immediately)
  await owner.click("nav >> text=Knowledge Upload & Ingestion");
  await owner
    .getByTestId(`file-${run.upload_title}`)
    .getByRole("button", { name: "Delete" })
    .click();
  await expect(owner.getByTestId(`file-${run.upload_title}`)).toHaveCount(0);
  await expect(await ask(colleague, run.upload_question)).not.toContainText(
    run.upload_answer,
  );
});

test("roles and empty access in the browser", async ({ browser }) => {
  const user = await as(browser, "ret.user1");
  await expect(user.locator("nav")).not.toContainText("Checker inbox");
  await expect(user.locator("nav")).not.toContainText("Sources");
  await user.goto("/inbox");
  await expect(user.locator(".error")).toContainText("not a CHECKER");

  const nobody = await as(browser, "nobody");
  await expect(
    nobody.locator("text=none (you have no document access)"),
  ).toBeVisible();
  const a = await ask(
    nobody,
    "What is the maximum loan to value for an investment property?",
  );
  await expect(a.getByTestId("citation-doc")).toHaveCount(0);
});
