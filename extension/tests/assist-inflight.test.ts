import { describe, expect, it } from "vitest";
import { InflightAssists } from "../shared/assist-inflight";

describe("stopping a one-shot ✦ request", () => {
  it("aborts the fetch and tells the server to cancel that exact key, as the same device", () => {
    const inflight = new InflightAssists();
    const entry = inflight.start("tab-7", "op-1", "11111111-2222-4333-8444-555555555555", "token-a");
    const sent: Array<[string | null, string]> = [];
    expect(inflight.cancel("tab-7", "op-1", (token, key) => sent.push([token, key]))).toBe(true);
    expect(entry.controller.signal.aborted).toBe(true);
    expect(sent).toEqual([["token-a", "11111111-2222-4333-8444-555555555555"]]);
    // A second press on Cancel sends nothing more.
    expect(inflight.cancel("tab-7", "op-1", (token, key) => sent.push([token, key]))).toBe(false);
    expect(sent).toHaveLength(1);
  });

  it("never lets one tab stop another tab's request", () => {
    const inflight = new InflightAssists();
    const mine = inflight.start("tab-1", "op-1", "key-mine-0001", null);
    const sent: string[] = [];
    expect(inflight.cancel("tab-2", "op-1", (_token, key) => sent.push(key))).toBe(false);
    expect(mine.controller.signal.aborted).toBe(false);
    expect(sent).toEqual([]);
  });

  it("forgets a finished request, but not a newer one under the same id", () => {
    const inflight = new InflightAssists();
    const first = inflight.start("tab-1", "op-1", "key-one-00001", null);
    const second = inflight.start("tab-1", "op-1", "key-two-00002", null);
    expect(first.controller.signal.aborted).toBe(true);
    inflight.finish("tab-1", "op-1", first);
    expect(inflight.size).toBe(1);
    inflight.finish("tab-1", "op-1", second);
    expect(inflight.size).toBe(0);
    expect(inflight.cancel("tab-1", "op-1", () => undefined)).toBe(false);
  });

  it("stops everything a closed tab left running", () => {
    const inflight = new InflightAssists();
    inflight.start("tab-1", "op-1", "key-a-0000001", null);
    inflight.start("tab-1", "op-2", "key-b-0000002", null);
    const other = inflight.start("tab-2", "op-1", "key-c-0000003", null);
    const sent: string[] = [];
    expect(inflight.cancelScope("tab-1", (_token, key) => sent.push(key))).toBe(2);
    expect(sent.sort()).toEqual(["key-a-0000001", "key-b-0000002"]);
    expect(other.controller.signal.aborted).toBe(false);
  });
});
