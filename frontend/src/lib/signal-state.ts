import type { Signal } from "./a500-client";

export function signalFromEvent(data: unknown): Signal | null {
  if (!data || typeof data !== "object") return null;
  const value = data as { signal?: unknown };
  const candidate = value.signal && typeof value.signal === "object" ? value.signal : data;
  if (!("symbol" in candidate) || typeof candidate.symbol !== "string") return null;
  return candidate as Signal;
}

export function mergeSignal(current: Signal[], next: Signal): Signal[] {
  const index = current.findIndex((signal) => signal.symbol === next.symbol);
  if (index < 0) return [...current, next];
  return current.map((signal, position) => position === index ? next : signal);
}
