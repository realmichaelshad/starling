/**
 * Attaching an approved image to X's composer (spec §8.3i).
 *
 * Two deliberate properties.
 *
 * 1. **x-action-guards is untouched.** Attaching happens by dispatching a
 *    paste event carrying a DataTransfer at the composer the user already
 *    opened — X's own media-upload controls are never clicked, so the
 *    `mediaUpload|addMedia` entries in FORBIDDEN stay in force. Nothing here
 *    weakens the click allowlist.
 *
 * 2. **The rights gate is re-checked here.** The engine already refuses to
 *    prepare a local copy for unknown-rights media, but this is the last step
 *    before the user's composer, so it refuses again rather than trusting that
 *    an earlier stage did its job.
 *
 * Publishing remains entirely manual. This puts an image and text in front of
 * the user; it never presses Post.
 */

/** Rights states the engine may mark as attachable (mirrors app/images.py). */
export const ATTACHABLE_RIGHTS = [
  "user_owned",
  "user_confirmed",
  "license_cleared",
  "public_domain",
  "attribution_required",
] as const;

export type RightsState = (typeof ATTACHABLE_RIGHTS)[number] | "unknown" | "blocked";

export interface ApprovedImage {
  /** Data URL or blob URL of the LOCAL prepared copy, never a remote URL. */
  dataUrl: string;
  mimeType: string;
  fileName: string;
  rights: RightsState;
  altText: string;
  sourceUrl?: string | null;
  creator?: string | null;
  requiredCredit?: string | null;
}

export interface AttachRefusal {
  ok: false;
  reason: string;
}

export interface AttachApproval {
  ok: true;
  file: File;
}

/** A remote or non-image URL must never reach X's composer. */
export function isLocalImageCopy(url: string): boolean {
  return /^data:image\/(?:png|jpeg|webp|gif);base64,/i.test(url) || url.startsWith("blob:");
}

/**
 * Decide whether an image may reach the composer at all.
 *
 * Exported separately from the DOM work so the rule is unit-testable without a
 * browser, which is where a rights regression would otherwise hide.
 */
export function canAttach(image: Pick<ApprovedImage, "rights" | "dataUrl" | "altText">): AttachRefusal | { ok: true } {
  if (!(ATTACHABLE_RIGHTS as readonly string[]).includes(image.rights)) {
    return { ok: false, reason: `Rights are ${image.rights}; Starling will not copy this image.` };
  }
  if (!image.dataUrl || !isLocalImageCopy(image.dataUrl)) {
    return { ok: false, reason: "Only a locally prepared copy can be attached." };
  }
  if (!image.altText.trim()) {
    // Alt text is required by §8.3i, and an empty one is worse than none
    // because it looks described when it is not.
    return { ok: false, reason: "Add alt text before attaching." };
  }
  return { ok: true };
}

