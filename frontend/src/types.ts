export type MessageStatus = "pending" | "complete" | "refused" | "error" | "cancelled" | "interrupted";

export interface ErrorInfo {
  code: string;
  message: string;
  retryable: boolean;
  retry_after_s?: number | null;
}

export interface Citation {
  ordinal: number;
  source_type: "document" | "database";
  doc_code?: string | null;
  doc_title?: string | null;
  doc_version?: string | null;
  doc_status?: string | null;
  section?: string | null;
  page?: number | null;
  snippet?: string | null;
  sql?: string | null;
  row_count?: number | null;
  data_snapshot?: string | null;
}

export interface Metrics {
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  latency_ms: number;
  llm_calls: number;
  retries: number;
  tool_calls: number;
  pii_hits: number;
}

export interface Provenance {
  documents_cited: { doc_code: string; doc_title?: string | null; version?: string | null; status?: string | null }[];
  database_queried: boolean;
  database_snapshot?: string | null;
  index_built_at?: string | null;
  sources_checked: string[];
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: MessageStatus;
  attempt: number;
  client_message_id?: string | null;
  error?: ErrorInfo | null;
  refusal_code?: string | null;
  citations: Citation[];
  metrics?: Metrics | null;
  provenance?: Provenance | null;
  created_at: string;
  completed_at?: string | null;
  /** client-only: reason for an interruption detected in the browser */
  interruptionReason?: string;
}

export interface Conversation {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface SourceSummary {
  n: number;
  source_type: "document" | "database";
  label: string;
  doc_code?: string;
  doc_title?: string;
  doc_version?: string;
  doc_status?: string;
  effective_date?: string;
  section?: string;
  page?: number;
  sql?: string;
  row_count?: number;
  data_snapshot?: string;
}

export interface Health {
  status: "ok" | "degraded";
  provider: { circuit: "closed" | "open" | "half_open"; reason: string | null; open_for_s: number; configured: boolean };
  index: { built_at: string | null; documents: number; chunks: number };
  claims_db: { snapshot_mtime?: string; latest_payment_date?: string };
}

export class ApiError extends Error {
  code: string;
  retryable: boolean;
  retryAfterS?: number | null;
  correlationId?: string;
  status: number;
  constructor(status: number, code: string, message: string, retryable: boolean, correlationId?: string, retryAfterS?: number | null) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.correlationId = correlationId;
    this.retryAfterS = retryAfterS;
  }
}

export type SseEvent =
  | { event: "accepted"; data: { user_message_id: string; assistant_message_id: string; deduplicated?: boolean; attempt?: number } }
  | { event: "stage"; data: { stage: string } }
  | { event: "sources"; data: { sources: SourceSummary[] } }
  | { event: "delta"; data: { text: string } }
  | { event: "reset"; data: Record<string, never> }
  | { event: "done"; data: { message: Message } }
  | { event: "error"; data: ErrorInfo & { message: Message } };
