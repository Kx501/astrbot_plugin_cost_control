import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";
import React, { act } from "react";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "http://localhost" });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { outputFiles } = await build({
  entryPoints: [new URL("../src/hooks/useAutoSave.ts", import.meta.url).pathname],
  bundle: true, write: false, format: "esm",
  plugins: [{ name: "shared-react", setup(build) {
    build.onResolve({ filter: /^react$/ }, () => ({
      path: new URL("../node_modules/react/index.js", import.meta.url).href, external: true,
    }));
  } }],
});
const { useAutoSave } = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].contents).toString("base64")}`);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

function mount(t, onSave) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  let result;
  let mounted = true;
  function Harness({ payload, enabled }) {
    result = useAutoSave(payload, onSave, { enabled, delay: 20, toastMs: 5 });
    return null;
  }
  const unmount = () => {
    if (mounted) act(() => root.unmount());
    mounted = false;
  };
  t.after(() => { unmount(); container.remove(); });
  return {
    render: (payload, enabled = true) => act(() => root.render(React.createElement(Harness, { payload, enabled }))),
    get result() { return result; },
    unmount,
  };
}

test("React hook seeds after hydration and debounces the first actual edit", async (t) => {
  const calls = [];
  const hook = mount(t, async (payload) => calls.push(payload));
  hook.render({}, false);
  hook.render({ limit: 100 });
  await act(async () => { await sleep(30); });
  assert.deepEqual(calls, []);
  hook.render({ limit: 200 });
  await act(async () => { await sleep(30); });
  assert.deepEqual(calls, [{ limit: 200 }]);
  assert.equal(hook.result.isDirty(), false);
});

test("React cleanup immediately starts saving after delayed enabling", async (t) => {
  const calls = [];
  const hook = mount(t, async (payload) => calls.push(payload));
  hook.render({}, false);
  hook.render({ limit: 100 });
  hook.render({ limit: 200 });
  hook.unmount();
  assert.deepEqual(calls, [{ limit: 200 }]);
  await act(async () => {});
});

test("React hook drains a later edit even if unmounted during the first save", async (t) => {
  const calls = [];
  const gate = deferred();
  const hook = mount(t, async (payload) => {
    calls.push(payload);
    if (calls.length === 1) await gate.promise;
  });
  hook.render(0);
  hook.render(1);
  let saving;
  act(() => { saving = hook.result.flush(); });
  hook.render(2);
  assert.equal(hook.result.isDirty(), true);
  hook.unmount();
  gate.resolve();
  await saving;
  assert.deepEqual(calls, [1, 2]);
});

test("React flush rejects failed saves, keeps the draft dirty, and allows retry", async (t) => {
  let fail = true;
  const hook = mount(t, async () => { if (fail) throw new Error("write failed"); });
  hook.render(0);
  hook.render(1);
  await act(async () => { await assert.rejects(hook.result.flush(), /write failed/); });
  assert.equal(hook.result.status, "error");
  assert.equal(hook.result.isDirty(), true);
  fail = false;
  await act(async () => { await hook.result.flush(); });
  assert.equal(hook.result.isDirty(), false);
});
