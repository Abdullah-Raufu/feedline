import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const onVercel = process.env.VERCEL === "1" || process.env.VERCEL_BUILD_RUNNING === "1";

if (!onVercel) {
  console.log("prepare-vercel: skipped (local build)");
  process.exit(0);
}

const secretPath = resolve(root, "server", "deployment_secret.txt");

if (!existsSync(secretPath)) {
  mkdirSync(dirname(secretPath), { recursive: true });
  writeFileSync(secretPath, `${randomBytes(32).toString("hex")}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  console.log("prepare-vercel: created server/deployment_secret.txt");
} else {
  console.log("prepare-vercel: using existing server/deployment_secret.txt");
}
