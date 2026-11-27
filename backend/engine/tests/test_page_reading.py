"""The page as a reader sees it, not as one collapsed line.

`_TextExtractor` appended every text node to one list and `fetch_page_text`
joined it with spaces, so a fetched page arrived with no boundary anywhere in
it. A table of model prices came back as "Model Price Opus 5 $0.90 Sonnet 5
$0.18", which reads as one sentence and lets a figure be carried away under the
wrong label — and on a long page the `PAGE_TEXT_LIMIT` slice was taken in
document order, so the cookie notice and the nav could push the article out.
"""
from app.research import _TextExtractor

PRICING_PAGE = """
<html><head><title>Prices</title><style>.x{color:red}</style></head>
<body>
  <nav>Home Docs Pricing Sign in</nav>
  <div id="consent"><button>Accept all cookies</button></div>
  <main>
    <h1>Model pricing</h1>
    <p>Prices changed on <a href="/blog">July 30</a>, and the mid tier moved
       most.</p>
    <table>
      <tr><th>Model</th><th>Per million</th></tr>
      <tr><td>Opus 5</td><td>$0.90</td></tr>
      <tr><td>Sonnet 5</td><td>$0.18</td></tr>
    </table>
    <ul><li>Batch is half price</li><li>Cache reads are free</li></ul>
  </main>
  <footer>Copyright 2026</footer>
</body></html>
"""


def read(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.text()


def test_table_cells_do_not_fuse_into_one_sentence():
    text = read(PRICING_PAGE)
    assert "Opus 5 $0.90" not in text
    assert "$0.90\nSonnet 5" not in text.replace("\n", " \n")
    # Each cell is its own block, so a price stays attached to its own model.
    assert "Opus 5" in text and "$0.90" in text


def test_a_heading_does_not_run_into_its_paragraph():
    text = read(PRICING_PAGE)
    assert "Model pricingPrices" not in text
    assert "Model pricing Prices" not in text
    assert "# Model pricing" in text


def test_inline_tags_stay_inside_their_sentence():
    """<a> is not a block — breaking on it would shatter every sentence."""
    text = read(PRICING_PAGE)
    assert "Prices changed on July 30, and the mid tier moved most." in text


def test_list_items_keep_their_shape():
    text = read(PRICING_PAGE)
    assert "- Batch is half price" in text
    assert "- Cache reads are free" in text


def test_the_chrome_is_gone():
    text = read(PRICING_PAGE)
    for junk in ("Accept all cookies", "Sign in", "Copyright 2026", "color:red"):
        assert junk not in text


def test_the_article_wins_over_the_whole_page():
    """A long nav must not be able to push the article out of the limit."""
    noise = "<nav>" + " ".join(f"Section{i}" for i in range(400)) + "</nav>"
    padding = "<div>" + ("unrelated sidebar copy " * 200) + "</div>"
    body = ("<main><p>" + ("The measured figure is $0.90 per million. " * 30)
            + "</p></main>")
    text = read(f"<html><body>{noise}{padding}{body}</body></html>")
    assert "$0.90" in text
    assert "unrelated sidebar copy" not in text


def test_a_page_with_no_main_still_reads():
    text = read("<html><body><p>One plain paragraph with $12.40 in it.</p>"
                "<p>And a second one.</p></body></html>")
    assert "One plain paragraph with $12.40 in it." in text
    assert "And a second one." in text


def test_br_separates_lines():
    text = read("<html><body><p>first line<br>second line</p></body></html>")
    assert "first linesecond" not in text
    assert "first line" in text and "second line" in text


def test_repeated_blocks_collapse():
    text = read("<html><body><p>Same</p><p>Same</p><p>Other</p></body></html>")
    assert text.count("Same") == 1


def test_adjacent_inline_tags_keep_their_word_boundary():
    """Found on a live page, not in a fixture.

    Hacker News separates the author from the timestamp with a whitespace-only
    text node between two tags. Buffering the text made that gap load-bearing,
    and the guard that skipped empty nodes threw it away — so the byline came
    back as "Jrh020340 minutes ago", a username and a duration welded into one
    token that reads as neither.
    """
    text = read('<html><body><p><a href="u">Jrh0203</a> '
                '<span><a href="i">40 minutes ago</a></span></p></body></html>')
    assert "Jrh020340" not in text
    assert "Jrh0203 40 minutes ago" in text


def test_a_real_word_split_by_markup_is_not_broken():
    """The other side of that fix: no whitespace means no boundary."""
    assert "Hypertext" in read("<html><body><p><b>Hyper</b>text</p></body></html>")


def test_malformed_html_does_not_raise():
    assert isinstance(read("<html><body><p>unclosed <b>bold</body>"), str)
    assert isinstance(read(""), str)
