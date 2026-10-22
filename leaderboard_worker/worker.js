import { ADJECTIVES, COLORS, ANIMALS } from "./words.js";

export const MAX_FILE_BYTES = 8 * 1024 * 1024;
export const MAX_RECORDS = 30_000;
export const MAX_REQUEST_BYTES = 4096;
export const API_ROOT = "https://api.github.com/repos/Maergoth/pnut-mnm";
export const BRANCH = "leaderboard-data";
export const CONTENTS_URL = `${API_ROOT}/contents/leaderboard.jsonl`;
const MAX_API_BYTES = Math.ceil(MAX_FILE_BYTES / 3) * 4 + 65_536;
const COUNTERS = ["personal_kills", "personal_deaths", "high_hit", "coin_looted"];
const LEGACY_FIELDS = ["public_key", "submission_id", "alias", "captured_at", "max_health", "max_mana",
  ...COUNTERS, "sequence", "capture_backend", "signature"];
const ATTRIBUTES = ["strength", "stamina", "agility", "dexterity", "intelligence", "wisdom", "charisma"];
const RESISTS = ["cold", "corruption", "disease", "electricity", "fire", "holy", "magic", "poison"]
  .map((name) => "resist_" + name);
const READING_FIELDS = [...ATTRIBUTES, "armor_class", ...RESISTS];
const FIELDS = [...LEGACY_FIELDS, "schema_version", "message", ...READING_FIELDS, "stat_total", "resist_total"];
const WORDS = [ADJECTIVES, COLORS, ANIMALS];
const WORD_SETS = WORDS.map((words) => new Set(words));
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });
const HEX32 = /^[0-9a-f]{32}$/;
const HEX40 = /^[0-9a-f]{40}$/;
const HEX64 = /^[0-9a-f]{64}$/;
const HEX128 = /^[0-9a-f]{128}$/;
const MESSAGE_SPACE = /[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u;
const MESSAGE_CONTROL = /\p{C}/u;
// Match Python's ASCII-only case-insensitive alternatives with Unicode word boundaries.
const SWEAR_BODY = "motherfuck(?:ers?|ing)?|fuck(?:ers?|ing|ed|s)?|"
  + "bullshit(?:ting|ted|s)?|shit(?:ty|ting|ted|s|heads?)?|bitch(?:es|ing|y)?|"
  + "assholes?|bastards?|cunts?|dicks?|cock(?:s|suckers?)?|piss(?:ed|ing|es)?|damn(?:ed|ing)?|hell|ass";
const MESSAGE_SWEARS = new RegExp("(?<![\\p{L}\\p{N}_])(?:"
  + SWEAR_BODY.replace(/[a-z]/g, (letter) => `[${letter}${letter.toUpperCase()}]`)
  + ")(?![\\p{L}\\p{N}_])", "gu");

class InvalidSnapshot extends Error {}
class StorageError extends Error {}
class UpstreamError extends StorageError {
  constructor(status) { super("Storage unavailable"); this.status = status; }
}

function hex(bytes) {
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
function unhex(value) {
  const bytes = new Uint8Array(value.length / 2);
  for (let index = 0; index < bytes.length; index++) bytes[index] = parseInt(value.slice(index * 2, index * 2 + 2), 16);
  return bytes;
}
function joinBytes(first, second) {
  const result = new Uint8Array(first.length + second.length);
  result.set(first); result.set(second, first.length);
  return result;
}
function encodeBase64(bytes) {
  const pieces = [];
  for (let index = 0; index < bytes.length; index += 32_768) {
    pieces.push(String.fromCharCode(...bytes.subarray(index, index + 32_768)));
  }
  return btoa(pieces.join(""));
}
function decodeBase64(value) {
  let binary;
  try { binary = atob(value); } catch { throw new StorageError("Invalid file encoding"); }
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

export function parseStamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)
      || value.startsWith("0000")) throw new InvalidSnapshot("Invalid time");
  const stamp = Date.parse(value);
  if (!Number.isFinite(stamp) || new Date(stamp).toISOString().replace(".000Z", "Z") !== value) {
    throw new InvalidSnapshot("Invalid time");
  }
  return stamp;
}

export function canonicalJson(record) {
  return JSON.stringify(Object.fromEntries(Object.keys(record).filter((key) => key !== "signature")
    .sort().map((key) => [key, record[key]])))
    .replace(/[\u007f-\uffff]/g, (character) => "\\u" + character.charCodeAt(0).toString(16).padStart(4, "0"));
}

