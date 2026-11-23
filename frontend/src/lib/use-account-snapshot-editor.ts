import { useEffect, useRef, useState } from "react";

import { ApiError, getSnapshot, saveSnapshot, type SnapshotResult } from "@/lib/a500-client";
import {
  parseSnapshotDraft,
  snapshotDraft,
  snapshotDraftWithPositions,
  snapshotPositions,
  type PositionDraft,
  type SnapshotDraft,
} from "@/lib/snapshot-draft";

export type AccountSnapshotEditorState = {
  snapshot: SnapshotResult | null;
  known: boolean;
  error: string | null;
  busy: boolean;
  draft: SnapshotDraft;
  positions: PositionDraft[];
  dirty: boolean;
  editing: boolean;
  hasLatest: boolean;
  conflict: boolean;
  notice: string | null;
  confirmDiscard: boolean;
};

type SnapshotEditorDependencies = {
  token: string | null;
  onUnauthorized: () => void;
  refreshNav: (token: string, signal?: AbortSignal) => Promise<void>;
};

export function useAccountSnapshotEditor({
  token,
  onUnauthorized,
  refreshNav,
}: SnapshotEditorDependencies) {
  const [snapshot, setSnapshot] = useState<SnapshotResult | null>(null);
  const [known, setKnown] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<SnapshotDraft>(snapshotDraft());
  const [positions, setPositions] = useState<PositionDraft[]>([]);
  const [dirty, setDirty] = useState(false);
  const [editing, setEditing] = useState(false);
  const [latest, setLatest] = useState<SnapshotResult | null | undefined>(undefined);
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const operationInProgress = useRef(false);
  const requestNumber = useRef(0);

  function replaceDraft(value: SnapshotDraft) {
    setDraft(value);
    setPositions(snapshotPositions(value));
  }

  function updatePosition(index: number, field: keyof PositionDraft, value: string) {
    const next = positions.map((position, row) =>
      row === index ? { ...position, [field]: value } : position,
    );
    setPositions(next);
    setDraft((current) => snapshotDraftWithPositions(current, next));
    setDirty(true);
    setError(null);
  }

  function changePositions(next: PositionDraft[]) {
    setPositions(next);
    setDraft((current) => snapshotDraftWithPositions(current, next));
    setDirty(true);
    setError(null);
  }
  function addPosition() {
    changePositions([
      ...positions,
      {
        symbol: "",
        holdings: "0",
        protected_floor: "0",
        sellable: "0",
        cost_basis: "",
      },
    ]);
  }

  function removePosition(index: number) {
    changePositions(positions.filter((_, row) => row !== index));
  }

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    const load = async () => {
      await Promise.resolve();
      if (controller.signal.aborted) return;
      const request = ++requestNumber.current;
      try {
        const result = await getSnapshot(token, controller.signal);
        if (controller.signal.aborted || request !== requestNumber.current) return;
        setSnapshot(result);
        setKnown(true);
        replaceDraft(snapshotDraft(result?.snapshot));
      } catch (reason) {
        if (controller.signal.aborted || request !== requestNumber.current) return;
        if (reason instanceof ApiError && reason.status === 401) onUnauthorized();
        setError(reason instanceof Error ? reason.message : "读取账户快照失败。");
      } finally {
        if (!controller.signal.aborted) setKnown(true);
      }
    };
    void load();
    return () => controller.abort();
  }, [token, onUnauthorized, refreshNav]);

  function startEditing() {
    setEditing(true);
    setError(null);
  }

  function discardDraft() {
    const source = latest !== undefined ? latest : snapshot;
    setSnapshot(source);
    replaceDraft(snapshotDraft(source?.snapshot));
    setDirty(false);
    setEditing(false);
    setLatest(undefined);
    setConflict(false);
    setConfirmDiscard(false);
    setError(null);
  }

  function requestDiscard() {
    if (dirty) setConfirmDiscard(true);
    else discardDraft();
  }
  function continueEditing() {
    setConfirmDiscard(false);
  }

  async function reload() {
    if (!token || operationInProgress.current) return;
    operationInProgress.current = true;
    const request = ++requestNumber.current;
    setBusy(true);
    setError(null);
    try {
      const result = await getSnapshot(token);
      if (request !== requestNumber.current) return;
      setLatest(result);
      if (!dirty) {
        setSnapshot(result);
        replaceDraft(snapshotDraft(result?.snapshot));
      }
      await refreshNav(token);
      setNotice("已读取服务端最新快照。当前草稿保持不变；请选择如何处理版本差异。");
    } catch (reason) {
      if (request !== requestNumber.current) return;
      if (reason instanceof ApiError && reason.status === 401) onUnauthorized();
      setError(reason instanceof Error ? reason.message : "读取最新快照失败。");
    } finally {
      operationInProgress.current = false;
      setBusy(false);
    }
  }

  async function save() {
    if (!token || !known || conflict || operationInProgress.current) return;
    operationInProgress.current = true;
    ++requestNumber.current;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (positions.some((position) => !position.symbol.trim())) {
        throw new Error("请为每行填写证券代码，或删除空白行。");
      }
      if (new Set(positions.map((position) => position.symbol.trim())).size !== positions.length) {
        throw new Error("证券代码不能重复。");
      }
      const payload = parseSnapshotDraft({
        ...draft,
        as_of: new Date().toISOString(),
      });
      const result = await saveSnapshot(token, payload, snapshot?.etag ?? null);
      setSnapshot(result);
      replaceDraft(snapshotDraft(result.snapshot));
      setDirty(false);
      setEditing(false);
      setLatest(undefined);
      setConflict(false);
      setNotice("账户快照已保存。正在从服务端重新读取完整净资产估值。");
      await refreshNav(token);
    } catch (reason) {
      if (reason instanceof ApiError && (reason.status === 412 || reason.status === 428)) {
        setConflict(true);
        setError("快照版本已变化，草稿仍保留，未覆盖服务端数据。请读取最新快照后明确选择处理方式。");
      } else {
        if (reason instanceof ApiError && reason.status === 401) onUnauthorized();
        const message = reason instanceof Error ? reason.message : "保存快照失败";
        setError(`${message}。输入内容已保留。`);
      }
    } finally {
      operationInProgress.current = false;
      setBusy(false);
    }
  }

  function setField<K extends keyof SnapshotDraft>(field: K, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
    setDirty(true);
    setError(null);
  }

  function replaceWithLatest() {
    setSnapshot(latest ?? null);
    replaceDraft(snapshotDraft(latest?.snapshot));
    setDirty(false);
    setEditing(false);
    setConflict(false);
    setLatest(undefined);
    setError(null);
  }

  function keepDraftOnLatest() {
    if (latest === undefined) return;
    setSnapshot(latest);
    setConflict(false);
    setLatest(undefined);
    setError(null);
    setNotice("草稿已保留；下次保存将以最新快照版本为条件进行完整替换。");
  }

  return {
    state: {
      snapshot,
      known,
      error,
      busy,
      draft,
      positions,
      dirty,
      editing,
      hasLatest: latest !== undefined,
      conflict,
      notice,
      confirmDiscard,
    } satisfies AccountSnapshotEditorState,
    actions: {
      startEditing,
      requestDiscard,
      discardDraft,
      reload,
      save,
      setField,
      updatePosition,
      addPosition,
      removePosition,
      continueEditing,
      replaceWithLatest,
      keepDraftOnLatest,
    },
  };
}
