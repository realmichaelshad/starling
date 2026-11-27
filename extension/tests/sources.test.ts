import { describe, expect, it } from "vitest";
import { MAX_SOURCES, mergeSources, sourceLinks } from "../shared/sources";

describe("sourceLinks: the pages behind a draft, as links a person can open", () => {
  it("keeps http and https pages, labelled by title, with the site beside it", () => {
    expect(sourceLinks([
      { url: "https://www.theverge.com/2026/9/21/model-launch", title: "The launch, explained" },
      { url: "http://example.org/post" },
    ])).toEqual([
      { href: "https://www.theverge.com/2026/9/21/model-launch", label: "The launch, explained", host: "theverge.com" },
      { href: "http://example.org/post", label: "example.org", host: "example.org" },
    ]);
  });

  it("never turns anything else into a link", () => {
    expect(sourceLinks([
      { url: "javascript:alert(1)", title: "click me" },
      { url: "data:text/html,<b>x</b>" },
      { url: "chrome-extension://abc/page.html" },
      { url: "https://user:secret@example.com/" },
      { url: "not a url" },
      { title: "no address" },
      null,
      "https://a-bare-string.example/",
    ])).toEqual([]);
    expect(sourceLinks(undefined)).toEqual([]);
    expect(sourceLinks({ url: "https://example.com/" })).toEqual([]);
  });

  it("shows each page once and at most three", () => {
    const many = ["a", "b", "c", "d", "e"].map(name => ({ url: `https://${name}.example/` }));
    const links = sourceLinks([many[0], many[0], ...many]);
    expect(links).toHaveLength(MAX_SOURCES);
    expect(links.map(link => link.host)).toEqual(["a.example", "b.example", "c.example"]);
  });

  it("keeps a long title to one line", () => {
    const [link] = sourceLinks([{ url: "https://example.com/", title: "word ".repeat(40) }]);
    expect(link.label.length).toBeLessThanOrEqual(90);
    expect(link.label.endsWith("…")).toBe(true);
    expect(link.label).not.toMatch(/\s{2}/);
  });

  it("shows a chat draft's own sources and its research together, each page once", () => {
    const draftSources = [{ url: "https://a.example/story", title: "The story" }];
    const research = [{ url: "https://a.example/story", title: "The story again" }, { url: "https://b.example/data" }];
    expect(sourceLinks(mergeSources(draftSources, research)).map(link => link.href))
      .toEqual(["https://a.example/story", "https://b.example/data"]);
    // The draft's own label wins for a page in both.
    expect(sourceLinks(mergeSources(draftSources, research))[0].label).toBe("The story");
    // Either list may be missing or not a list at all.
    expect(sourceLinks(mergeSources(undefined, research))).toHaveLength(2);
    expect(sourceLinks(mergeSources(draftSources, "nope", null))).toHaveLength(1);
    expect(mergeSources(undefined, undefined)).toEqual([]);
  });
});
