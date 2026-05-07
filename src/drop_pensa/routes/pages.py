"""
Static-ish content pages: about, api, privacy, terms.

Rendered with Jinja2 from templates/, served with the same security
headers as the rest of the UI. No params, no DB, no JS — just prose.
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


router = APIRouter()


_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# Map URL path → template name. Adding a new doc page is a one-liner.
_PAGES = {
    "about":   "about.html",
    "api":     "api.html",
    "privacy": "privacy.html",
    "terms":   "terms.html",
}


def _render(request: Request, page: str) -> HTMLResponse:
    """Common render path. no-store so updates ship without cache stalls."""
    template = _PAGES[page]
    response = _templates.TemplateResponse(template, {"request": request})
    response.headers["Cache-Control"] = "no-store"
    return response


# We register one route per page rather than a single /{page} catch-all,
# so the URL space is explicit and a typo gives a clean 404 instead of
# rendering a confusing template-not-found error.
@router.get("/about", response_class=HTMLResponse, include_in_schema=False)
async def about(request: Request):
    return _render(request, "about")


@router.get("/api", response_class=HTMLResponse, include_in_schema=False)
async def api_docs(request: Request):
    return _render(request, "api")


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
async def privacy(request: Request):
    return _render(request, "privacy")


@router.get("/terms", response_class=HTMLResponse, include_in_schema=False)
async def terms(request: Request):
    return _render(request, "terms")
