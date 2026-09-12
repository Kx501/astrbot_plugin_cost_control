// API 层：经 bridge 调后端 REST，统一 extractData 解包 {success,data} 信封。
// endpoint 不带前导斜杠、不带插件名（bridge 自动补 /api/plug/<pluginName>/ 前缀）。

import { getBridge } from "./bridge";
import type {
  AlertItem,
  AiDiagCached,
  AiDiagResult,
  AiProviderInfo,
  AttributionResponse,
  Bucket,
  BudgetResponse,
  CacheResponse,
  CatalogPrice,
  DeleteProviderDataResult,
  CompareResult,
  DetectResult,
  ExprValidateResult,
  OverviewReport,
  PriceSelection,
  PricingResponse,
  Provider,
  RecordRow,
  RecordsAggregate,
  SourceStatus,
  SyncReport,
  TimelineResponse,
  Window,
} from "./types";

export class ApiError extends Error {}

// 后端用非标准 {success, data}；父级 SPA 对它原样透传，前端自行解包
export function extractData<T>(response: unknown): T {
  if (response && typeof response === "object") {
    const r = response as { success?: boolean; data?: unknown; error?: string };
    if (r.success === true) return r.data as T;
    if (r.success === false) throw new ApiError(r.error || "请求失败");
  }
  return response as T;
}

async function get<T>(endpoint: string, params?: Record<string, unknown>): Promise<T> {
  const bridge = getBridge();
  if (!bridge) throw new ApiError("Bridge SDK 未就绪");
  return extractData<T>(await bridge.apiGet(endpoint, params ?? {}));
}

async function post<T>(endpoint: string, body?: unknown): Promise<T> {
  const bridge = getBridge();
  if (!bridge) throw new ApiError("Bridge SDK 未就绪");
  return extractData<T>(await bridge.apiPost(endpoint, body));
}

// Configuration readers wait for locally initiated mutations and retry if a
// mutation started while the response was in flight. This also covers switching
// tabs while the old page flushes its draft on unmount.
let configWrite: Promise<unknown> = Promise.resolve();
let configRevision = 0;

function mutateConfig<T>(write: () => Promise<T>): Promise<T> {
  configRevision += 1;
  const pending = configWrite.catch(() => {}).then(write);
  configWrite = pending;
  return pending;
}

async function readConfig<T>(endpoint: string): Promise<T> {
  for (;;) {
    const pending = configWrite;
    await pending.catch(() => {});
    if (pending !== configWrite) continue;
    const revision = configRevision;
    const value = await get<T>(endpoint);
    if (revision === configRevision) return value;
  }
}

export const api = {
  // overview
  getOverview: (window: Window) => get<OverviewReport>("overview", { window }),
  getAlerts: (window: Window) => get<AlertItem[]>("alerts", { window }),
  getCompare: (window: Window) => get<CompareResult | null>("compare", { window }),
  getTimeline: (
    days: number,
    bucket: Bucket = "day",
    extra?: Record<string, unknown>,
  ) => get<TimelineResponse>("timeline", { days, bucket, ...extra }),

  // records
  getRecords: (filter: Record<string, unknown>) => get<RecordRow[]>("records", filter),
  getRecordsAggregate: (params: Record<string, unknown>) =>
    get<RecordsAggregate>("records/aggregate", params),

  // budgets
  getBudgets: () => readConfig<BudgetResponse>("budgets"),
  getProviders: () => get<{ providers: Provider[] }>("providers"),

  // cache / attribution / pricing / config
  getCache: (window: Window, limit?: number) =>
    get<CacheResponse>("cache", limit != null ? { window, limit } : { window }),
  getAttribution: (window: Window, limit?: number) =>
    get<AttributionResponse>(
      "attribution",
      limit != null ? { window, limit } : { window },
    ),
  getPricing: () => readConfig<PricingResponse>("pricing"),
  getConfig: () => readConfig<Record<string, unknown>>("config"),

  // actions
  postCleanup: () => post<{ deleted: number; message?: string }>("actions/cleanup"),
  postPurge: (modules: string[]) =>
    post<{ results: Record<string, number> }>("actions/purge", {
      modules,
      confirm: "PURGE",
    }),
  postDeleteProviderData: (providerId: string) =>
    mutateConfig(() => post<DeleteProviderDataResult>("actions/delete_provider_data", {
      provider_id: providerId,
      confirm: "DELETE_PROVIDER_DATA",
    })),
  postReport: () => post<{ message: string }>("actions/report"),
  postSaveConfig: (body: unknown) =>
    mutateConfig(() => post<{ saved: string[]; config: Record<string, unknown> }>(
      "actions/save_config",
      body,
    )),
  postPriceSource: (sourceId: string, patch: Record<string, unknown>) =>
    mutateConfig(async () => {
      // Read inside the writer queue so simultaneous source toggles cannot replace
      // each other with copies of the same stale source map.
      const cfg = await get<Record<string, unknown>>("config");
      const sources = { ...((cfg.price_sources as Record<string, unknown>) ?? {}) };
      const previous = (sources[sourceId] as Record<string, unknown>) ?? {};
      const next = { ...previous, ...patch };
      if (sourceId.startsWith("newapi:")) {
        next.provider_id = previous.provider_id || patch.provider_id || sourceId.slice(7);
        if (!("use_provider_key" in next)) next.use_provider_key = true;
      }
      sources[sourceId] = next;
      return post<{ saved: string[]; config: Record<string, unknown> }>(
        "actions/save_config", { price_sources: sources },
      );
    }),
  postSyncRates: () =>
    mutateConfig(() => post<{
      exchange_rates: Record<string, number>;
      exchange_rates_updated_at: string;
      count: number;
    }>("actions/sync_rates")),
  // 多源价格目录（F1/F2/F3）
  postPricingSync: (sources?: string[]) =>
    post<SyncReport>("pricing/sync", sources ? { sources } : {}),
  getPricingCatalog: () =>
    get<{
      updated_at?: string;
      sources?: Record<string, SourceStatus>;
      prices?: Record<string, CatalogPrice>;
    }>("pricing/catalog"),
  postPricingSelect: (body: {
    provider_id: string;
    model: string;
    price_key: string;
  }) => mutateConfig(() => post<{ selected: PriceSelection }>("pricing/select", body)),
  postPricingSelectReset: (body: { provider_id: string; model?: string }) =>
    mutateConfig(() => post<{ removed: number }>("pricing/select/reset", body)),
  postPricingDetect: (provider_id: string) =>
    post<DetectResult>("pricing/sources/detect", { provider_id }),
  postPricingExprValidate: (expr: string) =>
    post<ExprValidateResult>("pricing/expr/validate", { expr }),

  // AI 诊断
  getAiProvider: () => get<AiProviderInfo>("ai_provider"),
  getAiDiagLast: () => get<AiDiagCached>("ai_diag_last"),
  postAiDiag: () => post<AiDiagResult>("ai_diag"),
};