export function normalizeMessage(value) {
  if (typeof value !== "string") throw new InvalidSnapshot("Message must be text");
  const text = Array.from(value.normalize("NFKC"), (character) => MESSAGE_SPACE.test(character) ? " "
    : MESSAGE_CONTROL.test(character) ? "" : character).join("").replace(/ +/g, " ").replace(/^ +| +$/g, "");
  if (Array.from(text).length > 160) throw new InvalidSnapshot("Message is too long");
  return text.replace(MESSAGE_SWEARS, (word) => "*".repeat(word.length));
}

function validateShape(record) {
  if (!record || typeof record !== "object" || Array.isArray(record)) {
    throw new InvalidSnapshot("Unexpected fields");
  }
  const fields = Object.hasOwn(record, "schema_version") ? FIELDS : LEGACY_FIELDS;
  if (Object.keys(record).length !== fields.length || fields.some((key) => !Object.hasOwn(record, key))) {
    throw new InvalidSnapshot("Unexpected fields");
  }
  if (fields === FIELDS) {
    if (record.schema_version !== 2 && record.schema_version !== 3) throw new InvalidSnapshot("Unsupported version");
    if (normalizeMessage(record.message) !== record.message) throw new InvalidSnapshot("Invalid public message");
    for (const field of READING_FIELDS) {
      if (record[field] !== null && (!Number.isSafeInteger(record[field]) || Math.abs(record[field]) > 1_000_000_000)) {
        throw new InvalidSnapshot("Invalid character stat");
      }
    }
    for (const [total, group] of [["stat_total", ATTRIBUTES], ["resist_total", RESISTS]]) {
      const expected = group.every((field) => record[field] !== null)
        ? group.reduce((sum, field) => sum + record[field], 0) : null;
      if (record.schema_version === 2) {
        if (record[total] !== expected) throw new InvalidSnapshot("Invalid character total");
      } else if (record[total] !== null && (!Number.isSafeInteger(record[total])
          || Math.abs(record[total]) > group.length * 1_000_000_000
          || expected === null || record[total] > expected)) {
        throw new InvalidSnapshot("Invalid personal-best total");
      }
    }
  }
  for (const [field, pattern] of [["public_key", HEX64], ["submission_id", HEX32], ["signature", HEX128]]) {
    if (typeof record[field] !== "string" || !pattern.test(record[field])) throw new InvalidSnapshot("Invalid identity");
  }
  const alias = typeof record.alias === "string" ? record.alias.split(" ") : [];
  if (alias.length !== 3 || alias.some((word, index) => !WORD_SETS[index].has(word))) {
    throw new InvalidSnapshot("Invalid alias");
  }
  parseStamp(record.captured_at);
  if (record.capture_backend !== "window") throw new InvalidSnapshot("Unsupported capture");
  for (const field of ["max_health", "max_mana"]) {
    if (field === "max_mana" && record[field] === null) continue;
    if (!Number.isSafeInteger(record[field]) || record[field] < 1 || record[field] > 1_000_000_000) {
      throw new InvalidSnapshot("Invalid maximum");
    }
  }
  for (const field of [...COUNTERS, "sequence"]) {
    if (!Number.isSafeInteger(record[field]) || record[field] < (field === "sequence" ? 1 : 0)) {
      throw new InvalidSnapshot("Invalid total");
    }
  }
  return record;
}

export async function validateSnapshot(record, subtle = globalThis.crypto.subtle) {
  validateShape(record);
  const keyBytes = unhex(record.public_key);
  const digest = new DataView(await subtle.digest("SHA-256", keyBytes));
  const alias = WORDS.map((words, index) => words[Number(digest.getBigUint64(index * 8, false) % BigInt(words.length))]).join(" ");
  const identifier = hex(await subtle.digest("SHA-256", joinBytes(keyBytes, encoder.encode(":" + record.captured_at.slice(0, 10))))).slice(0, 32);
  if (record.alias !== alias || record.submission_id !== identifier) throw new InvalidSnapshot("Identity does not match");
  let verified = false;
  try {
    const key = await subtle.importKey("raw", keyBytes, "Ed25519", false, ["verify"]);
    verified = await subtle.verify("Ed25519", key, unhex(record.signature), encoder.encode(canonicalJson(record)));
  } catch { /* Malformed public keys and signatures are invalid input. */ }
  if (!verified) throw new InvalidSnapshot("Invalid signature");
  return record;
}

