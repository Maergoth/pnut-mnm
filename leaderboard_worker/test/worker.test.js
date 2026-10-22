import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";
import { webcrypto } from "node:crypto";
import { ADJECTIVES, COLORS, ANIMALS } from "../words.js";
import { API_ROOT, BRANCH, CONTENTS_URL, MAX_FILE_BYTES, MAX_REQUEST_BYTES,
  canonicalJson, createWorker, parseSnapshotJson, validateSnapshot } from "../worker.js";
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
  const { post } = setup([R.previous]);
  assert.equal((await post()).status, 201);
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
  assert.deepEqual(github.bytes.toString().trim().split("\n").map(JSON.parse), [R.older, R.other, R.current]);
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
