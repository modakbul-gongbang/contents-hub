"""Tests for tools/parse.py and tools/fetchers.py — the new tools layer.

Re-targets the RSS / YouTube / HTTP-fetch coverage that previously lived in
``test_rss.py`` and ``test_youtube.py`` (both still around as long as the
``collectors/`` subpackage exists; T14 will delete those).  The tools layer
returns a JSON string per the ``ToolHandler`` contract, so assertions parse
the JSON before validating the same fields as the legacy parser tests.
"""

from __future__ import annotations

import json

import httpx
import respx

from contents_hub.tools.fetchers import fetch_url
from contents_hub.tools.parse import parse_html, parse_json, parse_rss


# ---------------------------------------------------------------------------
# Sample feeds (mirrored from tests/test_rss.py — assertion logic preserved)
# ---------------------------------------------------------------------------

RSS2_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>My Blog</title>
    <link>https://example.com</link>
    <item>
      <title>First Post</title>
      <link>https://example.com/first-post</link>
      <description>A short summary.</description>
      <dc:creator>Alice</dc:creator>
      <pubDate>Mon, 01 Jan 2024 12:00:00 +0000</pubDate>
      <category>tech</category>
      <category>python</category>
      <content:encoded><![CDATA[<p>Full content here</p>]]></content:encoded>
    </item>
    <item>
      <title>Second Post</title>
      <link>https://example.com/second-post/</link>
      <description>Another summary.</description>
      <pubDate>Tue, 02 Jan 2024 08:30:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Blog</title>
  <entry>
    <title>Atom Entry</title>
    <link rel="alternate" href="https://example.com/atom-entry"/>
    <summary>Atom summary</summary>
    <author><name>Bob</name></author>
    <published>2024-03-15T10:00:00Z</published>
    <category term="science"/>
    <content type="html">&lt;p&gt;Atom content&lt;/p&gt;</content>
  </entry>
</feed>
"""

YOUTUBE_FEED_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <title>Test Channel</title>
  <entry>
    <id>yt:video:dQw4w9WgXcQ</id>
    <yt:videoId>dQw4w9WgXcQ</yt:videoId>
    <title>First Video Title</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=dQw4w9WgXcQ"/>
    <published>2024-01-15T10:00:00+00:00</published>
  </entry>
</feed>
"""


# ---------------------------------------------------------------------------
# parse_rss — RSS 2.0 / Atom / YouTube Atom
# ---------------------------------------------------------------------------


class TestParseRss2:
    async def test_basic_parse(self):
        out = await parse_rss(RSS2_XML, feed_url="https://example.com/feed")
        result = json.loads(out)
        assert result["ok"] is True
        assert result["feed_title"] == "My Blog"
        assert len(result["items"]) == 2

    async def test_first_item_fields(self):
        out = await parse_rss(RSS2_XML, feed_url="https://example.com/feed")
        item = json.loads(out)["items"][0]
        assert item["url"] == "https://example.com/first-post"
        assert item["title"] == "First Post"
        assert item["summary"] == "A short summary."
        assert item["author"] == "Alice"
        assert item["published_at"].startswith("2024-01-01T12:00")
        assert item["tags"] == ["tech", "python"]
        assert "<p>Full content here</p>" in item["content_html"]

    async def test_trailing_slash_normalized(self):
        out = await parse_rss(RSS2_XML, feed_url="https://example.com/feed")
        items = json.loads(out)["items"]
        assert items[1]["url"] == "https://example.com/second-post"

    async def test_max_items_limit(self):
        out = await parse_rss(RSS2_XML, feed_url="https://example.com/feed", max_items=1)
        items = json.loads(out)["items"]
        assert len(items) == 1


