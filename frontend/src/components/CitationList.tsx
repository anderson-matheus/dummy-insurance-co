import { S } from "../strings";
import { Citation } from "../types";

interface Props {
  citations: Citation[];
  expanded: number | null;
  onToggle: (n: number) => void;
  compact?: boolean;
}

export function CitationList({ citations, expanded, onToggle, compact }: Props) {
  if (citations.length === 0) return null;
  return (
    <div className="citations">
      <div className="citations__title">{S.citations}</div>
      <ul>
        {citations.map((c) => {
          const open = expanded === c.ordinal;
          const isDb = c.source_type === "database";
          return (
            <li key={c.ordinal} className={open ? "citation citation--open" : "citation"}>
              <button type="button" className="citation__head" onClick={() => onToggle(c.ordinal)} aria-expanded={open}>
                <span className="chip">{c.ordinal}</span>
                <span className="citation__name">{isDb ? S.databaseSource : c.doc_title ?? c.doc_code}</span>
                <span className="citation__meta">
                  {isDb ? (
                    <>{c.row_count != null ? S.rows(c.row_count) : ""}</>
                  ) : (
                    <>
                      {c.doc_code}
                      {c.doc_version ? ` · ${S.citationVersion(c.doc_version)}` : ""}
                      {c.section ? ` · ${c.section}` : ""}
                      {c.page != null ? ` · ${S.page(c.page)}` : ""}
                    </>
                  )}
                </span>
                {c.doc_status === "superada" && <span className="badge badge--warn" title={S.supersededHint}>{S.superseded}</span>}
              </button>
              {open && !compact && (
                <div className="citation__panel">
                  {isDb ? (
                    <>
                      <pre className="sql">{c.sql}</pre>
                      <div className="muted small">
                        {S.readOnlyQuery}
                        {c.data_snapshot ? ` · ${c.data_snapshot}` : ""}
                      </div>
                      {c.snippet && <pre className="snippet">{c.snippet}</pre>}
                    </>
                  ) : (
                    <>
                      {c.doc_status === "superada" && <div className="small badge-note">{S.supersededHint}</div>}
                      {c.snippet && <blockquote className="snippet">{c.snippet}</blockquote>}
                    </>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
