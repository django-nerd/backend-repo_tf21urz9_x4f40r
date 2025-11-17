import os
import re
import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import requests

from database import db

# Constants
DEFAULT_TTL_SECONDS = 600  # 10 minutes
UPLOAD_DIR = os.path.join(os.getcwd(), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve uploaded files
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


class PageCreate(BaseModel):
    html: str = Field(..., description="Raw HTML content to render as-is")
    ttl_seconds: Optional[int] = Field(DEFAULT_TTL_SECONDS, ge=60, le=86400)
    assets: Optional[List[str]] = Field(default_factory=list, description="List of asset paths associated with this page")


@app.on_event("startup")
async def startup_cleanup_task():
    async def cleanup_loop():
        while True:
            try:
                now = datetime.now(timezone.utc)
                expired = list(db["page"].find({"expires_at": {"$lte": now}}))
                if expired:
                    # delete associated assets
                    for doc in expired:
                        for asset in doc.get("assets", []) or []:
                            # asset paths are like /uploads/filename.ext or uploads/filename.ext
                            relative = asset.lstrip("/")
                            path = os.path.join(os.getcwd(), relative)
                            # If path is outside uploads dir, try within uploads
                            if not os.path.isfile(path):
                                path = os.path.join(UPLOAD_DIR, os.path.basename(relative))
                            try:
                                if os.path.isfile(path):
                                    os.remove(path)
                            except Exception:
                                pass
                    # delete docs
                    db["page"].delete_many({"_id": {"$in": [d["_id"] for d in expired]}})
            except Exception:
                # Best-effort cleanup; ignore errors
                pass
            await asyncio.sleep(60)

    asyncio.create_task(cleanup_loop())


@app.get("/")
def read_root():
    return {"message": "Temporary Content Sharing API"}


@app.post("/api/upload")
async def upload_image(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed")
    ext = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
    }.get(file.content_type, "")
    filename = f"{secrets.token_urlsafe(12)}{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)
    url_path = f"/uploads/{filename}"
    return {"url": url_path}


@app.get("/api/proxy-image")
def proxy_image(url: str = Query(..., description="Image URL to mirror into uploads")):
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="Invalid URL")
    try:
        r = requests.get(url, timeout=10, stream=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Could not fetch image")
    if r.status_code != 200:
        raise HTTPException(status_code=400, detail="Image not accessible")
    content_type = r.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="URL is not an image")
    ext = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
    }.get(content_type, "")
    filename = f"{secrets.token_urlsafe(12)}{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    size = 0
    max_bytes = 8 * 1024 * 1024  # 8MB limit
    with open(filepath, "wb") as f:
        for chunk in r.iter_content(8192):
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                try:
                    os.remove(filepath)
                except Exception:
                    pass
                raise HTTPException(status_code=413, detail="Image too large")
            f.write(chunk)
    return {"url": f"/uploads/{filename}"}


def generate_slug(length: int = 8) -> str:
    # base62-like safe slug
    while True:
        slug = secrets.token_urlsafe(length)  # includes -_
        slug = re.sub(r"[^A-Za-z0-9]", "", slug)[:length]
        if slug and db["page"].count_documents({"slug": slug}) == 0:
            return slug


@app.post("/api/pages")
def create_page(payload: PageCreate):
    now = datetime.now(timezone.utc)
    ttl = payload.ttl_seconds or DEFAULT_TTL_SECONDS
    expires_at = now + timedelta(seconds=ttl)
    slug = generate_slug(8)

    doc = {
        "slug": slug,
        "html": payload.html,
        "created_at": now,
        "expires_at": expires_at,
        "assets": payload.assets or [],
    }
    db["page"].insert_one(doc)

    return {
        "slug": slug,
        "url": f"/p/{slug}",
        "expires_at": expires_at.isoformat(),
        "ttl_seconds": ttl,
    }


@app.get("/api/pages/{slug}")
def get_page(slug: str):
    doc = db["page"].find_one({"slug": slug})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    now = datetime.now(timezone.utc)
    if doc["expires_at"] <= now:
        # delete on access
        db["page"].delete_one({"_id": doc["_id"]})
        # try to remove assets
        for asset in doc.get("assets", []) or []:
            relative = asset.lstrip("/")
            path = os.path.join(os.getcwd(), relative)
            if not os.path.isfile(path):
                path = os.path.join(UPLOAD_DIR, os.path.basename(relative))
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass
        raise HTTPException(status_code=410, detail="Expired")

    remaining = int((doc["expires_at"] - now).total_seconds())
    return {
        "slug": doc["slug"],
        "html": doc["html"],
        "expires_at": doc["expires_at"].isoformat(),
        "remaining_seconds": max(0, remaining),
        "assets": doc.get("assets", []),
    }


@app.get("/p/{slug}", response_class=HTMLResponse)
def view_page(slug: str):
    doc = db["page"].find_one({"slug": slug})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    now = datetime.now(timezone.utc)
    if doc["expires_at"] <= now:
        # delete on access
        db["page"].delete_one({"_id": doc["_id"]})
        for asset in doc.get("assets", []) or []:
            relative = asset.lstrip("/")
            path = os.path.join(os.getcwd(), relative)
            if not os.path.isfile(path):
                path = os.path.join(UPLOAD_DIR, os.path.basename(relative))
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass
        return HTMLResponse(status_code=410, content="<html><body><div style='font-family:system-ui;padding:16px'>This temporary page has expired.</div></body></html>")

    # Minimal chrome: plain white background, no borders; render raw HTML exactly
    # Add a tiny unobtrusive timer + copy link in a corner using inline styles
    remaining = int((doc["expires_at"] - now).total_seconds())
    timer_html = (
        """
    <div id=\"_meta\" style=\"position:fixed;right:8px;bottom:8px;z-index:9999;font:12px/1.2 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#444;background:rgba(255,255,255,0.7);backdrop-filter:saturate(1.2) blur(2px);padding:6px 8px;border-radius:6px\">
      <span id=\"_time\">{remaining}</span>s
      <button id=\"_copy\" style=\"margin-left:8px;background:#000;color:#fff;border:none;padding:4px 6px;border-radius:4px;cursor:pointer;font-size:12px\">Copy link</button>
    </div>
    <script>
      (function(){
        var t = {{remaining: __REM__}};
        var el = document.getElementById('_time');
        var iv = setInterval(function(){
          if(!el) return;
          if(t.remaining <= 0){ clearInterval(iv); location.reload(); return; }
          t.remaining -= 1; el.textContent = t.remaining;
        }, 1000);
        document.getElementById('_copy').addEventListener('click', async function(){
          try { await navigator.clipboard.writeText(window.location.href); this.textContent='Copied'; setTimeout(()=>this.textContent='Copy link',1200);} catch(e){}
        });
      })();
    </script>
    """
    ).format(remaining=remaining).replace("__REM__", str(remaining))

    content_html = doc["html"] or ""
    html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset=\"utf-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
        <title>Shared Content</title>
        <style>html,body{{background:#fff;margin:0;padding:0}}img{{max-width:100%;height:auto}}</style>
      </head>
      <body>
        {content_html}
        {timer_html}
      </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    import os as _os
    response["database_url"] = "✅ Set" if _os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if _os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
