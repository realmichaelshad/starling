/**
 * Pure insertion-point chooser for the Starling pill.
 *
 * Goal order, reading left→right with a draft present:
 *   [toolbar …]   [counter ring] [+] [✦ Starling pill] [Post]
 * i.e. the pill sits immediately left of Post and RIGHT of everything X
 * mounts between the toolbar and Post. Guessing X's DOM nesting broke this
 * repeatedly, so the choice is made from measured geometry over a serialized
 * ancestry snapshot: climb from Post toward the root and stop before any
 * ancestor whose subtree holds another visible same-row control positioned
 * left of Post (that ancestor owns the ring/+ group — inserting before it
 * would strand the pill on their left), or that is not a compact row.
 */
export interface RowRect {
  left: number;
  right: number;
  top: number;
  bottom: number;
  width: number;
  height: number;
}

export interface RowNode {
  id: number;
  parentId: number | null;
  /** button / [role=button] / [role=progressbar], visible, not Starling's own. */
  isControl: boolean;
  /** Non-control visible content (placeholder text, avatar, dividers) — the
   *  pill must stay to the RIGHT of these too. In the collapsed reply row
   *  there is no control between the avatar and Reply, and counting only
   *  controls stranded the pill at the row's left edge instead of hugging
   *  the Reply button. */
  isContent?: boolean;
  visible: boolean;
  rect: RowRect;
}

export interface InsertionPlan {
  /** Insert the pill host before this node… */
  beforeId: number;
  /** …inside this parent. */
  parentId: number;
}

function centerY(rect: RowRect): number {
  return rect.top + rect.height / 2;
}

export function isCompactRow(rowHeight: number, submitHeight: number): boolean {
  return rowHeight <= Math.max(72, submitHeight + 28);
}

function sameRowAs(rect: RowRect, submitRect: RowRect): boolean {
  return Math.abs(centerY(rect) - centerY(submitRect)) < submitRect.height;
}

function childrenOf(nodes: readonly RowNode[], parentId: number): RowNode[] {
  return nodes.filter(node => node.parentId === parentId);
}

function subtreeIds(nodes: readonly RowNode[], rootId: number): Set<number> {
  const ids = new Set<number>([rootId]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const node of nodes) {
      if (node.parentId != null && ids.has(node.parentId) && !ids.has(node.id)) {
        ids.add(node.id);
        grew = true;
      }
    }
  }
  return ids;
}

/** True when root's subtree holds visible content, other than the submit
 *  button itself, sitting on the submit's row to its LEFT — the counter ring,
 *  the "+" thread button, toolbar icons, and equally the "Post your reply"
 *  placeholder or avatar in a collapsed row. The pill belongs to the RIGHT of
 *  all of it, immediately beside the submit. */
export function subtreeContainsForeignLeft(
  nodes: readonly RowNode[],
  rootId: number,
  submitId: number
): boolean {
  const submit = nodes.find(node => node.id === submitId);
  if (!submit) return false;
  const ids = subtreeIds(nodes, rootId);
  return nodes.some(node =>
    node.id !== submitId
    && ids.has(node.id)
    && (node.isControl || node.isContent)
    && node.visible
    && sameRowAs(node.rect, submit.rect)
    && node.rect.left < submit.rect.left
    // A LEFT neighbor ends before the submit starts; anything spanning under
    // or past it is layout chrome (overlays, stretched wrappers), not content
    // the pill needs to clear.
    && node.rect.right <= submit.rect.left + 4);
}

export function chooseInsertionBeforeSubmit(
  nodes: readonly RowNode[],
  submitId: number,
  maxClimb = 8
): InsertionPlan | null {
  const byId = new Map(nodes.map(node => [node.id, node]));
  const submit = byId.get(submitId);
  if (!submit) return null;

  // Compact ancestor chain, deepest first. A non-compact ancestor means we've
  // left the action row — the pill must never mount inside a tall container
  // (X clips it to slivers there), so the chain stops before it.
  const chain: RowNode[] = [];
  let current = submit;
  for (let depth = 0; depth < maxClimb; depth += 1) {
    const parent = current.parentId != null ? byId.get(current.parentId) : undefined;
    if (!parent || !isCompactRow(parent.rect.height, submit.rect.height)) break;
    chain.push(parent);
    current = parent;
  }
  if (!chain.length) return null;

  // The insertion parent is the DEEPEST ancestor whose subtree already holds
  // visible content left of the submit on its row (ring/+, toolbar icons, the
  // collapsed row's placeholder text…). Inserting just inside that owner, on
  // the submit's branch, lands the pill right of everything native and
  // immediately left of the submit — in every observed shape. With no such
  // owner, the outermost compact ancestor (the action row itself) hosts it.
  const owner = chain.find(ancestor => subtreeContainsForeignLeft(nodes, ancestor.id, submitId))
    ?? chain[chain.length - 1];

  let child = submit;
  while (child.parentId != null && child.parentId !== owner.id) {
    const parent = byId.get(child.parentId);
    if (!parent) return null;
    child = parent;
  }
  if (child.parentId !== owner.id) return null;
  return { beforeId: child.id, parentId: owner.id };
}

/** Minimum horizontal breathing room between adjacent visible controls on the
 *  submit's row (icons, ring, +, the submit itself), ignoring stacked or
 *  overlapping pairs. Null when fewer than two controls share the row. The
 *  caller uses this to collapse the pill to icon-only when its width is what
 *  squeezes X's own controls together. */
export function rowControlBreathingRoom(nodes: readonly RowNode[], submitId: number): number | null {
  const submit = nodes.find(node => node.id === submitId);
  if (!submit) return null;
  const rowControls = nodes
    .filter(node => node.isControl && node.visible && sameRowAs(node.rect, submit.rect) && node.rect.width > 0)
    .sort((left, right) => left.rect.left - right.rect.left);
  let min: number | null = null;
  for (let i = 1; i < rowControls.length; i += 1) {
    const gap = rowControls[i].rect.left - rowControls[i - 1].rect.right;
    if (gap < 0) continue; // stacked/overlapping render artifacts
    if (min == null || gap < min) min = gap;
  }
  return min;
}

/** Convenience for callers re-checking placement on input events: the plan is
 *  already satisfied when the host is the chosen node's immediate previous
 *  sibling inside the chosen parent. */
export function placementSatisfied(
  plan: InsertionPlan | null,
  hostParentId: number | null,
  hostNextSiblingId: number | null
): boolean {
  if (!plan) return false;
  return hostParentId === plan.parentId && hostNextSiblingId === plan.beforeId;
}
