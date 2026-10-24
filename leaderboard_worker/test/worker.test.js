import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";
import { createHash, createPrivateKey, createPublicKey, sign, webcrypto } from "node:crypto";
import { ADJECTIVES, COLORS, ANIMALS } from "../words.js";
import { API_ROOT, BRANCH, CONTENTS_URL, MAX_FILE_BYTES, MAX_REQUEST_BYTES,
  canonicalJson, createWorker, normalizeMessage, parseSnapshotJson, validateSnapshot } from "../worker.js";
import { FakeGitHub, history } from "./fake-github.js";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const fixture = JSON.parse(readFileSync(resolve(root, "test/python-fixtures.json")));
const R = fixture.records;
const NOW = Date.parse(fixture.now);
const environment = { GITHUB_TOKEN: "fake-server-only-secret" };
const request = (record) => new Request("https://service.invalid/snapshots", { method: "POST",
  headers: { "Content-Type": "application/json" }, body: typeof record === "string" ? record : JSON.stringify(record) });

function setup(records = [], now = NOW) {
  const github = new FakeGitHub(records);
  const worker = createWorker({ fetchImpl: github.fetch, now: () => now, subtle: webcrypto.subtle });
  return { github, worker, post: (record = R.current) => worker.fetch(request(record), environment) };
}

function signedRecord(seed, day, sequence, alias, changes = {}) {
  const privateKey = createPrivateKey({ format: "der", type: "pkcs8",
    key: Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), Buffer.from(seed, "hex")]) });
  const publicKey = createPublicKey(privateKey).export({ format: "der", type: "spki" }).subarray(-32);
  const captured_at = `2026-10-${day}T14:00:00Z`;
  const submission_id = createHash("sha256").update(publicKey).update(":" + captured_at.slice(0, 10))
    .digest("hex").slice(0, 32);
  const record = { ...R.current, ...changes, public_key: publicKey.toString("hex"),
    captured_at, submission_id, sequence, alias };
  record.signature = sign(null, Buffer.from(canonicalJson(record)), privateKey).toString("hex");
  return record;
}

test("curated words and canonical signed bytes exactly match Python fixtures", async () => {
  assert.deepEqual({ ADJECTIVES, COLORS, ANIMALS }, fixture.words);
  for (const [name, record] of Object.entries(R)) {
    assert.equal(canonicalJson(record), fixture.canonical[name], name);
    if (!name.startsWith("signed_")) assert.deepEqual(await validateSnapshot(record), record, name);
  }
});

test("new publication matches adapter contract and fixed GitHub path", async () => {
  const { github, post } = setup([R.other]);
  const response = await post();
  assert.equal(response.status, 201);
  assert.deepEqual(await response.json(), { snapshot: R.current, created: true });
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.deepEqual(github.bytes.toString().trim().split("\n").map(JSON.parse), [R.other, R.current]);
  for (const { url, options } of github.calls) {
    assert.ok(url.startsWith(API_ROOT));
    assert.equal(options.redirect, "manual");
    assert.equal(options.headers.Authorization, "Bearer " + environment.GITHUB_TOKEN);
  }
  const write = github.calls.at(-1);
  assert.equal(write.url, CONTENTS_URL);
  assert.equal(JSON.parse(write.options.body).branch, BRANCH);
  assert.ok(!github.bytes.includes(environment.GITHUB_TOKEN));
});

test("same UTC-day ID returns original signed row without replacing newer values", async () => {
  const { github, post } = setup([R.current]);
  const response = await post(R.same_day_newer);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { snapshot: R.current, created: false });
  assert.equal(github.puts, 0);
});

test("new day accepts increasing sequence and unchanged cumulative fields", async () => {
  const { github, post } = setup([R.older, R.other, R.previous]);
  assert.equal((await post()).status, 201);
  assert.deepEqual(github.bytes.toString().trim().split("\n").map(JSON.parse), [R.other, R.current]);
});