// This protocol is a flat object of strings, integers and null. Parsing it
// explicitly rejects duplicate keys and 1.0/1e0 numeric lexemes, which Python's
// strict integer type check rejects but JSON.parse otherwise rounds to ints.
export function parseSnapshotJson(text) {
  let offset = 0;
  const result = Object.create(null);
  const whitespace = () => { while (offset < text.length && /[\t\r\n ]/.test(text[offset])) offset++; };
  const stringToken = /"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"/y;
  whitespace();
  if (text[offset++] !== "{") throw new InvalidSnapshot("Invalid JSON");
  whitespace();
  if (text[offset] !== "}") {
    while (true) {
      stringToken.lastIndex = offset;
      const match = stringToken.exec(text);
      if (!match) throw new InvalidSnapshot("Invalid JSON");
      const key = JSON.parse(match[0]); offset = stringToken.lastIndex;
      if (Object.hasOwn(result, key)) throw new InvalidSnapshot("Duplicate field");
      whitespace();
      if (text[offset++] !== ":") throw new InvalidSnapshot("Invalid JSON");
      whitespace();
      let token;
      if (text[offset] === '"') {
        stringToken.lastIndex = offset;
        const value = stringToken.exec(text);
        if (!value) throw new InvalidSnapshot("Invalid JSON");
        token = value[0]; offset = stringToken.lastIndex;
      } else {
        const begin = offset;
        while (offset < text.length && !/[\t\r\n ,}]/.test(text[offset])) offset++;
        token = text.slice(begin, offset);
        if (token !== "null" && !/^-?(?:0|[1-9]\d*)$/.test(token)) throw new InvalidSnapshot("Invalid JSON value");
      }
      result[key] = JSON.parse(token);
      whitespace();
      if (text[offset] === "}") break;
      if (text[offset++] !== ",") throw new InvalidSnapshot("Invalid JSON");
      whitespace();
    }
  }
  offset++; whitespace();
  if (offset !== text.length) throw new InvalidSnapshot("Invalid JSON");
  return { ...result };
}

async function boundedBytes(message, limit) {
  const length = message.headers.get("content-length");
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > limit)) throw new StorageError("Response too large");
  if (!message.body) return new Uint8Array();
  const reader = message.body.getReader();
  const chunks = []; let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > limit) { await reader.cancel(); throw new StorageError("Response too large"); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const result = new Uint8Array(total); let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
  return result;
}

async function blobSha(bytes, subtle) {
  return hex(await subtle.digest("SHA-1", joinBytes(encoder.encode(`blob ${bytes.length}\0`), bytes)));
}

class GitHubStore {
  constructor(token, fetchImpl, subtle) { this.token = token; this.fetch = fetchImpl; this.subtle = subtle; }

