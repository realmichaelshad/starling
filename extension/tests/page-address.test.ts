import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { pageAddressForService } from "../shared/api";

/**
 * The address of the page a post was seen on is sent to the service with its path only.
 *
 * Found by the security review: every request and every snapshot carried `location.href`, query
 * string and fragment included. On X the query string holds what the user searched for; the
 * disclosure lists "the post you ask about and nearby posts", not search terms, so they stay put.
 */
describe("the page address sent to the service", () => {
  it.each([
    ["https://x.com/search?q=my+private+search&src=typed_query&f=live", "https://x.com/search"],
    ["https://x.com/someone/status/1900000000000000001?s=20&t=AbCdEf123", "https://x.com/someone/status/1900000000000000001"],
    ["https://x.com/home#section", "https://x.com/home"],
    ["https://x.com/i/bookmarks?post_id=42", "https://x.com/i/bookmarks"],
    ["https://twitter.com/someone", "https://twitter.com/someone"],
  ])("%s -> %s", (href, sent) => {
    expect(pageAddressForService(href)).toBe(sent);
  });

  it("sends nothing for an address it cannot read", () => {
    for (const href of ["", "not a url", "javascript:alert(1)", "data:text/html,x", "about:blank"]) {
      expect(pageAddressForService(href), href).toBe("");
    }
  });

  it("is what the content script uses, everywhere it names the page to the service", () => {
    const source = readFileSync(new URL("../entrypoints/content.ts", import.meta.url), "utf8");
    expect(source).not.toMatch(/(?:page_url|source_page)\s*:\s*location\.href/);
    expect(source.match(/(?:page_url|source_page):\s*pageAddressForService\(location\.href\)/g)?.length).toBe(3);
  });
});
