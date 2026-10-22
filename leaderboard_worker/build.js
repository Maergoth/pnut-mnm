// Build a single Worker ESM module without a package manager or bundler.
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const words = readFileSync(resolve(root, "words.js"), "utf8");
const source = readFileSync(resolve(root, "worker.js"), "utf8");
const entry = 'import { ADJECTIVES, COLORS, ANIMALS } from "./words.js";';
if (!source.startsWith(entry)) throw new Error("Unexpected Worker module imports");
const output = resolve(process.argv[2] || resolve(root, "dist/index.js"));
mkdirSync(dirname(output), { recursive: true });
writeFileSync(output, words + "\n" + source.slice(entry.length), "utf8");
console.log("Built single-module Worker: " + output);
