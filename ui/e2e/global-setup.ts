import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

// One run id per test run: every fact the tests look for is unique to this run.
export default function globalSetup(): void {
  const run = `r${Date.now().toString(36)}`;
  const out = execFileSync("uv", ["run", "python", "-m", "synthetic.e2e", "prepare", "--run", run], {
    cwd: join(HERE, "..", ".."),
    encoding: "utf-8",
  });
  const line = out.trim().split(/\r?\n/).pop()!;
  JSON.parse(line); // fail early on anything unexpected
  writeFileSync(join(HERE, ".run.json"), line, "utf-8");
}
