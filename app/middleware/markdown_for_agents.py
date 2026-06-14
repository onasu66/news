"""Markdown for Agents ミドルウェア — HTML レスポンスを Accept: text/markdown 時に変換"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.services.markdown_for_agents import (
    accepts_markdown,
    build_markdown_response,
    html_to_markdown,
    should_convert_path,
)


class MarkdownForAgentsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in ("GET", "HEAD") or not should_convert_path(request.url.path):
            return await call_next(request)

        wants_md = accepts_markdown(request)
        response = await call_next(request)

        content_type = (response.headers.get("content-type") or "").lower()
        if "text/html" not in content_type:
            return response

        response.headers["Vary"] = "Accept"

        if not wants_md:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        html = body.decode("utf-8", errors="replace")
        md_body, frontmatter, jsonld = html_to_markdown(html)
        md_response = build_markdown_response(
            md_body,
            frontmatter=frontmatter,
            jsonld=jsonld or None,
            original_html=html,
        )
        if request.method == "HEAD":
            return Response(
                status_code=md_response.status_code,
                headers=dict(md_response.headers),
            )
        return md_response
