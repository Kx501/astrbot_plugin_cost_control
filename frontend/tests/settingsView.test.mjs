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
  entryPoints: [new URL("../src/views/SettingsView.tsx", import.meta.url).pathname],
  bundle: true, write: false, format: "esm", jsx: "automatic",
  define: { "import.meta.env.DEV": "false" },
  plugins: [{ name: "shared-react", setup(build) {
    build.onResolve({ filter: /^react(?:\/|$)/ }, ({ path }) => ({
      path: new URL(`../node_modules/${path === "react" ? "react/index" : path}.js`, import.meta.url).href,
      external: true,
    }));
  } }],
});
const { SettingsView } = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].contents).toString("base64")}`);

test("settings hydrates real field paths and saves only fields owned by the page", async () => {
  const writes = [];
  const cfg = {
    enabled: true, currency_symbol: "$", refresh_time: "00:00", platforms: ["telegram_official"],
    alerts: { enabled: true, cooldown_seconds: 300, daily_report_time: "12:30", daily_report_to: ["old-session"] },
    schedule: { enable_daily_report: true, retain_days: 45 },
    budgets: { global_daily: 123 }, pricing: { provider: { input: 2 } },
    exchange_rates: { USD: 1, CNY: 7 },
  };
  window.AstrBotPluginPage = {
    apiGet: async (endpoint) => ({ success: true, data: endpoint === "config" ? cfg : { providers: [] } }),
    apiPost: async (endpoint, body) => {
      writes.push([endpoint, body]);
      return { success: true, data: {} };
    },
  };
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => { root.render(React.createElement(SettingsView)); });
  const field = (label) => [...container.querySelectorAll(".set-field")]
    .find((element) => element.querySelector(".set-field-label")?.textContent === label);
  assert.equal(field("日报推送时间").querySelector("input").value, "12:30");
  assert.equal(field("生效平台").querySelector("input").value, "telegram_official");
  assert.equal(field("主货币").querySelector("select").value, "USD");
  assert.deepEqual(writes, []);
  const recipients = field("日报接收方").querySelector("input");
  act(() => {
    recipients.value = "new-session";
    recipients.dispatchEvent(new window.FocusEvent("focusout", { bubbles: true }));
  });
  await act(async () => { root.unmount(); });
  assert.equal(writes.length, 1);
  const [endpoint, payload] = writes[0];
  assert.equal(endpoint, "actions/save_config");
  assert.deepEqual(payload.alerts.daily_report_to, ["new-session"]);
  assert.equal(payload.alerts.daily_report_time, "12:30");
  assert.deepEqual(payload.platforms, ["telegram_official"]);
  assert.equal("report" in payload, false);
  assert.equal("advanced" in payload, false);
  assert.equal("budgets" in payload, false);
  assert.equal("pricing" in payload, false);
  assert.equal("exchange_rates" in payload, false);
  container.remove();
});
