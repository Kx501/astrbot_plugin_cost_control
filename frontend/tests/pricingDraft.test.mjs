import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";

const { outputFiles } = await build({
  entryPoints: [new URL("../src/components/ProviderPricingCard.tsx", import.meta.url).pathname],
  bundle: true, write: false, format: "esm", jsx: "automatic",
  define: { "import.meta.env.DEV": "false" },
  plugins: [{ name: "shared-react", setup(build) {
    build.onResolve({ filter: /^react(?:\/|$)/ }, ({ path }) => ({
      path: new URL(`../node_modules/${path === "react" ? "react/index" : path}.js`, import.meta.url).href,
      external: true,
    }));
  } }],
});
const { entryToDraft, draftToEntry, normalizeDefaultCurrency } = await import(
  `data:text/javascript;base64,${Buffer.from(outputFiles[0].contents).toString("base64")}`,
);

test("pricing draft round trips explicit zero service-tier multipliers", () => {
  const entry = {
    mode: "per_tier", base: { input: 2, output: 5 }, context_tiers: [],
    service_tiers: [{ match: "free", input_multiplier: 0, output_multiplier: 0 }],
  };
  const result = draftToEntry(entryToDraft(entry));
  assert.deepEqual(result.service_tiers, entry.service_tiers);
});

test("price drafts reject non-finite and partially numeric values before JSON turns them into null", () => {
  const base = entryToDraft({ mode: "per_token", input: 1 });
  for (const value of ["Infinity", "1e999", "3oops"]) {
    assert.throws(() => draftToEntry({ ...base, input: value }), /非法数值/);
  }
});

test("legacy currency symbols initialize price drafts using the same currency", () => {
  assert.equal(normalizeDefaultCurrency("¥"), "CNY");
  assert.equal(normalizeDefaultCurrency("€"), "EUR");
  assert.equal(normalizeDefaultCurrency("$"), "");
});
