import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import MagicMock

from app.services.markdown_for_agents import (
    accepts_markdown,
    build_list_markdown,
    build_markdown_response,
    html_to_markdown,
)


def main() -> None:
    req = MagicMock()
    req.headers = {"accept": "text/markdown"}
    assert accepts_markdown(req)
    req.headers = {"accept": "text/html"}
    assert not accepts_markdown(req)

    html = (
        "<!DOCTYPE html><html><head>"
        "<title>Test</title>"
        '<meta name="description" content="desc">'
        "</head><body><main><h1>Hello</h1><p>World</p></main></body></html>"
    )
    md, fm, _ = html_to_markdown(html)
    assert fm["ai-train"] == "allow"
    assert fm["ai-search"] == "allow"
    assert "Hello" in md

    resp = build_markdown_response("## body", frontmatter=fm)
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.headers["vary"] == "Accept"
    assert resp.headers["content-signal"] == "ai-train=yes, search=yes, ai-input=yes"
    body = resp.body.decode("utf-8")
    assert body.startswith("---")
    assert "ai-train: allow" in body

    class Item:
        def __init__(self, id: str, title: str, summary: str = ""):
            self.id = id
            self.title = title
            self.summary = summary

    list_md, list_fm, _ = build_list_markdown(
        title="List",
        description="Desc",
        page_url="https://example.com/",
        site_url="https://example.com",
        items=[Item("abc", "Title", "Summary")],
        page_jsonld=None,
    )
    assert "Title" in list_md
    assert list_fm["title"] == "List"
    print("OK")


if __name__ == "__main__":
    main()
