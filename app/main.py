import logging
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator

from app.fixer.fixer import ScanTimeout, fix_page
from app.fixer.llm import LLMUnavailable
from app.security import SSRFBlocked, assert_public_host

logger = logging.getLogger("accesspilot")

app = FastAPI(
    title="AccessPilot",
    description="AI-powered web accessibility auditor -- scan, fix, verify.",
    version="1.0.0",
)

STATIC_DIR = Path(__file__).parent / "static"
MAX_URL_LENGTH = 2048

MAX_CONCURRENT_SCANS = int(os.environ.get("MAX_CONCURRENT_SCANS", "2"))
_scan_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SCANS)


class ScanRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def must_be_http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or len(value) > MAX_URL_LENGTH
        ):
            raise ValueError(
                "url must be a well-formed http:// or https:// URL "
                f"(max {MAX_URL_LENGTH} characters)"
            )
        return value


def _violation_json(result: dict) -> dict:
    return {
        "issue_type": result["rule_id"],
        "wcag_criterion": result["wcag_criterion"],
        "selector": result["selector"],
        "severity": result["severity"],
        "original_html": result["original_html"],
        "fix": {
            "fix_code": result["fix_code"],
            "explanation": result["explanation"],
            "llm_validated": result["llm_validated"],
            "rescan_validated": result["rescan_validated"],
            "status": result["status"],
            **(
                {"fix_generation_failed": True}
                if result["status"] == "fix_generation_failed"
                else {}
            ),
            **({"needs_review": True} if result["needs_review"] else {}),
            **({"detail": result["detail"]} if result["detail"] else {}),
        },
    }


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/scan")
def scan(req: ScanRequest):
    try:
        assert_public_host(req.url)
    except SSRFBlocked as e:
        return JSONResponse(status_code=422, content={"error": str(e)})

    if not _scan_slots.acquire(blocking=False):
        return JSONResponse(
            status_code=429,
            content={"error": "The server is busy with other scans. Try again shortly."},
            headers={"Retry-After": "30"},
        )
    try:
        report = fix_page(req.url)
    except SSRFBlocked as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    except LLMUnavailable as e:
        logger.error("LLM unavailable while scanning %s: %s", req.url, e)
        return JSONResponse(
            status_code=502,
            content={"error": "The AI provider is unavailable or misconfigured."},
        )
    except ScanTimeout as e:
        return JSONResponse(status_code=504, content={"error": str(e)})
    except Exception:
        logger.exception("Unexpected failure scanning %s", req.url)
        return JSONResponse(status_code=502, content={"error": "The scan failed."})
    finally:
        _scan_slots.release()

    return {
        "url": req.url,
        "wcag_level": "AA",
        "total_violations_found": report["total_violations_found"],
        "violations_processed": report["violations_processed"],
        "violations": [_violation_json(r) for r in report["results"]],
    }


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422, content={"error": jsonable_encoder(exc.errors())}
    )
