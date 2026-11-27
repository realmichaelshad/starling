import { describe, expect, it } from "vitest";
import { PILL_PALETTE, X_SURFACES, contrastRatio, isThemePreference, pillThemeFor, resolveTheme, themeHelp, toneFromRgb } from "../shared/theme";

// The three backgrounds X actually paints, so the thresholds are pinned to real
// values rather than to a guess about what "dark" means.
const LIGHTS_OUT = [0, 0, 0] as const;      // #000000
const DIM = [21, 32, 43] as const;          // #15202b
const DEFAULT = [255, 255, 255] as const;   // #ffffff

describe("toneFromRgb", () => {
  it("tells X's three themes apart", () => {
    expect(toneFromRgb(...LIGHTS_OUT)).toBe("dark");
    expect(toneFromRgb(...DIM)).toBe("dim");
    expect(toneFromRgb(...DEFAULT)).toBe("light");
  });

  it("keeps Dim on the dark side of the old light/dark cut", () => {
    // Everything inline in X's chrome still asks only "is this dark?", and Dim
    // has to keep answering yes or every pill on a Dim page turns white.
    expect(toneFromRgb(...DIM)).not.toBe("light");
  });

  it("reads a near-black surface as lights-out rather than dim", () => {
    expect(toneFromRgb(8, 8, 8)).toBe("dark");
  });

  it("puts X's own greys and mid-tones on the right side", () => {
    expect(toneFromRgb(30, 39, 50)).toBe("dim");     // #1e2732, Dim's cards
    expect(toneFromRgb(247, 249, 249)).toBe("light"); // #f7f9f9, X's inset light
  });
});

describe("resolveTheme", () => {
  it("lets X decide by default", () => {
    expect(resolveTheme("auto", "light")).toEqual({ mode: "light", tone: "light" });
    expect(resolveTheme("auto", "dim")).toEqual({ mode: "dark", tone: "dim" });
    expect(resolveTheme("auto", "dark")).toEqual({ mode: "dark", tone: "dark" });
  });

  it("keeps Dim's slate when dark is forced on a Dim page", () => {
    // "Go dark" is not "stop fitting in" — a #000 panel on #15202b reads as a
    // hole punched in the page.
    expect(resolveTheme("dark", "dim")).toEqual({ mode: "dark", tone: "dim" });
  });

  it("falls to lights-out when there is no dark tone to inherit", () => {
    expect(resolveTheme("dark", "light")).toEqual({ mode: "dark", tone: "dark" });
  });

  it("ignores the page entirely when light is forced", () => {
    for (const page of ["light", "dim", "dark"] as const) {
      expect(resolveTheme("light", page), page).toEqual({ mode: "light", tone: "light" });
    }
  });
});

describe("isThemePreference", () => {
  it("accepts the three it stores and refuses anything else", () => {
    expect(isThemePreference("auto")).toBe(true);
    expect(isThemePreference("light")).toBe(true);
    expect(isThemePreference("dark")).toBe(true);
    // Storage is shared with older builds, so junk has to bounce off the loader.
    for (const junk of ["dim", "Dark", "", null, undefined, 1, {}]) {
      expect(isThemePreference(junk), String(junk)).toBe(false);
    }
  });
});

describe("themeHelp", () => {
  it("says X is in charge, and what X currently is", () => {
    expect(themeHelp("auto", "dim")).toBe("Following X, which is dim right now.");
    expect(themeHelp("auto", "light")).toBe("Following X, which is light right now.");
  });

  it("admits the override instead of implying X still decides", () => {
    // The switch has two positions and three states behind it; this line is the
    // only thing that can say which one is live.
    expect(themeHelp("light", "dark")).toBe("Set to light. X is dark.");
    expect(themeHelp("dark", "light")).toBe("Set to dark. X is light.");
  });
});

describe("the composer pill is readable on every X surface", () => {
  // The Home "Create post" pill used to be forced dark on every theme: near-white text on X's white
  // toolbar. The pill now takes the surface it sits on, and these are the colours the content
  // script's CSS is built from.
  it("measures contrast the way WCAG does", () => {
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 1);
    expect(contrastRatio("#ffffff", "#ffffff")).toBeCloseTo(1, 5);
    // A translucent wash is composited over the page before it is measured.
    expect(contrastRatio("#000000", "rgba(0,0,0,.0)", "#ffffff")).toBeCloseTo(21, 1);
  });

  it("gives every surface a skin whose text passes 4.5:1", () => {
    for (const tone of ["light", "dim", "dark"] as const) {
      const skin = PILL_PALETTE[pillThemeFor(tone)];
      const page = X_SURFACES[tone];
      expect(contrastRatio(skin.text, page, page), `${tone}: resting label`).toBeGreaterThanOrEqual(4.5);
      expect(contrastRatio(skin.doneText, skin.doneWash, page), `${tone}: Done`).toBeGreaterThanOrEqual(4.5);
      expect(contrastRatio(skin.errorText, skin.errorWash, page), `${tone}: Try again`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("proves why the pill must follow the page: a dark pill on a white toolbar is unreadable", () => {
    expect(contrastRatio(PILL_PALETTE.dark.text, X_SURFACES.light)).toBeLessThan(1.5);
    expect(contrastRatio(PILL_PALETTE.dark.doneText, PILL_PALETTE.dark.doneWash, X_SURFACES.light)).toBeLessThan(4.5);
    expect(pillThemeFor("light")).toBe("light");
    expect(pillThemeFor("dim")).toBe("dark");
  });
});
