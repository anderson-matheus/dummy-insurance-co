// Minimal Server-Sent Events parser for fetch() bodies (EventSource cannot POST).
export interface SseFrame {
  event: string;
  data: string;
  id?: string;
}

/** Parse complete frames out of `buffer`; returns the frames and the unparsed remainder. */
export function parseSseChunk(buffer: string): { frames: SseFrame[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const frames: SseFrame[] = [];
  let start = 0;
  while (true) {
    const end = normalized.indexOf("\n\n", start);
    if (end === -1) break;
    const block = normalized.slice(start, end);
    start = end + 2;
    let event = "message";
    let id: string | undefined;
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith(":") || line === "") continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
      else if (field === "id") id = value;
    }
    if (data.length > 0) frames.push({ event, data: data.join("\n"), id });
  }
  return { frames, rest: normalized.slice(start) };
}

/** Read an SSE body to completion, invoking `onFrame` for each frame. Resolves when the stream ends. */
export async function readSse(body: ReadableStream<Uint8Array>, onFrame: (frame: SseFrame) => void): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = parseSseChunk(buffer);
      buffer = rest;
      for (const frame of frames) onFrame(frame);
    }
    buffer += decoder.decode();
    const { frames } = parseSseChunk(buffer + "\n\n");
    for (const frame of frames) onFrame(frame);
  } finally {
    reader.releaseLock();
  }
}