class TestParseAtom:
    async def test_basic_parse(self):
        out = await parse_rss(ATOM_XML, feed_url="https://example.com/feed")
        result = json.loads(out)
        assert result["ok"] is True
        assert result["feed_title"] == "Atom Blog"
        assert len(result["items"]) == 1

    async def test_entry_fields(self):
        out = await parse_rss(ATOM_XML, feed_url="https://example.com/feed")
        item = json.loads(out)["items"][0]
        assert item["url"] == "https://example.com/atom-entry"
        assert item["title"] == "Atom Entry"
        assert item["author"] == "Bob"
        assert item["published_at"].startswith("2024-03-15T10:00")
        assert item["tags"] == ["science"]


class TestParseYouTube:
    """Tools layer recognises ``yt:videoId`` per learnings.json T4 round 2."""

    async def test_youtube_atom_recognised(self):
        out = await parse_rss(YOUTUBE_FEED_XML, feed_url="https://example.com/feed")
        result = json.loads(out)
        assert result["ok"] is True
        item = result["items"][0]
        assert item["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert item.get("video_id") == "dQw4w9WgXcQ"


class TestParseErrors:
    async def test_invalid_xml(self):
        out = await parse_rss("<not valid xml", feed_url="https://x.com/feed")
        result = json.loads(out)
        assert result["ok"] is False
        assert "XML parse error" in result["error"]

    async def test_unknown_format(self):
        out = await parse_rss(
            '<?xml version="1.0"?><html></html>',
            feed_url="https://x.com/feed",
        )
        result = json.loads(out)
        assert result["ok"] is False
        assert "Unknown feed format" in result["error"]


# ---------------------------------------------------------------------------
# parse_html — text + meta extraction
# ---------------------------------------------------------------------------


class TestParseHtml:
    async def test_extracts_title(self):
        out = await parse_html("<html><head><title>Hello</title></head><body>x</body></html>")
        result = json.loads(out)
        assert result["ok"] is True
        assert result["title"] == "Hello"

    async def test_extracts_meta(self):
        html = (
            '<html><head>'
            '<meta name="description" content="my page">'
            '<meta property="og:title" content="OG Title">'
            '</head></html>'
        )
        out = await parse_html(html)
        result = json.loads(out)
        assert result["meta"]["description"] == "my page"
        assert result["meta"]["og:title"] == "OG Title"

    async def test_extracts_links(self):
        html = '<a href="https://example.com/x">Click</a>'
        out = await parse_html(html, base_url="https://example.com/")
        result = json.loads(out)
        assert any(
            link["url"] == "https://example.com/x" and "Click" in link["text"]
            for link in result["links"]
        )

    async def test_strips_scripts(self):
        html = "<html><body>visible<script>hidden();</script></body></html>"
        out = await parse_html(html)
        assert "hidden" not in json.loads(out)["text"]
        assert "visible" in json.loads(out)["text"]

    async def test_empty_input(self):
        out = await parse_html("")
        result = json.loads(out)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# parse_json — body parse + path resolution
# ---------------------------------------------------------------------------


class TestParseJson:
    async def test_full_parse(self):
        out = await parse_json('{"a": 1, "b": [1, 2, 3]}')
        result = json.loads(out)
        assert result["ok"] is True
        assert result["value"] == {"a": 1, "b": [1, 2, 3]}

    async def test_path_resolution(self):
        out = await parse_json(
            '{"data": {"items": [{"title": "first"}, {"title": "second"}]}}',
            path="data.items[0].title",
        )
        result = json.loads(out)
        assert result["ok"] is True
        assert result["value"] == "first"

    async def test_invalid_json(self):
        out = await parse_json("not json")
        result = json.loads(out)
        assert result["ok"] is False

    async def test_path_miss(self):
        out = await parse_json('{"a": 1}', path="missing.key")
        result = json.loads(out)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# fetch_url — HTTP fetch via tools/fetchers.py
# ---------------------------------------------------------------------------


class TestFetchUrl:
    @respx.mock
    async def test_success_compacts_feed_by_default(self):
        respx.get("https://example.com/feed.xml").respond(
            200, text=RSS2_XML, headers={"content-type": "application/rss+xml"}
        )
        out = await fetch_url("https://example.com/feed.xml")
        result = json.loads(out)
        assert result["ok"] is True
        assert result["status"] == 200
        assert result["mode"] == "feed"
        assert result["feed_title"] == "My Blog"
        assert result["body"] == ""
        assert result["items"][0]["title"] == "First Post"

    @respx.mock
    async def test_raw_mode_preserves_original_body(self):
        respx.get("https://example.com/feed.xml").respond(
            200, text=RSS2_XML, headers={"content-type": "application/rss+xml"}
        )
        out = await fetch_url("https://example.com/feed.xml", mode="raw", max_chars=0)
        result = json.loads(out)
        assert result["ok"] is True
        assert result["mode"] == "raw"
        assert "My Blog" in result["body"]

    @respx.mock
    async def test_html_mode_extracts_readable_text_and_links(self):
        html = """
        <html><head><title>Page</title></head>
        <body><nav>ignore</nav><article><h1>Hello</h1>
        <p>Useful body text.</p><a href="/next">Next</a></article></body></html>
        """
        respx.get("https://example.com/post").respond(
            200, text=html, headers={"content-type": "text/html"}
        )
        out = await fetch_url("https://example.com/post", max_chars=100)
        result = json.loads(out)
        assert result["ok"] is True
        assert result["mode"] == "html"
        assert result["title"] == "Page"
        assert "Useful body text." in result["markdown"]
        assert result["links"] == [{"url": "https://example.com/next", "text": "Next"}]

    @respx.mock
    async def test_headers_are_compacted(self):
        respx.get("https://example.com/post").respond(
            200,
            text="<html><body>ok</body></html>",
            headers={
                "content-type": "text/html",
                "content-security-policy": "x" * 1000,
                "set-cookie": "secret=value",
            },
        )
        out = await fetch_url("https://example.com/post")
        result = json.loads(out)
        assert result["headers"]["content-type"] == "text/html"
        assert "content-length" in result["headers"]
        assert "content-security-policy" not in result["headers"]
        assert "set-cookie" not in result["headers"]

    @respx.mock
    async def test_reddit_detail_json_flattens_listing_children(self):
        payload = [
            {
                "kind": "Listing",
                "data": {
                    "children": [
                        {
                            "kind": "t3",
                            "data": {
                                "title": "Post title",
                                "permalink": "/r/test/comments/abc/post_title/",
                                "selftext": "Post body",
                                "author": "alice",
                            },
                        }
                    ]
                },
            },
            {
                "kind": "Listing",
                "data": {
                    "children": [
                        {
                            "kind": "t1",
                            "data": {
                                "body": "Useful comment",
                                "permalink": "/r/test/comments/abc/post_title/def/",
                                "author": "bob",
                            },
                        }
                    ]
                },
            },
        ]
        respx.get("https://www.reddit.com/r/test/comments/abc/post_title/.json").respond(
            200, json=payload, headers={"content-type": "application/json"}
        )
        out = await fetch_url("https://www.reddit.com/r/test/comments/abc/post_title/.json")
        result = json.loads(out)
        assert result["mode"] == "json"
        assert result["items"][0]["title"] == "Post title"
        assert result["items"][0]["url"] == "https://www.reddit.com/r/test/comments/abc/post_title/"
        assert result["items"][1]["summary"] == "Useful comment"

    @respx.mock
    async def test_404(self):
        respx.get("https://example.com/feed.xml").respond(404)
        out = await fetch_url("https://example.com/feed.xml")
        result = json.loads(out)
        assert result["ok"] is False
        assert result["status"] == 404
        assert "HTTP 404" in result["error"]
        assert result["error_type"] == "http"

    @respx.mock
    async def test_timeout(self):
        respx.get("https://example.com/slow").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        out = await fetch_url("https://example.com/slow", timeout=1.0)
        result = json.loads(out)
        assert result["ok"] is False
        assert result["error_type"] == "timeout"

    @respx.mock
    async def test_network_error(self):
        respx.get("https://example.com/dead").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        out = await fetch_url("https://example.com/dead")
        result = json.loads(out)
        assert result["ok"] is False
        assert result["error_type"] == "network"
