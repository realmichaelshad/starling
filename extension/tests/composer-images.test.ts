/**
 * Create and Enhance with photos: the images must fit the one request body the gateway accepts
 * (256 KiB). Two phone photos at 1024 px / quality 0.7 did not, and the card said "Remove an
 * attachment or shorten your message". The canvas itself only exists in a browser, so the encoder is
 * a stand-in here whose output size depends on the step, the way a real JPEG's does.
 */
import { describe, expect, it } from "vitest";
import {
  IMAGE_ENCODE_STEPS,
  IMAGE_MAX_CHARS,
  REQUEST_BODY_MAX_BYTES,
  droppedImagesNote,
  encodeWithinBudget,
  fitImagesToRequest,
  fitWithin,
  jsonBytes,
  type EncodeStep,
} from "../shared/composer-images";

/** A data URL of exactly `chars` characters. */
function dataUrl(chars: number): string {
  const head = "data:image/jpeg;base64,";
  return head + "A".repeat(Math.max(0, chars - head.length));
}

/** A photo whose JPEG size grows with its area and its quality, like a real one. */
function photo(bytesAtFirstStep: number) {
  const first = IMAGE_ENCODE_STEPS[0];
  const calls: EncodeStep[] = [];
  const encode = (step: EncodeStep) => {
    calls.push(step);
    const area = (step.maxSide / first.maxSide) ** 2;
    return dataUrl(Math.round(bytesAtFirstStep * area * (step.quality / first.quality)));
  };
  return { encode, calls };
}

describe("each image is stepped down until it fits", () => {
  it("starts at 768 px and quality 0.6, and only gets smaller or rougher", () => {
    expect(IMAGE_ENCODE_STEPS[0]).toEqual({ maxSide: 768, quality: 0.6 });
    for (let index = 1; index < IMAGE_ENCODE_STEPS.length; index += 1) {
      const before = IMAGE_ENCODE_STEPS[index - 1];
      const step = IMAGE_ENCODE_STEPS[index];
      expect(step.maxSide <= before.maxSide && step.quality <= before.quality, JSON.stringify(step)).toBe(true);
      expect(step.maxSide < before.maxSide || step.quality < before.quality, JSON.stringify(step)).toBe(true);
    }
    expect(IMAGE_MAX_CHARS).toBe(90_000);
  });

  it("keeps the first step when it already fits", async () => {
    const small = photo(40_000);
    const url = await encodeWithinBudget(small.encode);
    expect(small.calls).toHaveLength(1);
    expect(url!.length).toBeLessThanOrEqual(IMAGE_MAX_CHARS);
  });

  it("steps down a large photo until it is under about 90 KB", async () => {
    const large = photo(260_000);
    const url = await encodeWithinBudget(large.encode);
    expect(url).not.toBeNull();
    expect(url!.length).toBeLessThanOrEqual(IMAGE_MAX_CHARS);
    expect(large.calls.length).toBeGreaterThan(1);
    expect(large.calls[0]).toEqual({ maxSide: 768, quality: 0.6 });
  });

  it("gives up (and the request goes without it) when even the smallest step is too big", async () => {
    const huge = photo(10_000_000);
    expect(await encodeWithinBudget(huge.encode)).toBeNull();
    expect(huge.calls).toHaveLength(IMAGE_ENCODE_STEPS.length);
  });

  it("treats a failing canvas as no image, never as an error", async () => {
    expect(await encodeWithinBudget(() => { throw new Error("tainted canvas"); })).toBeNull();
    expect(await encodeWithinBudget(() => null)).toBeNull();
  });

  it("never scales an image up, and keeps its shape", () => {
    expect(fitWithin(4032, 3024, 768)).toEqual({ width: 768, height: 576 });
    expect(fitWithin(3024, 4032, 768)).toEqual({ width: 576, height: 768 });
    expect(fitWithin(400, 300, 768)).toEqual({ width: 400, height: 300 });
    expect(fitWithin(10_000, 1, 256)).toEqual({ width: 256, height: 1 });
  });
});

describe("the whole request stays under the gateway's limit", () => {
  const build = (images: string[]) => ({
    mode: "enhance_composer",
    composer: { action: "post", draft_text: "Shipping day.", intent: "enhance", ...(images.length ? { images } : {}) },
  });

  it("keeps every image that fits", () => {
    const fitted = fitImagesToRequest([dataUrl(90_000), dataUrl(90_000)], build);
    expect(fitted.dropped).toBe(0);
    expect(fitted.images).toHaveLength(2);
    expect(jsonBytes(fitted.body)).toBeLessThanOrEqual(REQUEST_BODY_MAX_BYTES);
    expect(REQUEST_BODY_MAX_BYTES).toBeLessThan(256 * 1024);
  });

  it("leaves out the later images rather than failing the request", () => {
    const fitted = fitImagesToRequest([dataUrl(150_000), dataUrl(150_000)], build);
    expect(fitted.images).toHaveLength(1);
    expect(fitted.dropped).toBe(1);
    expect(jsonBytes(fitted.body)).toBeLessThanOrEqual(REQUEST_BODY_MAX_BYTES);
  });

  it("sends the text alone when no image fits, with no empty images list", () => {
    const fitted = fitImagesToRequest([dataUrl(300_000)], build);
    expect(fitted.images).toEqual([]);
    expect(fitted.dropped).toBe(1);
    expect(fitted.body.composer).not.toHaveProperty("images");
  });

  it("counts the bytes as they are sent", () => {
    expect(jsonBytes({ text: "é" })).toBe(new TextEncoder().encode('{"text":"é"}').length);
  });

  it("says on the card when images were left out", () => {
    expect(droppedImagesNote(0, 2)).toBeNull();
    expect(droppedImagesNote(1, 1)).toMatch(/without the image/);
    expect(droppedImagesNote(2, 2)).toMatch(/without the images/);
    expect(droppedImagesNote(1, 2)).toMatch(/one of the images/);
  });
});
