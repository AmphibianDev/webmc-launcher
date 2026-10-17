#!/usr/bin/env node
/* The launcher's GitHub flow, checked where it can be checked without a token.
 *
 *   node tools/test-launcher-github.mjs
 *
 * The page drives GitHub's contents API from the browser, and the one thing that is easy to get wrong is the
 * file that is *already* on the branch: GitHub answers 422 '"sha" wasn't supplied' unless the request carries
 * the sha of the copy that is there. Every path the launcher writes usually exists - the mod it is being
 * asked to add is in the repository already, and mods/mods.json certainly is - so that is not an edge case,
 * it is the normal one. It is also exactly what the first real build hit.
 *
 * This lifts the functions out of index.html itself, so what is tested is the page, not a copy of it.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { webcrypto } from "node:crypto";
import { Blob } from "node:buffer";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const html = readFileSync(path.join(ROOT, "index.html"), "utf8");
const script = html.match(/<script>\n([\s\S]*)\n<\/script>/)[1];

// everything the page defines, without running its main()
const body = script.split("main().catch")[0];

let failures = 0;
function check(name, ok, detail = "") {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${ok || !detail ? "" : `\n        ${detail}`}`);
  if (!ok) failures++;
}

// a document stub: only what these functions touch
const element = () => ({ value: "", hidden: false, style: {}, textContent: "", innerHTML: "",
                         append() {}, appendChild() {}, addEventListener() {}, querySelectorAll: () => [],
                         querySelector: () => element(), classList: { add() {}, remove() {} }, firstElementChild: { style: {} } });
globalThis.el = () => element();
globalThis.document = { createElement: () => element(), createTextNode: () => ({}), body: element() };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.crypto ??= webcrypto;
globalThis.Blob ??= Blob;
globalThis.btoa ??= (text) => Buffer.from(text, "binary").toString("base64");

const page = await import(`data:text/javascript,${encodeURIComponent(body + "\nexport { putFile, whyFailed, checkWriteAccess, modId };")}`);

const calls = [];
function fakeGitHub({ permissions = { push: true } } = {}) {
  calls.length = 0;
  globalThis.fetch = async (url, init = {}) => {
    const target = String(url);
    calls.push({ url: target, method: init.method || "GET", body: init.body });
    if (!init.method || init.method === "GET") {
      if (/\/repos\/[^/]+\/[^/]+$/.test(target)) {
        return new Response(JSON.stringify({ permissions }), { status: 200 });
      }
      if (target.includes("existing.jar")) {
        return new Response(JSON.stringify({ sha: "the-old-blob" }), { status: 200 });
      }
      if (target.includes("/contents/")) return new Response(JSON.stringify({ message: "Not Found" }), { status: 404 });
      return new Response("{}", { status: 200 });
    }
    const body = JSON.parse(init.body);
    if (target.includes("existing.jar") && body.sha !== "the-old-blob") {
      return new Response(JSON.stringify({ message: '"sha" wasn\'t supplied.' }), { status: 422 });
    }
    if (target.includes("fresh.jar") && body.sha) {
      return new Response(JSON.stringify({ message: "sha given for a file that is not there" }), { status: 422 });
    }
    return new Response(JSON.stringify({ content: { sha: "new" } }), { status: 201 });
  };
}

console.log("\nthe page's own functions");
{
  check("putFile is where the page writes a file", typeof page.putFile === "function");
  check("and modId turns a jar name into an id", page.modId("Meteor Client 26.2-35.jar") === "meteor-client-26.2-35",
    page.modId("Meteor Client 26.2-35.jar"));
}

console.log("\nwriting a file that is already on the branch");
{
  fakeGitHub();
  const response = await page.putFile("token", "owner/repo", "build/abc", "mods/vendor/existing.jar", "add", "AAAA");
  check("it succeeds rather than answering 422", response.ok, `status ${response.status}`);
  const put = calls.find((call) => call.method === "PUT");
  check("because the sha of the copy that is there travels with it",
    JSON.parse(put.body).sha === "the-old-blob", put.body);
  check("and the sha was read from the branch being written, not from main",
    calls.some((call) => call.url.includes("?ref=build%2Fabc")), calls.map((c) => c.url).join("\n"));
}

console.log("\nwriting a file that is not there yet");
{
  fakeGitHub();
  const response = await page.putFile("token", "owner/repo", "build/abc", "mods/vendor/fresh.jar", "add", "AAAA");
  check("it is written without a sha", response.ok, `status ${response.status}`);
  const put = calls.find((call) => call.method === "PUT");
  check("and the request carries no sha at all", !("sha" in JSON.parse(put.body)), put.body);
}

console.log("\nwhat the page says when a token cannot write");
{
  fakeGitHub({ permissions: { push: false } });
  const problem = await page.checkWriteAccess("token", "owner/repo");
  check("a read-only token is caught before a build is attempted", /not writable/.test(problem), String(problem));
  check("and the advice names both kinds of token",
    /classic token/i.test(problem) && /fine-grained/i.test(problem), String(problem));

  fakeGitHub({ permissions: { push: true } });
  check("a token that can write passes", (await page.checkWriteAccess("token", "owner/repo")) === null);

  globalThis.fetch = async () => new Response(JSON.stringify({ message: "Bad credentials" }), { status: 401 });
  const expired = await page.checkWriteAccess("bad", "owner/repo");
  check("an expired token is named as such", /expired|revoked/.test(expired), String(expired));
}

console.log("\nwhat a 403 from GitHub turns into");
{
  globalThis.fetch = async () => new Response(JSON.stringify({ message: "Resource not accessible by personal access token" }), { status: 403 });
  const words = await page.whyFailed(new Response(JSON.stringify({ message: "Resource not accessible by personal access token" }), { status: 403 }), "creating build/abc");
  check("the message GitHub gave is kept", /Resource not accessible/.test(words), words);
  check("the two ways to fix it are named", /classic token/i.test(words) && /fine-grained/i.test(words), words);
}

console.log(failures ? `\n${failures} check(s) failed` : "\nall launcher GitHub checks passed");
process.exit(failures ? 1 : 0);