test("identity upsert preserves separate signed keys with the same anonymous alias", async () => {
  const firstSeed = "8bcbb4c131df56f7c79066016241cc4bdf4e58db55c4f674e88b22365bd2e2ad";
  const secondSeed = "0bba869d7f392cbcaca6b8935ddc7fc3a8c50846d884959333fb7da475957511";
  const first = signedRecord(firstSeed, 21, 1, "Noble Beige Mantis");
  const second = signedRecord(secondSeed, 21, 1, "Noble Beige Mantis");
  const updated = signedRecord(firstSeed, 22, 2, "Noble Beige Mantis");
  assert.notEqual(first.public_key, second.public_key);
  assert.deepEqual(await validateSnapshot(first), first);
  assert.deepEqual(await validateSnapshot(second), second);
  const { github, post } = setup([first, second]);
  assert.equal((await post(updated)).status, 201);
  assert.deepEqual(github.bytes.toString().trim().split("\n").map(JSON.parse), [second, updated]);
});

test("all historical rollback floors are checked before daily rows are compacted", async () => {
  const seed = "01".repeat(32);
  const old = signedRecord(seed, 20, 8, R.current.alias, { personal_kills: 100 });
  const previous = signedRecord(seed, 21, 9, R.current.alias, { personal_kills: 10 });
  const { github, post } = setup([old, previous]);
  assert.equal((await post()).status, 400);
  assert.equal(github.puts, 0);
  assert.deepEqual(github.bytes.toString().trim().split("\n").map(JSON.parse), [old, previous]);
});

test("unrelated publication cannot erase another identity's historical counter floor", async () => {
  const seed = "01".repeat(32);
  const old = signedRecord(seed, 20, 8, R.current.alias, { personal_kills: 100 });
  const previous = signedRecord(seed, 21, 9, R.current.alias, { personal_kills: 10 });
  const { github, post } = setup([old, previous]);
  assert.equal((await post(R.other)).status, 201);
  assert.deepEqual(github.bytes.toString().trim().split("\n").map(JSON.parse), [old, previous, R.other]);
  assert.equal((await post()).status, 400);
  assert.equal(github.puts, 1);
  assert.deepEqual(github.bytes.toString().trim().split("\n").map(JSON.parse), [old, previous, R.other]);
});

test("updating an identity at record capacity does not require another slot", async () => {
  // Lower only the record bound in an isolated module instance; signature,
  // storage and publication code remain the real implementation.
  const source = readFileSync(resolve(root, "worker.js"), "utf8");
  const words = readFileSync(resolve(root, "words.js"), "utf8");
  const entry = 'import { ADJECTIVES, COLORS, ANIMALS } from "./words.js";';
  const limit = "export const MAX_RECORDS = 30_000;";
  assert.ok(source.startsWith(entry) && source.includes(limit));
  const moduleSource = words + "\n" + source.slice(entry.length).replace(limit, "export const MAX_RECORDS = 2;");
  const bounded = await import("data:text/javascript;base64," + Buffer.from(moduleSource).toString("base64"));
  const github = new FakeGitHub([R.previous, R.other]);
  const worker = bounded.createWorker({ fetchImpl: github.fetch, now: () => NOW, subtle: webcrypto.subtle });
  assert.equal((await worker.fetch(request(R.current), environment)).status, 201);
  assert.deepEqual(github.bytes.toString().trim().split("\n").map(JSON.parse), [R.other, R.current]);
});

test("v2 readings and legacy history coexist without changing old signed rows or daily caps", async () => {
  const legacy = setup([R.current]);
  const duplicate = await legacy.post(R.v2_current);
  assert.equal(duplicate.status, 200);
  assert.deepEqual((await duplicate.json()).snapshot, R.current);
  assert.equal(legacy.github.puts, 0);
  const upgraded = setup([R.previous, R.other]);
  assert.equal((await upgraded.post(R.v2_current)).status, 201);
  assert.deepEqual(upgraded.github.bytes.toString().trim().split("\n").map(JSON.parse),
    [R.other, R.v2_current]);
  const oldClient = setup([R.v2_previous]);
  assert.equal((await oldClient.post(R.current)).status, 201);
});

