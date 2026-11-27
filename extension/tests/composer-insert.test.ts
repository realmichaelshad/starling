import { describe, expect, it } from "vitest";
import {
  chooseInsertionBeforeSubmit,
  isCompactRow,
  placementSatisfied,
  rowControlBreathingRoom,
  subtreeContainsForeignLeft,
  type RowNode,
  type RowRect,
} from "../shared/composer-insert";

const rect = (left: number, width: number, top = 0, height = 36): RowRect =>
  ({ left, right: left + width, top, bottom: top + height, width, height });

const node = (id: number, parentId: number | null, r: RowRect, opts: Partial<RowNode> = {}): RowNode =>
  ({ id, parentId, rect: r, isControl: false, visible: true, ...opts });

/** Shape A — ring/+ are SIBLINGS of Post's wrapper inside the action row:
 *  row → [toolbar-btn][ring][+][postWrapper → Post] */
function siblingShape(): { nodes: RowNode[]; submitId: number } {
  return {
    submitId: 5,
    nodes: [
      node(0, null, rect(0, 600)),                                    // action row
      node(1, 0, rect(0, 34), { isControl: true }),                   // toolbar icon
      node(2, 0, rect(430, 30), { isControl: true }),                 // counter ring
      node(3, 0, rect(466, 30), { isControl: true }),                 // + button
      node(4, 0, rect(520, 80)),                                      // postWrapper
      node(5, 4, rect(520, 80), { isControl: true }),                 // Post
    ],
  };
}

/** Shape B — the bug: ring/+ share ONE child with Post:
 *  row → [toolbar-btn][group → [ring][+][postWrapper → Post]] */
function sharedChildShape(): { nodes: RowNode[]; submitId: number } {
  return {
    submitId: 6,
    nodes: [
      node(0, null, rect(0, 600)),                                    // action row
      node(1, 0, rect(0, 34), { isControl: true }),                   // toolbar icon
      node(2, 0, rect(430, 170)),                                     // shared group
      node(3, 2, rect(430, 30), { isControl: true }),                 // counter ring
      node(4, 2, rect(466, 30), { isControl: true }),                 // + button
      node(5, 2, rect(520, 80)),                                      // postWrapper
      node(6, 5, rect(520, 80), { isControl: true }),                 // Post
    ],
  };
}

describe("chooseInsertionBeforeSubmit", () => {
  it("sibling shape: inserts before Post's wrapper, right of ring/+", () => {
    const { nodes, submitId } = siblingShape();
    expect(chooseInsertionBeforeSubmit(nodes, submitId)).toEqual({ beforeId: 4, parentId: 0 });
  });

  it("shared-child shape (the observed bug): descends INTO the group so the pill lands right of ring/+", () => {
    const { nodes, submitId } = sharedChildShape();
    // The old wrapper-climb chose {beforeId:2, parentId:0} here — stranding the
    // pill left of the whole ring/+/Post group. The geometry stop fixes it.
    expect(chooseInsertionBeforeSubmit(nodes, submitId)).toEqual({ beforeId: 5, parentId: 2 });
  });

  it("empty composer (no ring/+): climbs Post's own wrappers and stops at the toolbar row", () => {
    const nodes = [
      node(0, null, rect(0, 600)),                                    // action row
      node(1, 0, rect(0, 34), { isControl: true }),                   // toolbar icon
      node(2, 0, rect(520, 80)),                                      // wrapper a
      node(3, 2, rect(520, 80)),                                      // wrapper b
      node(4, 3, rect(520, 80), { isControl: true }),                 // Post
    ];
    expect(chooseInsertionBeforeSubmit(nodes, 4)).toEqual({ beforeId: 2, parentId: 0 });
  });

  it("tall parent (Post loose in the composer body): refuses to plan — skip mount, never clip", () => {
    const nodes = [
      node(0, null, rect(0, 600, 0, 400)),                            // composer body, tall
      node(1, 0, rect(520, 80, 340), { isControl: true }),            // Post
    ];
    expect(chooseInsertionBeforeSubmit(nodes, 1)).toBe(null);
  });

  it("controls on OTHER rows never stop the climb", () => {
    const nodes = [
      node(0, null, rect(0, 600)),                                    // action row at y=0
      node(1, 0, rect(10, 30, -80), { isControl: true }),             // control far above
      node(2, 0, rect(520, 80)),                                      // postWrapper
      node(3, 2, rect(520, 80), { isControl: true }),                 // Post
    ];
    expect(chooseInsertionBeforeSubmit(nodes, 3)).toEqual({ beforeId: 2, parentId: 0 });
  });

  it("hidden controls left of Post are ignored", () => {
    const nodes = [
      node(0, null, rect(0, 600)),
      node(1, 0, rect(430, 30), { isControl: true, visible: false }), // unmounted ring
      node(2, 0, rect(520, 80)),
      node(3, 2, rect(520, 80), { isControl: true }),
    ];
    expect(chooseInsertionBeforeSubmit(nodes, 3)).toEqual({ beforeId: 2, parentId: 0 });
  });

  it("unknown submit id yields no plan", () => {
    expect(chooseInsertionBeforeSubmit(siblingShape().nodes, 99)).toBe(null);
  });

  it("tall wrapper between the row and Post: pill still lands inside Post's own compact wrapper", () => {
    const nodes = [
      node(0, null, rect(0, 600, 0, 110)),                            // multi-row group (tall)
      node(1, 0, rect(430, 30, 74), { isControl: true }),             // ring, bottom row
      node(2, 0, rect(466, 30, 74), { isControl: true }),             // +, bottom row
      node(3, 0, rect(520, 80, 74)),                                  // postWrapper (compact)
      node(4, 3, rect(520, 80, 74), { isControl: true }),             // Post
    ];
    // The compact chain stops below the tall group, so the pill mounts inside
    // Post's own wrapper — still visually right of ring/+ and left of Post.
    expect(chooseInsertionBeforeSubmit(nodes, 4)).toEqual({ beforeId: 4, parentId: 3 });
  });
});