/** The four image types a prepared copy may have, and the file extension each one gets. */
const ATTACHABLE_TYPES: Record<string, string> = { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif" };

/** Decode a base64 image data URL in place, without any network API. */
function dataUrlToBlob(dataUrl: string): Blob {
  const comma = dataUrl.indexOf(",");
  const mime = dataUrl.slice(5, comma).split(";")[0] || "image/png";
  const binary = atob(dataUrl.slice(comma + 1));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new Blob([bytes], { type: mime });
}

/** Turn the prepared local copy into a File the composer can accept. */
export async function toFile(image: ApprovedImage): Promise<AttachApproval | AttachRefusal> {
  const gate = canAttach(image);
  if (!gate.ok) return gate;
  // Content scripts never use fetch, so only a data URL (what the server sends)
  // can be turned into a file here.
  if (!image.dataUrl.startsWith("data:")) {
    return { ok: false, reason: "Only an image copy Starling prepared can be attached." };
  }
  try {
    const blob = dataUrlToBlob(image.dataUrl);
    // The type comes from the data URL, which `canAttach` has already held to four image types; the
    // `mimeType` and `fileName` fields are the service's say-so and were the unchecked part of what
    // reaches the user's composer ("text/html", "../invoice.html").
    const type = ATTACHABLE_TYPES[blob.type] ? blob.type : "image/png";
    const extension = ATTACHABLE_TYPES[type];
    const stem = (image.fileName ?? "").replace(/\.[A-Za-z0-9]{1,5}$/, "");
    const name = /^[A-Za-z0-9 _-]{1,64}$/.test(stem) ? `${stem}.${extension}` : `image.${extension}`;
    return { ok: true, file: new File([blob], name, { type }) };
  } catch (error) {
    return { ok: false, reason: `Could not read the prepared copy: ${String(error)}` };
  }
}

export interface AttachTarget {
  /** The contenteditable X is using for this draft. */
  editable: HTMLElement;
}

/**
 * Attach by paste, not by clicking X's upload button.
 *
 * X's composer accepts pasted image data, which is the same path a human uses
 * with Ctrl+V. That keeps the media-upload controls untouched and means no
 * change to the click allowlist.
 */
export async function attachToComposer(
  image: ApprovedImage,
  target: AttachTarget
): Promise<{ ok: boolean; reason?: string }> {
  const prepared = await toFile(image);
  if (!prepared.ok) return { ok: false, reason: prepared.reason };

  const transfer = new DataTransfer();
  transfer.items.add(prepared.file);

  target.editable.focus();
  const event = new ClipboardEvent("paste", {
    clipboardData: transfer,
    bubbles: true,
    cancelable: true,
  });
  // THE RETURN VALUE OF dispatchEvent IS NOT AN ATTACH SIGNAL, and reading it as
  // one had the verdict exactly backwards.
  //
  // Per the DOM spec it is false only when a handler called `preventDefault()` on
  // a cancelable event, and true otherwise — including when NOTHING handled the
  // event at all. X's composer is Draft.js, whose `editOnPaste` begins with
  // `e.preventDefault()` as its first statement, before it even looks at the
  // pasted files. So on the real composer a paste that DID land reported
  // "X refused the paste; attach the image manually.", while a page where no
  // listener existed reported success. Measured on both shapes: handler consumes
  // the file and preventDefaults -> ok:false; nothing handles it -> ok:true with
  // zero images in the composer. The synthetic event is untrusted, so the browser
  // performs no default paste of its own — "nobody touched it" and "it worked"
  // are indistinguishable from the dispatch alone.
  //
  // `recordImageChoice(..., "used")` keys off this ok, so the learning loop was
  // being told the image was rejected every time it was accepted.
  target.editable.dispatchEvent(event);
  // Two frames: X re-renders the composer and mounts the media tray in response.
  //
  // RACED AGAINST A TIMER, because requestAnimationFrame does not fire in a tab
  // that is not compositing. Chrome stops painting a backgrounded tab, so an
  // unconditional await on two frames never resolves there and this function
  // hangs forever — taking the caller's await with it and leaving the user on a
  // spinner with no error. Found by a harness that hung on exactly this.
  //
  // The frames are the fast path when the tab is visible (~32ms); the timer is
  // the guarantee that this returns at all. 250ms is far longer than two frames
  // and far shorter than a person's patience.
  await new Promise<void>(resolve => {
    let settled = false;
    const done = () => { if (!settled) { settled = true; resolve(); } };
    requestAnimationFrame(() => requestAnimationFrame(done));
    setTimeout(done, 250);
  });

  const scope = target.editable.closest<HTMLElement>('[role="dialog"], form') ?? document.body;
  const landed = scope.querySelector(
    '[data-testid="attachments"] img, [data-testid="attachments"] video, '
    + '[data-testid="media"] img, [aria-label*="remove" i][aria-label*="media" i]');
  if (landed) return { ok: true };

  // NOT a failure claim. Absence here means one of two things — the paste did not
  // land, or X changed the media tray's markup and this selector no longer finds
  // it — and this code cannot tell which. Asserting refusal is how the previous
  // version got it wrong, so say what is actually known and let the user look.
  return {
    ok: false,
    reason: "Pasted the image into your composer, but couldn't confirm it landed — check it before posting.",
  };
}

/**
 * The credit line, when the licence requires one (§8.3i step 4).
 * Returned as text for the user to keep or remove — never silently appended.
 */
export function creditLine(image: Pick<ApprovedImage, "requiredCredit" | "creator" | "sourceUrl">): string | null {
  if (image.requiredCredit) return image.requiredCredit;
  if (image.creator && image.sourceUrl) return `${image.creator} — ${image.sourceUrl}`;
  return null;
}
