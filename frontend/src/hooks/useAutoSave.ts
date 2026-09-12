import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AutoSaveQueue } from "../lib/autoSaveQueue";

export type SaveStatus = "idle" | "saving" | "saved" | "error";

interface UseAutoSaveOptions {
  delay?: number;
  toastMs?: number;
  /** Wait for initial hydration before seeding the saved baseline. */
  enabled?: boolean;
}

interface UseAutoSaveResult {
  status: SaveStatus;
  error?: string;
  /** Wait for all pending changes, and reject if they could not be saved. */
  flush: () => Promise<void>;
  isDirty: () => boolean;
}

export function useAutoSave<T>(
  payload: T,
  onSave: (payload: T) => Promise<void>,
  opts: UseAutoSaveOptions = {},
): UseAutoSaveResult {
  const { delay = 800, toastMs = 1500, enabled = true } = opts;
  const serialized = useMemo(() => JSON.stringify(payload), [payload]);
  const onSaveRef = useRef(onSave);
  onSaveRef.current = onSave;
  const mountedRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const resetRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [status, setStatus] = useState<SaveStatus>("idle");
  const [error, setError] = useState<string>();
  const queueRef = useRef<AutoSaveQueue<T> | null>(null);
  if (!queueRef.current) {
    queueRef.current = new AutoSaveQueue((value) => onSaveRef.current(value));
  }
  const queue = queueRef.current;

  useLayoutEffect(() => {
    queue.update(payload, enabled);
  }, [queue, payload, enabled]);

  const clearReset = useCallback(() => {
    if (resetRef.current !== null) clearTimeout(resetRef.current);
    resetRef.current = null;
  }, []);

  const flush = useCallback(async () => {
    if (timerRef.current !== null) clearTimeout(timerRef.current);
    timerRef.current = null;
    if (!queue.isDirty()) return;
    clearReset();
    if (mountedRef.current) setStatus("saving");
    try {
      await queue.flush();
      if (mountedRef.current) {
        setStatus("saved");
        setError(undefined);
        clearReset();
        resetRef.current = setTimeout(() => setStatus("idle"), toastMs);
      }
    } catch (e) {
      if (mountedRef.current) {
        setStatus("error");
        setError(e instanceof Error ? e.message : String(e));
        clearReset();
        resetRef.current = setTimeout(() => setStatus("idle"), toastMs * 3);
      }
      throw e;
    }
  }, [queue, toastMs, clearReset]);

  useEffect(() => {
    if (!enabled || !queue.isDirty()) return;
    timerRef.current = setTimeout(() => void flush().catch(() => {}), delay);
    return () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      timerRef.current = null;
    };
  }, [serialized, enabled, delay, flush, queue]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      clearReset();
      // Uses the current enabled state; an in-flight save drains the remaining draft.
      void queue.flush().catch(() => {});
    };
  }, [queue, clearReset]);

  const isDirty = useCallback(() => queue.isDirty(), [queue]);
  return { status, error, flush, isDirty };
}