describe("subtreeContainsForeignLeft", () => {
  it("sees the ring/+ inside the shared group but not inside Post's own wrapper", () => {
    const { nodes, submitId } = sharedChildShape();
    expect(subtreeContainsForeignLeft(nodes, 2, submitId)).toBe(true);   // group holds ring/+
    expect(subtreeContainsForeignLeft(nodes, 5, submitId)).toBe(false);  // postWrapper is clean
  });

  it("controls RIGHT of the submit never count (nothing native sits there)", () => {
    const nodes = [
      node(0, null, rect(0, 600)),
      node(1, 0, rect(560, 30), { isControl: true }),                 // right of Post
      node(2, 0, rect(430, 80), { isControl: true }),                 // Post
    ];
    expect(subtreeContainsForeignLeft(nodes, 0, 2)).toBe(false);
  });

  it("non-control content (placeholder, avatar) counts — the pill stays right of it", () => {
    const nodes = [
      node(0, null, rect(0, 600)),
      node(1, 0, rect(60, 300), { isContent: true }),                 // placeholder text
      node(2, 0, rect(520, 80)),
      node(3, 2, rect(520, 80), { isControl: true }),                 // Reply
    ];
    expect(subtreeContainsForeignLeft(nodes, 0, 3)).toBe(true);
  });

  it("a full-row overlay spanning past the submit is chrome, not a neighbor", () => {
    const nodes = [
      node(0, null, rect(0, 600)),
      node(1, 0, rect(0, 600), { isContent: true }),                  // spans the whole row
      node(2, 0, rect(520, 80), { isControl: true }),                 // Post
    ];
    expect(subtreeContainsForeignLeft(nodes, 0, 2)).toBe(false);
  });
});

describe("collapsed reply row (avatar + placeholder + Reply, no controls between)", () => {
  it("plans the pill immediately left of Reply, not at the row's left edge", () => {
    const nodes = [
      node(0, null, rect(0, 620, 0, 48)),                             // collapsed row
      node(1, 0, rect(4, 40, 4, 40), { isContent: true }),            // avatar
      node(2, 0, rect(52, 560, 0, 48)),                               // content block B
      node(3, 2, rect(60, 300, 12, 24), { isContent: true }),         // "Post your reply"
      node(4, 2, rect(540, 70, 6)),                                   // replyWrapper
      node(5, 4, rect(540, 70, 6), { isControl: true }),              // Reply button
    ];
    // The old control-only owner test found nothing and fell back to the row →
    // pill left of B, i.e. next to the avatar. The placeholder now anchors the
    // owner at B, putting the pill between the placeholder and Reply.
    expect(chooseInsertionBeforeSubmit(nodes, 5)).toEqual({ beforeId: 4, parentId: 2 });
  });
});

describe("rowControlBreathingRoom", () => {
  it("reports the tightest gap between adjacent same-row controls", () => {
    const nodes = [
      node(0, null, rect(0, 600)),
      node(1, 0, rect(0, 36), { isControl: true }),
      node(2, 0, rect(40, 36), { isControl: true }),                  // gap 4 ← tightest
      node(3, 0, rect(100, 36), { isControl: true }),                 // gap 24
      node(4, 0, rect(520, 80), { isControl: true }),                 // Post
    ];
    expect(rowControlBreathingRoom(nodes, 4)).toBe(4);
  });

  it("ignores stacked/overlapping controls and other rows", () => {
    const nodes = [
      node(0, null, rect(0, 600)),
      node(1, 0, rect(10, 36), { isControl: true }),
      node(2, 0, rect(8, 36), { isControl: true }),                   // overlaps 1 → skipped
      node(3, 0, rect(300, 36, -80), { isControl: true }),            // different row
      node(4, 0, rect(520, 80), { isControl: true }),                 // Post: gap 474 from #1/#2
    ];
    expect(rowControlBreathingRoom(nodes, 4)).toBe(474);
  });

  it("null when fewer than two controls share the row", () => {
    expect(rowControlBreathingRoom([
      node(0, null, rect(0, 600)),
      node(1, 0, rect(520, 80), { isControl: true }),
    ], 1)).toBe(null);
  });
});

describe("compact-row + placement helpers", () => {
  it("isCompactRow tracks the submit height with a floor of 72", () => {
    expect(isCompactRow(60, 36)).toBe(true);
    expect(isCompactRow(72, 20)).toBe(true);
    expect(isCompactRow(73, 20)).toBe(false);
    expect(isCompactRow(120, 100)).toBe(true);
  });

  it("placementSatisfied only when host sits exactly before the chosen node", () => {
    const plan = { beforeId: 4, parentId: 0 };
    expect(placementSatisfied(plan, 0, 4)).toBe(true);
    expect(placementSatisfied(plan, 0, 2)).toBe(false);
    expect(placementSatisfied(plan, 1, 4)).toBe(false);
    expect(placementSatisfied(null, 0, 4)).toBe(false);
  });
});
