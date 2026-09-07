import { describe, expect, it } from "vitest";
import { parseSseChunk } from "../sse";

describe("parseSseChunk", () => {
  it("parses complete frames and keeps the remainder", () => {
    const { frames, rest } = parseSseChunk('event: delta\ndata: {"text":"a"}\n\nevent: del');
    expect(frames).toEqual([{ event: "delta", data: '{"text":"a"}', id: undefined }]);
    expect(rest).toBe("event: del");
  });

  it("handles frames split across chunks, comments, ids and CRLF", () => {
    const first = parseSseChunk(": ping\r\n\r\nid: 1\r\nevent: stage\r\ndata: {\"sta");
    expect(first.frames).toEqual([]);
    const second = parseSseChunk(first.rest + 'ge":"generating"}\r\n\r\n');
    expect(second.frames).toEqual([{ event: "stage", data: '{"stage":"generating"}', id: "1" }]);
    expect(second.rest).toBe("");
  });

  it("joins multi-line data and defaults the event name", () => {
    const { frames } = parseSseChunk("data: line1\ndata: line2\n\n");
    expect(frames).toEqual([{ event: "message", data: "line1\nline2", id: undefined }]);
  });
});
