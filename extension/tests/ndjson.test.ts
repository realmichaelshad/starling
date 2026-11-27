import { describe, expect, it } from "vitest";
import { MAX_NDJSON_LINE_CHARS, NdjsonError, createLineSplitter, parseNdjsonLine, readNdjson } from "../shared/ndjson";

const encode = (text: string) => new TextEncoder().encode(text);

/** Bytes as they come off the network, decoded the way the transport decodes them. */
function decoded(chunks: Uint8Array<ArrayBuffer>[]): ReadableStreamDefaultReader<string> {
  return new ReadableStream<BufferSource>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  }).pipeThrough(new TextDecoderStream()).getReader();
}

async function collect(chunks: Uint8Array<ArrayBuffer>[], maxLineChars?: number): Promise<unknown[]> {
  const values: unknown[] = [];
  await readNdjson(decoded(chunks), value => values.push(value), maxLineChars);
  return values;
}

describe("NDJSON line splitting", () => {
  it("joins a line split across chunks and splits several lines in one chunk", () => {
    const splitter = createLineSplitter();
    expect(splitter.push('{"event":"tok')).toEqual([]);
    expect(splitter.push('en","text":"hi"}\n{"event":')).toEqual(['{"event":"token","text":"hi"}']);
    expect(splitter.push('"heartbeat"}\n{"event":"status","stage":"x"}\n')).toEqual([
      '{"event":"heartbeat"}',
      '{"event":"status","stage":"x"}',
    ]);
    expect(splitter.flush()).toEqual([]);
  });

  it("accepts CRLF line endings and skips blank lines", () => {
    const splitter = createLineSplitter();
    expect(splitter.push('{"a":1}\r\n\r\n   \n{"b":2}\r\n')).toEqual(['{"a":1}', '{"b":2}']);
  });

  it("keeps multibyte characters that are split across network chunks", async () => {
    const bytes = encode('{"event":"token","text":"café ✦"}\n');
    const insideE = bytes.indexOf(0xc3) + 1;
    const insideStar = bytes.indexOf(0xe2) + 2;
    const values = await collect([bytes.slice(0, insideE), bytes.slice(insideE, insideStar), bytes.slice(insideStar)]);
    expect(values).toEqual([{ event: "token", text: "café ✦" }]);
  });

  it("refuses a line longer than the limit, complete or still arriving", () => {
    expect(() => createLineSplitter(16).push(`${"x".repeat(17)}\n`)).toThrowError(NdjsonError);
    try {
      createLineSplitter(16).push("y".repeat(17));
      expect.unreachable("an unterminated oversize line must throw");
    } catch (error) {
      expect(error).toBeInstanceOf(NdjsonError);
      expect((error as NdjsonError).code).toBe("line_too_large");
    }
    expect(MAX_NDJSON_LINE_CHARS).toBeGreaterThanOrEqual(1024 * 1024);
  });

  it("delivers a final line that arrived without its newline", async () => {
    expect(await collect([encode('{"a":1}\n{"b":2}')])).toEqual([{ a: 1 }, { b: 2 }]);
    const splitter = createLineSplitter();
    expect(splitter.push('{"c":3}\r')).toEqual([]);
    expect(splitter.flush()).toEqual(['{"c":3}']);
  });

  it("names invalid JSON instead of guessing", () => {
    try {
      parseNdjsonLine("not json");
      expect.unreachable("invalid JSON must throw");
    } catch (error) {
      expect((error as NdjsonError).code).toBe("invalid_json");
    }
  });
});