  async request(method, url, payload = undefined, accept = "application/vnd.github+json") {
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 10_000);
    try {
      const response = await this.fetch(url, { method, redirect: "manual", signal: abort.signal,
        headers: { Authorization: `Bearer ${this.token}`, Accept: accept, "Content-Type": "application/json",
          "User-Agent": "PNUT-Leaderboard-Worker", "X-GitHub-Api-Version": "2022-11-28" },
        ...(payload === undefined ? {} : { body: JSON.stringify(payload) }) });
      if (!response.ok) { await response.body?.cancel(); throw new UpstreamError(response.status); }
      const result = JSON.parse(decoder.decode(await boundedBytes(response, MAX_API_BYTES)));
      if (!result || typeof result !== "object" || Array.isArray(result)) throw new StorageError("Invalid storage response");
      return result;
    } catch (error) {
      if (error instanceof StorageError) throw error;
      throw new StorageError("Storage unavailable");
    } finally { clearTimeout(timeout); }
  }

  async read(identity) {
    let metadata;
    try { metadata = await this.request("GET", `${CONTENTS_URL}?ref=${BRANCH}`, undefined, "application/vnd.github.object+json"); }
    catch (error) {
      if (!(error instanceof UpstreamError) || error.status !== 404) throw error;
      await this.request("GET", `${API_ROOT}/branches/${BRANCH}`);
      return { records: [], sha: null };
    }
    const { sha, size } = metadata;
    if (metadata.type !== "file" || typeof sha !== "string" || !HEX40.test(sha)
        || !Number.isSafeInteger(size) || size < 0 || size > MAX_FILE_BYTES) throw new StorageError("Invalid storage metadata");
    const blob = metadata.encoding === "none" ? await this.request("GET", `${API_ROOT}/git/blobs/${sha}`) : metadata;
    if (blob.encoding !== "base64" || typeof blob.content !== "string") throw new StorageError("Invalid storage content");
    const bytes = decodeBase64(blob.content);
    if (bytes.length !== size || await blobSha(bytes, this.subtle) !== sha) throw new StorageError("Invalid storage checksum");
    const records = [], seen = new Set();
    try {
      for (const line of decoder.decode(bytes).split(/\r?\n/)) {
        if (!line.trim()) continue;
        const record = validateShape(parseSnapshotJson(line));
        // Only this fixed repository is trusted as history. Verify the current
        // identity's signatures before using its daily/rollback decisions;
        // unrelated rows receive complete schema/type/date validation only.
        if (record.public_key === identity) await validateSnapshot(record, this.subtle);
        if (!seen.has(record.submission_id)) { seen.add(record.submission_id); records.push(record); }
        if (records.length > MAX_RECORDS) throw new StorageError("Storage capacity reached");
      }
    } catch { throw new StorageError("Invalid stored history"); }
    return { records, sha };
  }

  async append(record) {
    for (let attempt = 0; attempt < 3; attempt++) {
      const { records, sha } = await this.read(record.public_key);
      const existing = records.find((row) => row.submission_id === record.submission_id);
      if (existing) {
        if (existing.public_key !== record.public_key) throw new StorageError("Conflicting stored identity");
        return { snapshot: existing, created: false };
      }
      for (const previous of records) {
        if (previous.public_key !== record.public_key) continue;
        if (record.sequence <= previous.sequence || COUNTERS.some((field) => record[field] < previous[field])) {
          throw new InvalidSnapshot("Cumulative values cannot decrease");
        }
      }
      if (records.length >= MAX_RECORDS) throw new StorageError("Storage capacity reached");
      const bytes = encoder.encode([...records, record].map((row) => JSON.stringify(row) + "\n").join(""));
      if (bytes.length > MAX_FILE_BYTES) throw new StorageError("Storage capacity reached");
      const contentSha = await blobSha(bytes, this.subtle);
      try {
        const result = await this.request("PUT", CONTENTS_URL, { branch: BRANCH, message: "Add anonymous leaderboard snapshot",
          content: encodeBase64(bytes), ...(sha === null ? {} : { sha }) });
        if (!result.content || result.content.sha !== contentSha) throw new StorageError("Save was not confirmed");
        return { snapshot: record, created: true };
      } catch (error) {
        const conflict = error instanceof UpstreamError && (error.status === 409 || error.status === 422 && sha === null);
        if (!conflict || attempt === 2) throw error;
      }
    }
    throw new StorageError("Storage unavailable");
  }
}

function jsonResponse(status, value) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } });
}

export function createWorker({ fetchImpl = (...args) => globalThis.fetch(...args), now = () => Date.now(),
  subtle = globalThis.crypto.subtle } = {}) {
  return { async fetch(request, env = {}) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health" && !url.search) return jsonResponse(200, { status: "ok" });
    if (request.method !== "POST" || url.pathname !== "/snapshots" || url.search) return jsonResponse(404, { error: "Not found" });
    if (request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
      return jsonResponse(415, { error: "Content-Type must be application/json" });
    }
    let bytes;
    try { bytes = await boundedBytes(request, MAX_REQUEST_BYTES); }
    catch { return jsonResponse(413, { error: "Snapshot body is too large or empty" }); }
    if (!bytes.length) return jsonResponse(413, { error: "Snapshot body is too large or empty" });
    try {
      let record;
      try { record = parseSnapshotJson(decoder.decode(bytes)); }
      catch { throw new InvalidSnapshot("Invalid JSON"); }
      await validateSnapshot(record, subtle);
      const current = now(), stamp = parseStamp(record.captured_at);
      if (!Number.isFinite(current) || Math.abs(stamp - current) > 300_000
          || record.captured_at.slice(0, 10) !== new Date(current).toISOString().slice(0, 10)) throw new InvalidSnapshot("Invalid time");
      if (typeof env.GITHUB_TOKEN !== "string" || !env.GITHUB_TOKEN.trim() || /[\r\n]/.test(env.GITHUB_TOKEN)) {
        throw new StorageError("Storage unavailable");
      }
      const result = await new GitHubStore(env.GITHUB_TOKEN, fetchImpl, subtle).append(record);
      return jsonResponse(result.created ? 201 : 200, result);
    } catch (error) {
      return error instanceof InvalidSnapshot ? jsonResponse(400, { error: "Invalid snapshot" })
        : jsonResponse(503, { error: "Leaderboard storage is temporarily unavailable" });
    }
  } };
}

export default createWorker();
