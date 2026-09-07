import { describe, expect, it } from "vitest";
import { Action, ChatState, initialState, reducer } from "../chatReducer";
import { Message } from "../types";

function apply(actions: Action[], start: ChatState = { ...initialState, conversationId: "c1", historyStatus: "ready" }) {
  return actions.reduce(reducer, start);
}

const done: Message = { id: "a1", role: "assistant", content: "12 meses [1].", status: "complete", attempt: 1, citations: [], created_at: "" };

describe("chat reducer", () => {
  it("sends, streams deltas, resets and completes", () => {
    let s = apply([{ type: "SEND_START", content: "vigência?", clientMessageId: "k1", isRetry: false, now: 0 }]);
    expect(s.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(s.phase.kind).toBe("sending");
    s = apply([{ type: "ACCEPTED", userMessageId: "u1", assistantMessageId: "a1" }, { type: "STAGE", stage: "generating" }, { type: "DELTA", text: "12 " }, { type: "DELTA", text: "meses" }], s);
    expect(s.messages[1].id).toBe("a1");
    expect(s.messages[1].content).toBe("12 meses");
    expect(s.phase.kind === "streaming" && s.phase.stage).toBe("generating");
    s = apply([{ type: "RESET_TEXT" }, { type: "DELTA", text: "Doze" }, { type: "DONE", message: done }], s);
    expect(s.messages[1]).toEqual(done);
    expect(s.phase.kind).toBe("idle");
  });

  it("retry with the same client_message_id does not duplicate messages", () => {
    const failed: Message = { ...done, status: "error", content: "parcial", error: { code: "LLM_TIMEOUT", message: "x", retryable: true } };
    let s = apply([
      { type: "SEND_START", content: "vigência?", clientMessageId: "k1", isRetry: false, now: 0 },
      { type: "ACCEPTED", userMessageId: "u1", assistantMessageId: "a1" },
      { type: "STREAM_ERROR", message: failed },
    ]);
    expect(s.messages[1].status).toBe("error");
    s = apply([{ type: "SEND_START", content: "vigência?", clientMessageId: "k1", isRetry: true, now: 1 }], s);
    expect(s.messages).toHaveLength(2);
    expect(s.messages[1].status).toBe("pending");
    expect(s.messages[1].content).toBe("");
    expect(s.messages[1].error).toBeNull();
    s = apply([{ type: "ACCEPTED", userMessageId: "u1", assistantMessageId: "a1", attempt: 2 }, { type: "DONE", message: { ...done, attempt: 2 } }], s);
    expect(s.messages).toHaveLength(2);
    expect(s.messages[1].attempt).toBe(2);
  });

  it("cancel and interruption keep the partial text and offer retry", () => {
    const s = apply([
      { type: "SEND_START", content: "q", clientMessageId: "k2", isRetry: false, now: 0 },
      { type: "ACCEPTED", userMessageId: "u2", assistantMessageId: "a2" },
      { type: "DELTA", text: "parte da resposta" },
      { type: "CANCELLED" },
    ]);
    expect(s.messages[1].status).toBe("cancelled");
    expect(s.messages[1].content).toBe("parte da resposta");
    expect(s.messages[1].error?.retryable).toBe(true);
    const t = apply([{ type: "SEND_START", content: "q", clientMessageId: "k3", isRetry: false, now: 0 }, { type: "INTERRUPTED", reason: "network" }]);
    expect(t.messages[1].status).toBe("interrupted");
    expect(t.messages[1].interruptionReason).toBe("network");
    expect(t.phase.kind).toBe("idle");
  });

  it("ignores a second send while busy", () => {
    const s = apply([
      { type: "SEND_START", content: "a", clientMessageId: "k1", isRetry: false, now: 0 },
      { type: "SEND_START", content: "b", clientMessageId: "k2", isRetry: false, now: 0 },
    ]);
    expect(s.messages).toHaveLength(2);
  });
});
