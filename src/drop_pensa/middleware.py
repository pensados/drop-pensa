"""
Security headers middleware.

We apply a strict CSP and assorted hardening headers to all responses,
EXCEPT to the served files themselves (`/f/<id>/<filename>`) — applying
CSP to user content is meaningless (we already serve it as text/plain
when dangerous) and X-Frame-Options would break embedding for legit
users sharing images.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


# Content Security Policy for the web UI.
#
# - default-src 'self': only our own origin for everything by default
# - script-src 'self': the QR lib and app.js are served by us
# - style-src 'self': style.css is served by us; no inline styles allowed
# - img-src 'self' data:: favicon + the QR svg (which is inline SVG, not data URI,
#   but we leave data: in for future flexibility)
# - connect-src 'self': fetch/XHR only to our own origin
# - frame-ancestors 'none': nobody can iframe us (clickjacking)
# - form-action 'self': forms can only submit to us
# - base-uri 'self': blocks <base> tag tricks
# - object-src 'none': no <object>, <embed>, <applet>
# - upgrade-insecure-requests: belt-and-suspenders alongside HSTS
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "upgrade-insecure-requests"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply security headers to all UI/API responses except served files."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        path = request.url.path
        is_file_serve = path.startswith("/f/") and len(path.split("/")) >= 4

        if not is_file_serve:
            # CSP only on UI/API responses. Files don't need it (we don't
            # render them) and adding it would just confuse browsers.
            response.headers.setdefault("Content-Security-Policy", _CSP)

        # Headers safe to apply to all responses, including served files.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            # Deny everything we don't use — camera, mic, geolocation,
            # payment APIs, FLoC, etc.
            "camera=(), microphone=(), geolocation=(), payment=(), "
            "usb=(), magnetometer=(), gyroscope=(), accelerometer=(), "
            "browsing-topics=()"
        )
        # HSTS is set by nginx (it owns TLS); don't duplicate from here.

        return response
