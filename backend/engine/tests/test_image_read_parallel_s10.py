"""S10: in the composer, the post's image is described WHILE its links are read.

A reply or quote created from the composer described the post's image (a vision call, about
1-2 s) and only then started reading the post's own links (up to 3 s): two waits in a row
before the first draft token. The ✦ press already ran them side by side; the composer now does.
"""
from __future__ import annotations

import asyncio
import json
import time

from app import cloud, main, news, providers, research
from app.schemas import BrowserAssistRequest, ComposerEnhancement, MediaItem, PostContext

PAGE = ("Example Labs released Orca 2, an open-weight coding model under the Apache 2.0 licence. "
        "It ships with an inference guide for a single 24 GB card.")


def test_the_image_and_the_links_are_read_at_the_same_time(monkeypatch):
    starts: dict[str, float] = {}

    def image_line(context):
        starts["image"] = time.monotonic()
        time.sleep(0.4)
        return " The post has an IMAGE attached showing: a benchmark table."

    def preview(url, *, timeout_s=None):
        starts["link"] = time.monotonic()
        time.sleep(0.4)
        return {"text": PAGE, "title": "Orca 2 is out", "image": None, "description": None}

    def chat(model, messages, *, options=None, fmt=None):
        props = list(((fmt or {}).get("json_schema") or {}).get("schema", {}).get("properties", {}))
        answer = {name: "adds the consequence" for name in props}
        answer.update(point="The Apache 2.0 licence matters more than the benchmark.",
                      text="the apache 2.0 licence on orca 2 is the real news, not the benchmark table")
        return json.dumps({k: answer[k] for k in props}), {}

    monkeypatch.setattr(main, "_post_image_line", image_line)
    monkeypatch.setattr(research, "research_enabled", lambda: True)
    monkeypatch.setattr(research, "fetch_page_preview", preview)
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [])
    monkeypatch.setenv("STARLING_SERVICES_URL", "http://127.0.0.1:9")
    cloud.set_services_token("t" * 43)
    monkeypatch.setattr(cloud, "chat", chat)
    providers.reset_router()
    context = PostContext(post_id="1790000000000000901", author="Lab News", author_handle="labnews",
                          text="Our new open model is out today. Details here:",
                          links=["https://example.com/blog/orca-2"], visibility="public",
                          media=[MediaItem(url="https://pbs.example/table.png", kind="image")])
    request = BrowserAssistRequest(mode="enhance_composer", context=context,
                                   composer=ComposerEnhancement(action="reply", draft_text="", intent="create"))
    try:
        started = time.monotonic()
        result = asyncio.run(main.browser_assist(request))
        elapsed = time.monotonic() - started
    finally:
        cloud.reset()
        providers.reset_router()
    assert "image" in starts and "link" in starts
    assert abs(starts["image"] - starts["link"]) < 0.2, "the two reads overlap"
    assert elapsed < 0.8 + 0.6, f"one after the other would be 0.8 s before any drafting; took {elapsed:.2f} s"
    assert (result.get("decision") or {}).get("web_checked") is True
