import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";

const { outputFiles } = await build({
  entryPoints: [new URL("../src/lib/api.ts", import.meta.url).pathname],
  bundle: true,
  write: false,
  format: "esm",
  define: { "import.meta.env.DEV": "false" },
});
const { api } = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].contents).toString("base64")}`);

test("confirmed purge sends the required server confirmation", async () => {
  const calls = [];
  globalThis.window = {
    AstrBotPluginPage: {
      apiPost: async (...args) => {
        calls.push(args);
        return { success: true, data: { results: { supplements: 2 } } };
      },
    },
  };
  assert.deepEqual(await api.postPurge(["supplements"]), { results: { supplements: 2 } });
  assert.deepEqual(calls, [["actions/purge", { modules: ["supplements"], confirm: "PURGE" }]]);
  delete globalThis.window;
});

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

test("configuration reads wait for a pending autosave", async () => {
  const gate = deferred();
  const calls = [];
  globalThis.window = { AstrBotPluginPage: {
    apiPost: async () => {
      calls.push("write");
      await gate.promise;
      return { success: true, data: {} };
    },
    apiGet: async () => {
      calls.push("read");
      return { success: true, data: { enabled: false } };
    },
  } };
  const write = api.postSaveConfig({ enabled: false });
  const read = api.getConfig();
  await new Promise((done) => setImmediate(done));
  assert.deepEqual(calls, ["write"]);
  gate.resolve();
  await write;
  assert.deepEqual(await read, { enabled: false });
  assert.deepEqual(calls, ["write", "read"]);
  delete globalThis.window;
});

test("an old configuration response cannot overwrite a completed save", async () => {
  const gate = deferred();
  const started = deferred();
  let reads = 0;
  globalThis.window = { AstrBotPluginPage: {
    apiPost: async () => ({ success: true, data: {} }),
    apiGet: async () => {
      reads += 1;
      if (reads === 1) {
        started.resolve();
        await gate.promise;
        return { success: true, data: { enabled: true } };
      }
      return { success: true, data: { enabled: false } };
    },
  } };
  const read = api.getConfig();
  await started.promise;
  await api.postSaveConfig({ enabled: false });
  gate.resolve();
  assert.deepEqual(await read, { enabled: false });
  assert.equal(reads, 2);
  delete globalThis.window;
});

test("simultaneous source toggles read and modify the latest source map serially", async () => {
  let sources = { modelsdev: { enabled: true }, litellm: { enabled: true } };
  globalThis.window = { AstrBotPluginPage: {
    apiGet: async () => ({ success: true, data: { price_sources: structuredClone(sources) } }),
    apiPost: async (_endpoint, body) => {
      sources = structuredClone(body.price_sources);
      return { success: true, data: {} };
    },
  } };
  await Promise.all([
    api.postPriceSource("modelsdev", { enabled: false }),
    api.postPriceSource("litellm", { enabled: false }),
  ]);
  assert.deepEqual(sources, { modelsdev: { enabled: false }, litellm: { enabled: false } });
  delete globalThis.window;
});

test("source toggles preserve an explicit anonymous authentication setting", async () => {
  let source = { provider_id: "provider", enabled: false, use_provider_key: false };
  globalThis.window = { AstrBotPluginPage: {
    apiGet: async () => ({ success: true, data: { price_sources: { "newapi:provider": source } } }),
    apiPost: async (_endpoint, body) => {
      source = body.price_sources["newapi:provider"];
      return { success: true, data: {} };
    },
  } };
  await api.postPriceSource("newapi:provider", { enabled: true });
  assert.equal(source.enabled, true);
  assert.equal(source.use_provider_key, false);
  delete globalThis.window;
});
