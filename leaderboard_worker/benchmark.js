// Local CPU/latency estimates only. Node process CPU includes native crypto and
// Response plumbing; it is not Cloudflare's metered isolate CPU measurement.
import { createHash, createPrivateKey, createPublicKey, sign } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";
import { ADJECTIVES, COLORS, ANIMALS } from "./words.js";
import { API_ROOT, BRANCH, CONTENTS_URL, MAX_FILE_BYTES, canonicalJson, createWorker } from "./worker.js";

const root = dirname(fileURLToPath(import.meta.url));
const fixtures = JSON.parse(readFileSync(resolve(root, "test/python-fixtures.json")));
const current = fixtures.records.current;
const now = Date.parse(fixtures.now);
const words = [ADJECTIVES, COLORS, ANIMALS];
const sha256 = (bytes) => createHash("sha256").update(bytes).digest();
const blobSha = (bytes) => createHash("sha1").update(`blob ${bytes.length}\0`).update(bytes).digest("hex");
const privatePrefix = Buffer.from("302e020100300506032b657004220420", "hex");

const calibrationStart = performance.now(), calibrationCpu = process.cpuUsage();
let calibrationValue = 0;
while (performance.now() - calibrationStart < 250) calibrationValue += Math.sqrt(calibrationValue + 1);
const calibrationElapsed = process.cpuUsage(calibrationCpu);
const calibration = { busy_wall_ms: performance.now() - calibrationStart,
  reported_cpu_ms: (calibrationElapsed.user + calibrationElapsed.system) / 1000,
  measured_value_is_finite: Number.isFinite(calibrationValue) };
console.log(JSON.stringify({ cpu_counter_calibration: calibration }));

function representativeRow(index) {
  const key = createPrivateKey({ key: Buffer.concat([privatePrefix, sha256(Buffer.from("benchmark-key-" + index))]),
    type: "pkcs8", format: "der" });
  const pub = createPublicKey(key).export({ type: "spki", format: "der" }).subarray(-32);
  const digest = sha256(pub);
  const alias = words.map((list, offset) => list[Number(digest.readBigUInt64BE(offset * 8) % BigInt(list.length))]).join(" ");
  const row = { public_key: pub.toString("hex"), submission_id: sha256(Buffer.concat([pub, Buffer.from(":2026-10-21")])).toString("hex").slice(0, 32),
    alias, captured_at: "2026-10-21T14:00:00Z", max_health: 733, max_mana: 570,
    personal_kills: index, personal_deaths: 2, high_hit: 300, coin_looted: index * 100, sequence: 1, capture_backend: "window" };
  row.signature = sign(null, Buffer.from(canonicalJson(row)), key).toString("hex");
  return JSON.stringify(row) + "\n";
}

const lines = [];
let bytes = 0;
for (let index = 0; ; index++) {
  const line = representativeRow(index);
  if (bytes + Buffer.byteLength(line) + Buffer.byteLength(JSON.stringify(current)) + 1 > MAX_FILE_BYTES) break;
  lines.push(line); bytes += Buffer.byteLength(line);
}
const counts = [0, 10, 100, 1000, 5000, lines.length];
const results = [];
for (const count of counts) {
  const raw = Buffer.from(lines.slice(0, count).join(""));
  const sha = blobSha(raw);
  const nextSha = blobSha(Buffer.concat([raw, Buffer.from(JSON.stringify(current) + "\n")]));
  const metadata = JSON.stringify({ type: "file", sha, size: raw.length,
    encoding: raw.length > 1_048_576 ? "none" : "base64", content: raw.length > 1_048_576 ? "" : raw.toString("base64") });
  const blob = JSON.stringify({ encoding: "base64", content: raw.toString("base64"), sha });
  const saved = JSON.stringify({ content: { sha: nextSha } });
  const fakeFetch = async (url, options) => {
    if (options.method === "GET" && url === `${CONTENTS_URL}?ref=${BRANCH}`) return new Response(metadata);
    if (options.method === "GET" && url === `${API_ROOT}/git/blobs/${sha}`) return new Response(blob);
    if (options.method === "PUT" && url === CONTENTS_URL) return new Response(saved);
    throw new Error("Unexpected fake request");
  };
  const worker = createWorker({ fetchImpl: fakeFetch, now: () => now });
  const cpu = [], wall = [];
  // Windows process CPU counters can advance in ~15ms increments. A batch
  // average is meaningful for small requests where one call reads as zero.
  const repetitions = count < 100 ? 64 : count < 1000 ? 32 : count < 5000 ? 16 : 8;
  const batchStarted = process.cpuUsage();
  for (let repetition = 0; repetition < repetitions; repetition++) {
    const request = new Request("https://benchmark.invalid/snapshots", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(current) });
    const started = performance.now(), before = process.cpuUsage();
    const response = await worker.fetch(request, { GITHUB_TOKEN: "fake-benchmark-token" });
    const elapsed = process.cpuUsage(before);
    const elapsedWall = performance.now() - started;
    if (response.status !== 201) throw new Error(`Benchmark request failed: ${response.status} ${await response.text()}`);
    // First repetition includes the per-case cold path; report it separately.
    cpu.push((elapsed.user + elapsed.system) / 1000); wall.push(elapsedWall);
  }
  const batchCpu = process.cpuUsage(batchStarted);
  const middle = (samples) => [...samples].sort((a, b) => a - b)[Math.floor(samples.length / 2)];
  const result = { records: count, file_bytes: raw.length, requests_measured: repetitions,
    batch_cpu_ms: (batchCpu.user + batchCpu.system) / 1000,
    cpu_per_request_ms: (batchCpu.user + batchCpu.system) / 1000 / repetitions,
    max_single_cpu_ms: Math.max(...cpu), wall_median_ms: middle(wall) };
  results.push(result);
  console.log(JSON.stringify(result));
}
mkdirSync(resolve(root, "dist"), { recursive: true });
writeFileSync(resolve(root, "dist/benchmark-results.json"), JSON.stringify({ node: process.version, calibration,
  runtime: "Local Node built-in WebCrypto; not a Cloudflare metered-CPU measurement", results }, null, 2) + "\n");