test("v2 signed totals require one complete group and accept explicit zero or negative values", async () => {
  for (const record of [R.v2_current, R.v2_partial]) {
    assert.deepEqual(await validateSnapshot(record), record);
  }
  assert.equal(R.v2_current.strength, 0);
  assert.equal(R.v2_current.resist_cold, -3);
  assert.equal(R.v2_partial.stat_total, null);
  assert.equal(R.v2_partial.resist_total, null);
  for (const changed of [{ strength: true }, { wisdom: null }, { stat_total: 0 },
    { schema_version: 1 }, { resist_cold: -1_000_000_001 }, { armor_class: 0.5 }]) {
    const { github, post } = setup();
    assert.equal((await post({ ...R.v2_current, ...changed })).status, 400);
    assert.equal(github.puts, 0);
  }
  const { post } = setup();
  assert.equal((await post(R.signed_bad_v2_total)).status, 400);
});

test("v3 personal bests keep observed totals rather than summing independent maxima", async () => {
  assert.equal(R.v3_bests.schema_version, 3);
  assert.equal(R.v3_bests.strength + R.v3_bests.stamina, 200);
  assert.equal(R.v3_bests.stat_total, 106);
  assert.equal(R.v3_bests.resist_total, 107);
  assert.equal(R.v3_bests.max_health, 900);
  assert.equal(R.v3_bests.max_mana, 700);
  for (const record of [R.v3_bests, R.v3_no_complete]) {
    assert.deepEqual(await validateSnapshot(record), record);
    const { post } = setup([R.v2_previous]);
    assert.equal((await post(record)).status, 201);
  }
  assert.equal(R.v3_no_complete.stat_total, null);
  assert.equal(R.v3_no_complete.resist_total, null);
  for (const record of [R.signed_bad_v3_total, R.signed_bad_v3_partial, R.signed_bad_v3_range]) {
    const { github, post } = setup();
    assert.equal((await post(record)).status, 400);
    assert.equal(github.puts, 0);
  }
  for (const original of [R.current, R.v2_current, R.v3_bests]) {
    const { post, github } = setup([original]);
    const repeat = await post(R.v3_bests);
    assert.equal(repeat.status, 200);
    assert.deepEqual((await repeat.json()).snapshot, original);
    assert.equal(github.puts, 0);
  }
});

test("optional public messages match Python normalization and preserve signed Unicode and first daily message", async () => {
  for (const [input, expected] of fixture.messages) assert.equal(normalizeMessage(input), expected);
  assert.throws(() => normalizeMessage("🐉".repeat(161)));
  assert.equal(R.v2_message.message, '<b>****!</b> Café 🐉 & "great"');
  const { post, github } = setup();
  assert.equal((await post(R.v2_message)).status, 201);
  const repeat = await post(R.v2_message_retry);
  assert.equal(repeat.status, 200);
  assert.deepEqual((await repeat.json()).snapshot, R.v2_message);
  assert.equal(github.puts, 1);
  for (const record of [R.signed_bad_message, R.signed_noncanonical_message]) {
    const invalid = setup();
    assert.equal((await invalid.post(record)).status, 400);
    assert.equal(invalid.github.puts, 0);
  }
});

test("new day rejects sequence and all four cumulative rollbacks before writing", async () => {
  for (const name of ["equal_sequence", "lower_sequence", ...Object.keys(R).filter((name) => name.startsWith("decreased_"))]) {
    const { github, post } = setup([R.previous]);
    assert.equal((await post(R[name])).status, 400, name);
    assert.equal(github.puts, 0);
  }
});

