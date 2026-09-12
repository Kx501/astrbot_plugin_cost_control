import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { transform } from "esbuild";

process.env.TZ = "Asia/Shanghai";
const source = await readFile(new URL("../src/lib/recordRange.ts", import.meta.url), "utf8");
const { code } = await transform(source, { loader: "ts", format: "esm" });
const { rangeParams } = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);

test("today uses local midnight even when UTC is still yesterday", () => {
  const range = rangeParams({ preset: "today" }, new Date("2026-09-06T00:30:00+08:00"));
  assert.deepEqual(range, {
    start: "2026-09-05T16:00:00.000Z",
    end: "2026-09-06T15:59:59.999999Z",
  });
});

test("seven days and custom dates use the same local day bounds", () => {
  const expected = { start: "2026-08-30T16:00:00.000Z", end: "2026-09-06T15:59:59.999999Z" };
  assert.deepEqual(rangeParams({ preset: "7d" }, new Date("2026-09-06T12:00:00+08:00")), expected);
  assert.deepEqual(rangeParams({ preset: "custom", start: "2026-08-31", end: "2026-09-06" }), expected);
});
