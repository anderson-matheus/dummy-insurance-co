import { fmtDate, fmtDateTime } from "../format";
import { S } from "../strings";
import { Message } from "../types";

export function ProvenanceFooter({ message: m }: { message: Message }) {
  const p = m.provenance;
  const metrics = m.metrics;
  if (!p && !metrics) return null;
  const parts: string[] = [];
  if (p) {
    const docs = p.documents_cited?.length ?? 0;
    if (docs > 0) parts.push(S.provenance.documents(docs));
    if (p.database_queried) parts.push(S.provenance.database);
    if (p.index_built_at) parts.push(S.provenance.index(fmtDateTime(p.index_built_at)));
    if (p.database_snapshot) parts.push(S.provenance.snapshot(p.database_snapshot));
  }
  if (metrics) {
    parts.push(S.provenance.latency(metrics.latency_ms));
    parts.push(S.provenance.tokens(metrics.input_tokens + metrics.output_tokens));
    parts.push(S.provenance.cost(metrics.cost_usd));
  }
  return (
    <div className="provenance muted small" title={m.completed_at ? fmtDate(m.completed_at) : undefined}>
      {parts.join(" · ")}
    </div>
  );
}
