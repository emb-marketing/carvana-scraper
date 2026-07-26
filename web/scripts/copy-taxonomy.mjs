/**
 * Copy the committed Carvana taxonomy into public/ before dev and build.
 *
 * Vercel builds with `web/` as the project root, so `../config` is outside the build context and
 * cannot be imported at build time. Copying keeps one source of truth: the taxonomy is generated
 * by tools/extract_taxonomy.py and committed at config/carvana-taxonomy.json, and this is only
 * ever a mirror of it.
 */
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = resolve(here, "..", "..", "config", "carvana-taxonomy.json");
const target = resolve(here, "..", "public", "carvana-taxonomy.json");

if (!existsSync(source)) {
  if (existsSync(target)) {
    console.warn(`[copy-taxonomy] ${source} missing — reusing the existing mirror.`);
    process.exit(0);
  }
  // Failing here beats shipping a build whose only symptom is an empty Make dropdown, which
  // looks like "Carvana has no inventory" rather than a missing file.
  console.error(
    `[copy-taxonomy] ${source} not found and no mirror exists.\n` +
      "Run tools/extract_taxonomy.py, or restore config/carvana-taxonomy.json.",
  );
  process.exit(1);
}

mkdirSync(dirname(target), { recursive: true });
copyFileSync(source, target);
console.log(`[copy-taxonomy] ${source} -> ${target}`);
