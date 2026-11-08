import type { AccountSnapshot } from "./a500-client";

const decimalString = /^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
export type SnapshotDraft = Record<keyof AccountSnapshot, string>;
export const snapshotFields = [
  ["as_of", "快照时间（含时区）"],
  ["available_cash", "可用现金"],
  ["holdings", "持仓数量（JSON）"],
  ["protected_floor", "保护底仓（JSON）"],
  ["sellable", "可卖数量（JSON）"],
  ["cost_basis", "成本基础（JSON）"],
] as const;
export function snapshotDraft(snapshot?: AccountSnapshot): SnapshotDraft {
  return {
    as_of: snapshot?.as_of ?? "",
    available_cash: snapshot?.available_cash ?? "0",
    holdings: JSON.stringify(snapshot?.holdings ?? {}, null, 2),
    protected_floor: JSON.stringify(snapshot?.protected_floor ?? {}, null, 2),
    sellable: JSON.stringify(snapshot?.sellable ?? {}, null, 2),
    cost_basis: JSON.stringify(snapshot?.cost_basis ?? {}, null, 2),
  };
}
export function parseSnapshotDraft(draft: SnapshotDraft): AccountSnapshot {
  if (
    !/^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$/.test(draft.as_of)
  ) {
    throw new Error("快照时间须包含秒和时区，例如 2026-11-06T15:00:00+08:00。");
  }
  const mappings: Record<string, Record<string, number | string>> = {};
  for (const field of ["holdings", "protected_floor", "sellable", "cost_basis"] as const) {
    let value: unknown;
    try {
      value = JSON.parse(draft[field]);
    } catch {
      throw new Error(`${snapshotFields.find(([key]) => key === field)?.[1]}须为有效 JSON 对象。`);
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error(`${field} 须为标的到数值的 JSON 对象。`);
    }
    for (const [symbol, amount] of Object.entries(value)) {
      if (field === "cost_basis") {
        if (typeof amount !== "string") {
          throw new Error("成本基础须使用十进制字符串，不能使用 JSON 数字。");
        }
        if (!decimalString.test(amount)) {
          throw new Error(`成本基础 ${symbol} 须为有效的非负十进制字符串。`);
        }
      } else if (!Number.isSafeInteger(amount) || (amount as number) < 0) {
        throw new Error(`${field} 数量须为非负安全整数。`);
      }
    }
    mappings[field] = value as Record<string, number | string>;
  }
  if (!decimalString.test(draft.available_cash)) {
    throw new Error("可用现金须为有效的非负十进制字符串。");
  }
  return {
    as_of: draft.as_of,
    available_cash: draft.available_cash,
    holdings: mappings.holdings as Record<string, number>,
    protected_floor: mappings.protected_floor as Record<string, number>,
    sellable: mappings.sellable as Record<string, number>,
    cost_basis: mappings.cost_basis as Record<string, string>,
  };
}