test("same-key history signatures are checked before idempotent/rollback decisions", async () => {
  const changed = { ...R.previous, personal_kills: R.previous.personal_kills + 1 };
  const { github, post } = setup([changed]);
  assert.equal((await post()).status, 503);
  assert.equal(github.puts, 0);
});

test("optimistic conflict retry preserves concurrent rows and rechecks rollback", async () => {
  const { github, post } = setup([R.older]);
  github.conflicts.push((fake) => { fake.bytes = history([R.older, R.other]); });
  assert.equal((await post()).status, 201);
  assert.equal(github.puts, 2);
  assert.deepEqual(github.bytes.toString().trim().split("\n").map(JSON.parse), [R.other, R.current]);
  const second = setup([R.older]);
  second.github.conflicts.push((fake) => { fake.bytes = history([R.older, R.previous]); });
  assert.equal((await second.post(R.equal_sequence)).status, 400);
  assert.equal(second.github.puts, 1);
});

test("concurrent identical ID is idempotent and conflict attempts are bounded", async () => {
  const first = setup();
  first.github.conflicts.push((fake) => { fake.bytes = history([R.current]); });
  const response = await first.post();
  assert.equal(response.status, 200);
  assert.equal((await response.json()).created, false);
  assert.equal(first.github.puts, 1);
  const second = setup();
  second.github.conflicts = [null, null, null, null];
  assert.equal((await second.post()).status, 503);
  assert.equal(second.github.puts, 3);
});

test("missing fixed file requires existing data branch; no branch provisioning", async () => {
  const first = setup(); first.github.bytes = null;
  assert.equal((await first.post()).status, 201);
  assert.equal(first.github.calls[1].url, `${API_ROOT}/branches/${BRANCH}`);
  assert.ok(!Object.hasOwn(JSON.parse(first.github.calls.at(-1).options.body), "sha"));
  const second = setup(); second.github.bytes = null; second.github.branchExists = false;
  assert.equal((await second.post()).status, 503);
  assert.equal(second.github.puts, 0);
});

test("large-file representation uses immutable blob SHA without following download URLs", async () => {
  const { github, post } = setup([R.other]); github.encodingNone = true;
  assert.equal((await post()).status, 201);
  assert.ok(github.calls[1].url.startsWith(`${API_ROOT}/git/blobs/`));
  assert.ok(github.calls.every(({ url }) => url.startsWith(API_ROOT)));
});

test("invalid stored shape/date/checksum or oversized metadata never gets overwritten", async () => {
  for (const values of [{ captured_at: "2026-02-30T14:00:00Z" }, { max_health: true }, { player_name: "Do not publish" }]) {
    const { github, post } = setup([{ ...R.other, ...values }]);
    assert.equal((await post()).status, 503);
    assert.equal(github.puts, 0);
  }
  for (const metadata of [{ sha: "f".repeat(40) }, { size: MAX_FILE_BYTES + 1 }]) {
    const { github, post } = setup(); github.metadata = metadata;
    assert.equal((await post()).status, 503);
    assert.equal(github.puts, 0);
  }
});

test("bad/mutated signature, signed nonwindow/alias/ID and extra/private fields are rejected", async () => {
  const bad = [{ ...R.current, personal_kills: 900 }, { ...R.current, signature: "0".repeat(128) },
    { ...R.current, max_health: true }, { ...R.current, player_name: "Do not publish" },
    { ...R.current, token: "client-secret" }, ...Object.keys(R).filter((name) => name.startsWith("signed_")).map((name) => R[name])];
  for (const value of bad) {
    const { github, post } = setup();
    assert.equal((await post(value)).status, 400);
    assert.equal(github.calls.length, 0);
  }
});

