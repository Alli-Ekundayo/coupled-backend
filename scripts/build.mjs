import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");

async function main() {
  // 1. Validate neon.ts loads cleanly against @neon/config/v1
  const neonTs = path.join(rootDir, "neon.ts");
  if (existsSync(neonTs)) {
    const mod = await import(pathToFileURL(neonTs).href);
    const cfg = mod.default;
    if (!cfg || typeof cfg !== "object") {
      throw new Error("neon.ts did not export a valid config object");
    }
    console.log("[build] Verified neon.ts (auth:", Boolean(cfg.auth), ")");
  }

  // 2. Verify SQL migrations are present and non-empty
  const migDir = path.join(rootDir, "coupled_backend", "migrations");
  const migs = existsSync(migDir)
    ? readdirSync(migDir).filter((f) => f.endsWith(".sql")).sort()
    : [];
  if (migs.length === 0) {
    throw new Error("No SQL migrations found in coupled_backend/migrations");
  }
  for (const m of migs) {
    const sql = readFileSync(path.join(migDir, m), "utf8").trim();
    if (!sql) throw new Error(`Migration ${m} is empty`);
  }
  console.log(`[build] Verified ${migs.length} SQL migration(s): ${migs.join(", ")}`);

  // 3. Compile and import-check the Python FastAPI backend
  const venvPy = path.join(rootDir, ".venv", "bin", "python");
  const pyBin = existsSync(venvPy) ? venvPy : "python3";
  execFileSync(pyBin, ["-m", "compileall", "-q", path.join(rootDir, "coupled_backend")], {
    stdio: "inherit",
  });
  execFileSync(
    pyBin,
    ["-c", "import coupled_backend.api, coupled_backend.auth, coupled_backend.db, coupled_backend.parts, coupled_backend.store"],
    { cwd: rootDir, stdio: "inherit" }
  );
  console.log(`[build] Verified Python backend (${path.basename(pyBin)} compileall + imports)`);
}

main().catch((err) => {
  console.error("[build] Failed:", err);
  process.exit(1);
});
