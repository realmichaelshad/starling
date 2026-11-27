/**
 * The images attached to X's composer, sized to fit the request they ride in.
 *
 * Create and Enhance send the composer's images to the vision model as JPEG data URLs inside the one
 * JSON body the gateway accepts: it refuses a body over 256 KiB with a 413, which the card can only
 * turn into "Remove an attachment or shorten your message". So each image is encoded at 768 px and
 * quality 0.6 first, then smaller and rougher, until it fits in about 90 KB, and the whole body is
 * kept under 240 KB. The images are context, not the request: one that still does not fit is left
 * out and the draft is written from the text alone.
 *
 * Pure: the canvas work is passed in as `encode`, so the stepping is unit-tested without a browser.
 */

/** One image as a data URL, at most (base64 is ASCII, so characters are bytes). */
export const IMAGE_MAX_CHARS = 90_000;
/** The whole request body, at most: under the gateway's 256 KiB with room to spare. */
export const REQUEST_BODY_MAX_BYTES = 240_000;
/** Read from one composer, at most. */
export const MAX_COMPOSER_IMAGES = 2;

export interface EncodeStep {
  /** The longer side, in pixels; a smaller image is never scaled up. */
  maxSide: number;
  /** JPEG quality, 0 to 1. */
  quality: number;
}

/** Largest and clearest first; each step is smaller or rougher than the one before. */
export const IMAGE_ENCODE_STEPS: readonly EncodeStep[] = [
  { maxSide: 768, quality: 0.6 },
  { maxSide: 768, quality: 0.5 },
  { maxSide: 640, quality: 0.5 },
  { maxSide: 512, quality: 0.45 },
  { maxSide: 384, quality: 0.4 },
  { maxSide: 256, quality: 0.35 },
];

/** Width and height with the longer side at most `maxSide`: never scaled up, never zero. */
export function fitWithin(width: number, height: number, maxSide: number): { width: number; height: number } {
  const scale = Math.min(1, maxSide / Math.max(width, height, 1));
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
}

/**
 * The first step whose encoding fits in `maxChars`, or null when even the last one does not (or the
 * encoder fails: a tainted canvas, an image that never loaded). An image is a bonus, never a blocker.
 */
export async function encodeWithinBudget(
  encode: (step: EncodeStep) => string | null | Promise<string | null>,
  maxChars = IMAGE_MAX_CHARS,
  steps: readonly EncodeStep[] = IMAGE_ENCODE_STEPS,
): Promise<string | null> {
  for (const step of steps) {
    let url: string | null;
    try {
      url = await encode(step);
    } catch {
      return null;
    }
    if (url && url.length <= maxChars) return url;
  }
  return null;
}

/** Bytes of a value as the worker will send it (JSON, UTF-8). */
export function jsonBytes(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).length;
}

export interface FittedRequest<T> {
  body: T;
  /** The images that went into `body`, in their original order. */
  images: string[];
  /** How many were left out so the body fits. */
  dropped: number;
}

/**
 * The request body with as many of the images as fit under `maxBytes`, keeping the first ones.
 * `build` makes the body from a list of images; with none it must leave the images out entirely.
 */
export function fitImagesToRequest<T>(
  images: readonly string[],
  build: (images: string[]) => T,
  maxBytes = REQUEST_BODY_MAX_BYTES,
): FittedRequest<T> {
  for (let count = images.length; count > 0; count -= 1) {
    const kept = images.slice(0, count);
    const body = build(kept);
    if (jsonBytes(body) <= maxBytes) return { body, images: kept, dropped: images.length - count };
  }
  return { body: build([]), images: [], dropped: images.length };
}

/** The card's note when images were left out, or null when every image went. */
export function droppedImagesNote(dropped: number, total: number): string | null {
  if (dropped <= 0 || total <= 0) return null;
  if (dropped >= total) {
    return total === 1
      ? "Sending to Starling without the image: it was too large to send…"
      : "Sending to Starling without the images: they were too large to send…";
  }
  return "Sending to Starling with one of the images: the other was too large to send…";
}
