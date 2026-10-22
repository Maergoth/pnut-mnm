import { createHash } from "node:crypto";
import { API_ROOT, BRANCH, CONTENTS_URL } from "../worker.js";

export const blobSha = (bytes) => createHash("sha1").update(`blob ${bytes.length}\0`).update(bytes).digest("hex");
export const history = (records) => Buffer.from(records.map((record) => JSON.stringify(record) + "\n").join(""));

export class FakeGitHub {
  constructor(records = []) {
    this.bytes = history(records);
    this.calls = [];
    this.puts = 0;
    this.branchExists = true;
    this.conflicts = [];
    this.encodingNone = false;
    this.metadata = {};
    this.error = 0;
    this.badConfirmation = false;
    this.fetch = this.fetch.bind(this);
  }
  async fetch(url, options) {
    this.calls.push({ url, options });
    if (this.error) return Response.json({ error: "Upstream private detail" }, { status: this.error });
    if (options.method === "GET" && url === `${CONTENTS_URL}?ref=${BRANCH}`) {
      if (this.bytes === null) return Response.json({}, { status: 404 });
      return Response.json({ type: "file", sha: blobSha(this.bytes), size: this.bytes.length,
        encoding: this.encodingNone ? "none" : "base64", content: this.encodingNone ? "" : this.bytes.toString("base64"),
        download_url: "https://untrusted.invalid/do-not-follow", ...this.metadata });
    }
    if (options.method === "GET" && url === `${API_ROOT}/branches/${BRANCH}`) {
      return Response.json({ name: BRANCH }, { status: this.branchExists ? 200 : 404 });
    }
    if (options.method === "GET" && url === `${API_ROOT}/git/blobs/${blobSha(this.bytes)}`) {
      return Response.json({ encoding: "base64", sha: blobSha(this.bytes), content: this.bytes.toString("base64") });
    }
    if (options.method === "PUT" && url === CONTENTS_URL) {
      this.puts++;
      if (this.conflicts.length) {
        const action = this.conflicts.shift();
        if (typeof action === "function") action(this);
        return Response.json({}, { status: 409 });
      }
      const payload = JSON.parse(options.body);
      if ((payload.sha ?? null) !== (this.bytes === null ? null : blobSha(this.bytes))) {
        return Response.json({}, { status: 409 });
      }
      if (payload.branch !== BRANCH) throw new Error("Wrong branch");
      this.bytes = Buffer.from(payload.content, "base64");
      return Response.json(this.badConfirmation ? {} : { content: { sha: blobSha(this.bytes) }, commit: { sha: "a".repeat(40) } });
    }
    throw new Error("Unexpected destination: " + url);
  }
}
