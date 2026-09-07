import { readSse } from "./sse";
import { ApiError, Conversation, Health, Message, SseEvent } from "./types";

const BASE = "/api";

async function toApiError(res: Response): Promise<ApiError> {
  let code = "INTERNAL";
  let message = `Erro ${res.status}`;
  let retryable = res.status >= 500;
  let correlationId: string | undefined;
  let retryAfterS: number | null | undefined;
  try {
    const body = await res.json();
    if (body?.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      retryable = body.error.retryable ?? retryable;
      correlationId = body.error.correlation_id;
      retryAfterS = body.error.retry_after_s;
    }
  } catch {
    /* non-JSON body: keep defaults, never show raw internals */
  }
  return new ApiError(res.status, code, message, retryable, correlationId, retryAfterS);
}

async function json<T>(input: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + input, { headers: { "Content-Type": "application/json" }, ...init });
  } catch {
    throw new ApiError(0, "NETWORK", "Falha de rede ao contatar o servidor.", true);
  }
  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  health: () => json<Health>("/health"),
  listConversations: () => json<{ items: Conversation[] }>("/conversations").then((r) => r.items),
  createConversation: () => json<Conversation>("/conversations", { method: "POST", body: "{}" }),
  getConversation: (id: string) => json<Conversation & { messages: Message[] }>(`/conversations/${id}`),
  deleteConversation: (id: string) => json<void>(`/conversations/${id}`, { method: "DELETE" }),
  cancelMessage: (conversationId: string, assistantId: string) =>
    json<{ cancelled: boolean }>(`/conversations/${conversationId}/messages/${assistantId}/cancel`, { method: "POST", body: "{}" }),

  /** Stream an answer. Resolves when the stream ends (normally after `done` or `error`). */
  async streamMessage(
    conversationId: string,
    body: { content: string; client_message_id: string },
    onEvent: (ev: SseEvent) => void,
    signal: AbortSignal,
  ): Promise<void> {
    let res: Response;
    try {
      res = await fetch(`${BASE}/conversations/${conversationId}/messages/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
        signal,
      });
    } catch (err) {
      if ((err as Error).name === "AbortError") throw err;
      throw new ApiError(0, "NETWORK", "Falha de rede ao contatar o servidor.", true);
    }
    if (!res.ok || !res.body) throw await toApiError(res);
    await readSse(res.body, (frame) => {
      let data: unknown;
      try {
        data = JSON.parse(frame.data);
      } catch {
        return;
      }
      onEvent({ event: frame.event, data } as SseEvent);
    });
  },
};
