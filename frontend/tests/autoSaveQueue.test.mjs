import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { transform } from "esbuild";

const source = await readFile(new URL("../src/lib/autoSaveQueue.ts", import.meta.url), "utf8");
const { code } = await transform(source, { loader: "ts", format: "esm" });
const { AutoSaveQueue } = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

test("hydration seeds without writing; disabling prevents saves", async () => {
  const calls = [];
  const queue = new AutoSaveQueue(async (value) => calls.push(value));
  queue.update({}, false);
  queue.update({ value: 1 }, true);
  await queue.flush();
  assert.deepEqual(calls, []);
  queue.update({ value: 2 }, false);
  await queue.flush();
  assert.deepEqual(calls, []);
});

test("an edit during a save is written after that request, never marked saved prematurely", async () => {
  const gate = deferred();
  const calls = [];
  const queue = new AutoSaveQueue(async (value) => {
    calls.push(value);
    if (calls.length === 1) await gate.promise;
  });
  queue.update(0, true);
  queue.update(1, true);
  const first = queue.flush();
  await Promise.resolve();
  queue.update(2, true);
  queue.update(3, true);
  const second = queue.flush();
  assert.strictEqual(first, second);
  assert.deepEqual(calls, [1]);
  gate.resolve();
  await second;
  assert.deepEqual(calls, [1, 3]);
  assert.equal(queue.isDirty(), false);
});

test("reverting to the original value while saving still restores the server", async () => {
  const gate = deferred();
  const calls = [];
  const queue = new AutoSaveQueue(async (value) => {
    calls.push(value);
    if (calls.length === 1) await gate.promise;
  });
  queue.update(0, true);
  queue.update(1, true);
  const save = queue.flush();
  await Promise.resolve();
  queue.update(0, true);
  assert.equal(queue.isDirty(), true);
  gate.resolve();
  await save;
  assert.deepEqual(calls, [1, 0]);
});

test("failure rejects flush and preserves changes for an explicit retry", async () => {
  let fail = true;
  const calls = [];
  const queue = new AutoSaveQueue(async (value) => {
    calls.push(value);
    if (fail) throw new Error("disk full");
  });
  queue.update(0, true);
  queue.update(1, true);
  await assert.rejects(queue.flush(), /disk full/);
  assert.equal(queue.isDirty(), true);
  fail = false;
  await queue.flush();
  assert.deepEqual(calls, [1, 1]);
  assert.equal(queue.isDirty(), false);
});

test("flush after delayed enabling writes the current draft", async () => {
  const calls = [];
  const queue = new AutoSaveQueue(async (value) => calls.push(value));
  queue.update({}, false);
  queue.update({ threshold: 100 }, true);
  queue.update({ threshold: 200 }, true);
  await queue.flush();
  assert.deepEqual(calls, [{ threshold: 200 }]);
});
