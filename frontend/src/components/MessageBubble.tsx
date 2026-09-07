import { Fragment, useEffect, useState } from "react";
import { Phase } from "../chatReducer";
import { fmtElapsed } from "../format";
import { S } from "../strings";
import { Citation, Message, SourceSummary } from "../types";
import { CitationList } from "./CitationList";
import { ProvenanceFooter } from "./ProvenanceFooter";

interface Props {
  message: Message;
  active: boolean;
  phase: Phase;
  now: number;
  onRetry: (m: Message) => void;
  onCancel: () => void;
}

const MARKER = /\[(\d+)\]/g;

function renderWithChips(content: string, lookup: (n: number) => boolean, onChip: (n: number) => void) {
  const parts: (string | number)[] = [];
  let last = 0;
  for (const m of content.matchAll(MARKER)) {
    parts.push(content.slice(last, m.index));
    parts.push(Number(m[1]));
    last = (m.index ?? 0) + m[0].length;
  }
  parts.push(content.slice(last));
  return parts.map((p, i) =>
    typeof p === "number" ? (
      <button key={i} type="button" className={lookup(p) ? "chip" : "chip chip--unknown"} onClick={() => onChip(p)} title={S.citations}>
        {p}
      </button>
    ) : (
      <Fragment key={i}>{p}</Fragment>
    ),
  );
}

function sourcesAsCitations(sources: SourceSummary[]): Citation[] {
  return sources.map((s) => ({
    ordinal: s.n,
    source_type: s.source_type,
    doc_code: s.doc_code,
    doc_title: s.doc_title,
    doc_version: s.doc_version,
    doc_status: s.doc_status,
    section: s.section,
    page: s.page,
    sql: s.sql,
    row_count: s.row_count,
    data_snapshot: s.data_snapshot,
  }));
}

function RetryButton({ message, onRetry, now }: { message: Message; onRetry: (m: Message) => void; now: number }) {
  const [retryAt] = useState(() => (message.error?.retry_after_s ? Date.now() + message.error.retry_after_s * 1000 : 0));
  const wait = Math.ceil((retryAt - now) / 1000);
  const retryable = message.error?.retryable ?? true;
  if (!retryable) {
    return <button className="btn" disabled title={S.retryDisabled}>{S.retry}</button>;
  }
  if (wait > 0) {
    return <button className="btn" disabled>{S.retryIn(wait)}</button>;
  }
  return <button className="btn" onClick={() => onRetry(message)}>{S.retry}</button>;
}

export function MessageBubble({ message: m, active, phase, now, onRetry, onCancel }: Props) {
  const [expanded, setExpanded] = useState<number | null>(null);
  useEffect(() => setExpanded(null), [m.status]);

  if (m.role === "user") {
    return <div className="msg msg--user"><div className="msg__body">{m.content}</div></div>;
  }

  const streamingSources = active && phase.kind === "streaming" ? phase.sources : [];
  const citations = m.citations.length > 0 ? m.citations : sourcesAsCitations(streamingSources);
  const has = (n: number) => citations.some((c) => c.ordinal === n);
  const toggle = (n: number) => setExpanded((cur) => (cur === n ? null : n));
  const body = renderWithChips(m.content, has, toggle);

  if (m.status === "pending") {
    if (!active) {
      return <div className="msg msg--assistant msg--pending"><div className="msg__body"><span className="spinner" /> {S.pendingTitle}</div></div>;
    }
    const startedAt = phase.kind === "idle" ? now : phase.startedAt;
    const elapsed = now - startedAt;
    const stage = phase.kind === "streaming" ? phase.stage : "waiting";
    return (
      <div className="msg msg--assistant msg--streaming" aria-live="polite">
        {m.content && <div className="msg__body">{body}<span className="cursor">▍</span></div>}
        <div className="status-line">
          <span className="spinner" />
          <span>{S.stages[stage] ?? S.stages.waiting}</span>
          <span className="muted">{fmtElapsed(elapsed)}</span>
          <button className="btn btn--small" onClick={onCancel}>{S.cancel}</button>
        </div>
        {elapsed > 8000 && <div className="muted small">{S.slowHint}</div>}
        {citations.length > 0 && <CitationList citations={citations} expanded={expanded} onToggle={toggle} compact />}
      </div>
    );
  }

  if (m.status === "complete") {
    return (
      <div className="msg msg--assistant">
        <div className="msg__body">{body}</div>
        <CitationList citations={citations} expanded={expanded} onToggle={toggle} />
        <ProvenanceFooter message={m} />
      </div>
    );
  }

  if (m.status === "refused") {
    const title = m.refusal_code === "PII_REQUEST" ? S.refusalPii : m.refusal_code === "PROVIDER_REFUSAL" ? S.refusalProvider : S.refusalTitle;
    const checked = m.provenance?.sources_checked ?? [];
    return (
      <div className="msg msg--assistant card card--refused">
        <div className="card__title">⚠ {title}</div>
        <div className="msg__body">{m.content}</div>
        <div className="small">
          <strong>{S.sourcesChecked}</strong> {checked.length > 0 ? checked.join(" · ") : S.noSourcesChecked}
        </div>
        <div className="muted small">{S.refusalHint}</div>
        <ProvenanceFooter message={m} />
      </div>
    );
  }

  if (m.status === "cancelled" || m.status === "interrupted") {
    const reasonKey = m.interruptionReason ?? m.error?.code ?? "";
    const reason = S.interruptedReasons[reasonKey];
    return (
      <div className="msg msg--assistant card card--muted">
        <div className="card__title">{m.status === "cancelled" ? S.cancelledTitle : S.interruptedTitle}</div>
        {reason && <div className="small">{reason}</div>}
        {m.content && <div className="msg__body msg__body--partial">{body}</div>}
        <div className="card__actions"><RetryButton message={m} onRetry={onRetry} now={now} /></div>
      </div>
    );
  }

  // status === "error"
  const text = m.error?.message || S.errorFallback[m.error?.code ?? ""] || S.errorFallback.INTERNAL;
  return (
    <div className="msg msg--assistant card card--error" role="alert">
      <div className="card__title">✕ {S.errorTitle}</div>
      <div className="msg__body">{text}</div>
      {m.content && (
        <div className="small">
          <div className="muted">{S.partialLabel}</div>
          <div className="msg__body msg__body--partial">{body}</div>
        </div>
      )}
      <div className="mono muted small">{S.errorCode(m.error?.code ?? "INTERNAL")}</div>
      <div className="card__actions"><RetryButton message={m} onRetry={onRetry} now={now} /></div>
    </div>
  );
}
