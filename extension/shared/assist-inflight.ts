/**
 * The worker's list of one-shot ✦ requests still waiting for the server, so the page can stop one.
 *
 * A ✦ press is a single `POST /assist`. Without this the page had no way to stop it: its card could
 * only stop showing the wait, while the server kept working, kept the account's one drafting slot,
 * and could still charge for a draft nobody would see. Each request now carries its own
 * Idempotency-Key and AbortController. Stopping it aborts the fetch and tells the server to cancel
 * that exact key (`POST /requests/{key}/cancel`), which frees the slot.
 *
 * Keyed by tab and the page's own request id, so two tabs can never stop each other's work.
 */
export interface InflightAssist {
  key: string;
  controller: AbortController;
  /** The device token the request was sent with: the cancel must be sent as the same device. */
  token: string | null;
  cancelled: boolean;
}

export type SendCancel = (token: string | null, key: string) => void;

export class InflightAssists {
  private readonly entries = new Map<string, InflightAssist>();

  private static id(scope: string, requestId: string): string {
    return `${scope}\u0000${requestId}`;
  }

  start(scope: string, requestId: string, key: string, token: string | null): InflightAssist {
    const id = InflightAssists.id(scope, requestId);
    // A page reusing an id it already started stops the older request first: only one may run.
    this.entries.get(id)?.controller.abort();
    const entry: InflightAssist = { key, controller: new AbortController(), token, cancelled: false };
    this.entries.set(id, entry);
    return entry;
  }

  /** Stop one request and tell the server. False when there was nothing running under that id. */
  cancel(scope: string, requestId: string, sendCancel: SendCancel): boolean {
    const entry = this.entries.get(InflightAssists.id(scope, requestId));
    if (!entry || entry.cancelled) return false;
    entry.cancelled = true;
    entry.controller.abort();
    sendCancel(entry.token, entry.key);
    return true;
  }

  /** Stop everything a tab started (the tab closed or navigated away). */
  cancelScope(scope: string, sendCancel: SendCancel): number {
    let stopped = 0;
    for (const [id, entry] of this.entries) {
      if (!id.startsWith(`${scope}\u0000`) || entry.cancelled) continue;
      entry.cancelled = true;
      entry.controller.abort();
      sendCancel(entry.token, entry.key);
      stopped += 1;
    }
    return stopped;
  }

  /** The request finished one way or another; forget it (only if it is still the same entry). */
  finish(scope: string, requestId: string, entry: InflightAssist): void {
    const id = InflightAssists.id(scope, requestId);
    if (this.entries.get(id) === entry) this.entries.delete(id);
  }

  get size(): number {
    return this.entries.size;
  }
}
