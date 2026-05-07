"""
Entry point for drop-pensa.
"""
import sys
import uvicorn


def main():
    """Run the application."""
    uvicorn.run(
        "drop_pensa.app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        # Trust X-Forwarded-* headers from the reverse proxy (nginx).
        # Without this, request.base_url and request.client.host see the
        # internal docker network instead of the real client / scheme.
        proxy_headers=True,
        # Limit who's trusted to set those headers. Inside the docker
        # network the upstream is always nginx via the bridge; the wildcard
        # is fine because we never expose port 8000 to the host directly.
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    sys.exit(main())
