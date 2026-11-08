import type { AccountSnapshot } from "./a500-client";

const decimalString = /^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
export type SnapshotDraft = Record<keyof AccountSnapshot, string>;
export type PositionDraft = {
  symbol: string;
  holdings: string;
  protected_floor: string;
  sellable: string;
  cost_basis: string;
};
export const snapshotFields = [
  ["as_of", "快照时间"],
  ["holdings", "持有总份额"],
  ["protected_floor", "保护底仓"],
  ["sellable", "当前可卖"],
  ["cost_basis", "成本基础"],
  ["available_cash", "可用现金"],
] as const;
const positionFields = ["holdings", "protected_floor", "sellable", "cost_basis"] as const;

export function snapshotPositions(draft: SnapshotDraft): PositionDraft[] {
  const mappings = Object.fromEntries(
    positionFields.map((field) => [
      field,
      JSON.parse(draft[field]) as Record<string, number | string>,
    ]),
  ) as Record<(typeof positionFields)[number], Record<string, number | string>>;
  const symbols = new Set(positionFields.flatMap((field) => Object.keys(mappings[field])));
  return [...symbols].map((symbol) => ({
    symbol,
    holdings: String(mappings.holdings[symbol] ?? 0),
    protected_floor: String(mappings.protected_floor[symbol] ?? 0),
    sellable: String(mappings.sellable[symbol] ?? 0),
    cost_basis: String(mappings.cost_basis[symbol] ?? ""),
  }));
}

export function snapshotDraftWithPositions(
  draft: SnapshotDraft,
  positions: PositionDraft[],
): SnapshotDraft {
  const mappings = Object.fromEntries(positionFields.map((field) => [field, {}])) as Record<
    (typeof positionFields)[number],
    Record<string, number | string>
  >;
  for (const position of positions) {
    const symbol = position.symbol.trim();
    if (!symbol) continue;
    for (const field of positionFields) {
      const value = position[field].trim();
      if (field === "cost_basis") {
        if (value) mappings[field][symbol] = value;
      } else {
        const number = Number(value);
        mappings[field][symbol] =
          value !== "" && Number.isSafeInteger(number) && number >= 0 ? number : value;
      }
    }
  }
  return {
    ...draft,
    holdings: JSON.stringify(mappings.holdings, null, 2),
    protected_floor: JSON.stringify(mappings.protected_floor, null, 2),
    sellable: JSON.stringify(mappings.sellable, null, 2),
    cost_basis: JSON.stringify(mappings.cost_basis, null, 2),
  };
}

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