test("integer lexical validation and duplicate JSON keys conform to Python", async () => {
  for (const suffix of [',"max_health":733}', ',"max_health":733.0}']) {
    const { github, post } = setup();
    assert.equal((await post(JSON.stringify(R.current).slice(0, -1) + suffix)).status, 400);
    assert.equal(github.calls.length, 0);
  }
  for (const replacement of ["733.0", "7.33e2", "9007199254740992"]) {
    const { github, post } = setup();
    assert.equal((await post(JSON.stringify(R.current).replace('"max_health":733', '"max_health":' + replacement))).status, 400);
    assert.equal(github.calls.length, 0);
  }
  assert.deepEqual(parseSnapshotJson(JSON.stringify(R.current)), R.current);
});

test("integral-float/exponent history is rejected like the Python strict parser", async () => {
  for (const record of [R.current, R.other]) {
    for (const replacement of ["733.0", "7.33e2"]) {
      const { github, post } = setup();
      github.bytes = Buffer.from(JSON.stringify(record).replace('"max_health":733', '"max_health":' + replacement) + "\n");
      assert.equal((await post()).status, 503);
      assert.equal(github.puts, 0);
    }
  }
});

test("UTC day and five-minute freshness checks precede GitHub access", async () => {
  for (const now of [NOW - 300_001, NOW + 300_001, Date.parse("2026-10-23T00:01:00Z")]) {
    const { github, post } = setup([], now);
    assert.equal((await post()).status, 400);
    assert.equal(github.calls.length, 0);
  }
  for (const now of [NOW - 300_000, NOW + 300_000]) {
    assert.equal((await setup([], now).post()).status, 201);
  }
});

test("health, endpoint/media/body limits and missing token return JSON without writes", async () => {
  const { worker, github } = setup();
  assert.deepEqual(await (await worker.fetch(new Request("https://service.invalid/health"))).json(), { status: "ok" });
  assert.equal((await worker.fetch(new Request("https://service.invalid/other"))).status, 404);
  assert.equal((await worker.fetch(new Request("https://service.invalid/snapshots?path=elsewhere", { method: "POST" }))).status, 404);
  assert.equal((await worker.fetch(new Request("https://service.invalid/snapshots", { method: "POST", body: "{}" }))).status, 415);
  assert.equal((await worker.fetch(request("x".repeat(MAX_REQUEST_BYTES + 1)), environment)).status, 413);
  const response = await worker.fetch(request(R.current), {});
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: "Leaderboard storage is temporarily unavailable" });
  assert.equal(github.calls.length, 0);
});

test("ambiguous successful write is not acknowledged until matching public record is read", async () => {
  const { github, post } = setup(); github.badConfirmation = true;
  assert.equal((await post()).status, 503);
  assert.equal((await post()).status, 200);
  assert.equal(github.puts, 1);
});

test("upstream failure is generic and does not leak token or private response", async () => {
  const { github, post } = setup(); github.error = 403;
  const response = await post();
  assert.equal(response.status, 503);
  const body = await response.text();
  assert.ok(!body.includes(environment.GITHUB_TOKEN));
  assert.ok(!body.includes("private detail"));
});

test("upstream redirects are not followed and never forward credentials to their target", async () => {
  const calls = [];
  const worker = createWorker({ now: () => NOW, fetchImpl: async (url, options) => {
    calls.push({ url, options });
    assert.equal(options.redirect, "manual");
    return new Response(null, { status: 302, headers: { Location: "https://untrusted.invalid/redirect-target" } });
  } });
  const response = await worker.fetch(request(R.current), environment);
  assert.equal(response.status, 503);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${CONTENTS_URL}?ref=${BRANCH}`);
  assert.ok(!JSON.stringify(await response.json()).includes(environment.GITHUB_TOKEN));
});

test("built artifact is one import-free standard Worker ESM with same behavior", async () => {
  execFileSync(process.execPath, [resolve(root, "build.js")]);
  const artifact = readFileSync(resolve(root, "dist/index.js"), "utf8");
  assert.ok(!/^import\s/m.test(artifact));
  const built = await import(pathToFileURL(resolve(root, "dist/index.js")).href);
  const response = await built.default.fetch(new Request("https://service.invalid/health"));
  assert.deepEqual(await response.json(), { status: "ok" });
});
