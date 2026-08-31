"""
server.py — Local REST & WebSocket API Backend untuk Desktop App.
Menyediakan antarmuka async untuk scraping TikTok & Instagram serta export data.
"""

import os
import sys
import asyncio
import threading
import webbrowser
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path
from contextlib import asynccontextmanager

# Pastikan direktori desktop_app lokal berada di sys.path
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Pastikan Playwright browser path terhubung ke default lokal jika berjalan dalam PyInstaller
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ or os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0":
    _local_appdata = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(_local_appdata, "ms-playwright")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.scraper_tiktok import get_tiktok_posts_in_range, get_all_tiktok_comments
from core.scraper_instagram import (
    create_client, login_instagram,
    get_posts_in_range as get_ig_posts_in_range,
    get_all_comments as get_ig_comments,
    LoginRequiredError as IGLoginRequiredError,
)
from core.analyzer import count_top_commenters, get_detailed_comments_by_user, get_summary_stats
from core.exporter import export_to_excel

from fastapi.staticfiles import StaticFiles

# State Pelacakan Progress Terpusat (Bisa diakses via Polling & WebSocket)
current_task_state: Dict[str, Any] = {
    "is_running": False,
    "status": "Siap untuk memulai analisis",
    "progress_percent": 0,
    "logs": [],
    "result": None,
    "error": None,
    "last_updated": datetime.now().isoformat()
}

# WebSocket Manager untuk Live Progress Logging
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, data: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(data)
            except Exception:
                self.disconnect(connection)

manager = ConnectionManager()
main_loop: Optional[asyncio.AbstractEventLoop] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    yield

app = FastAPI(title="Social Scraper Desktop API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def reset_task_state(status_msg: str):
    """Reset state task global sebelum memulai proses scraping baru agar data lama langsung terhapus."""
    global current_task_state, main_loop
    now_str = datetime.now().isoformat()
    current_task_state["is_running"] = True
    current_task_state["status"] = status_msg
    current_task_state["progress_percent"] = 0
    current_task_state["logs"] = [{
        "time": datetime.now().strftime("%H:%M:%S"),
        "text": status_msg,
        "type": "info"
    }]
    current_task_state["result"] = None
    current_task_state["links_result"] = None
    current_task_state["error"] = None
    current_task_state["last_updated"] = now_str

    if main_loop and main_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(manager.broadcast({
                "type": "started",
                "message": status_msg,
                "payload": None,
                "timestamp": now_str
            }), main_loop)
        except Exception:
            pass


def sync_broadcast(event_type: str, message: str, payload: Any = None):
    """Kirim event ke frontend secara thread-safe dan update state global."""
    global main_loop, current_task_state
    now_str = datetime.now().isoformat()
    data = {
        "type": event_type,
        "message": message,
        "payload": payload,
        "timestamp": now_str
    }

    # Update state global untuk polling fallback
    current_task_state["last_updated"] = now_str
    current_task_state["status"] = message

    if event_type in ("status", "log"):
        current_task_state["logs"].append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "text": message,
            "type": "info" if event_type == "status" else "log"
        })
    elif event_type == "post_found":
        current_task_state["logs"].append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "text": message,
            "type": "success"
        })
    elif event_type == "comment_progress":
        if payload and payload.get("total", 0) > 0:
            current_task_state["progress_percent"] = round((payload["current"] / payload["total"]) * 100)
        current_task_state["logs"].append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "text": message,
            "type": "info"
        })
    elif event_type == "completed":
        current_task_state["is_running"] = False
        current_task_state["progress_percent"] = 100
        current_task_state["result"] = payload
        current_task_state["logs"].append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "text": f"✓ {message}",
            "type": "completed"
        })
    elif event_type == "error":
        current_task_state["is_running"] = False
        current_task_state["error"] = message
        current_task_state["logs"].append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "text": f"✗ {message}",
            "type": "error"
        })

    # Batasi riwayat log maksimal 200 baris
    if len(current_task_state["logs"]) > 200:
        current_task_state["logs"] = current_task_state["logs"][-200:]

    if main_loop and main_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(manager.broadcast(data), main_loop)
        except Exception:
            pass


@app.get("/api/progress")
def get_progress_state():
    """Endpoint polling progress untuk memastikan UI tidak pernah freeze."""
    return current_task_state


class AnalyzeRequest(BaseModel):
    platform: str  # "instagram" | "tiktok"
    target: str
    start_date: str  # "DD-MM-YYYY"
    end_date: str    # "DD-MM-YYYY"
    top_n: int = 10
    ig_username: Optional[str] = None
    ig_password: Optional[str] = None


class ExportRequest(BaseModel):
    top_commenters: List[Dict[str, Any]] = []
    detail_comments: Any = []
    summary_stats: Dict[str, Any] = {}
    target_username: str = ""
    start_date: str = ""
    end_date: str = ""
    platform: str = "Instagram"
    filename: Optional[str] = None


class OpenUrlRequest(BaseModel):
    url: str


@app.get("/api/health")
def health_check():
    return {"status": "ok", "message": "Backend engine is running"}


@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.post("/api/analyze")
async def run_analysis(req: AnalyzeRequest):
    """Jalankan scraping & analisis secara asynchronous."""
    try:
        s_dt = datetime.strptime(req.start_date, "%d-%m-%Y")
        e_dt = datetime.strptime(req.end_date, "%d-%m-%Y").replace(hour=23, minute=59, second=59)
    except ValueError:
        raise HTTPException(status_code=400, detail="Format tanggal salah. Gunakan DD-MM-YYYY")

    if s_dt > e_dt:
        raise HTTPException(status_code=400, detail="Tanggal mulai tidak boleh lebih besar dari tanggal akhir")

    init_msg = f"Memulai analisis {req.platform.upper()} untuk target: @{req.target}"
    reset_task_state(init_msg)

    def run_task():
        try:
            posts = []
            all_comments = []

            if req.platform.lower() == "tiktok":
                def on_tiktok_post(item):
                    if isinstance(item, str):
                        sync_broadcast("status", item)
                        sync_broadcast("log", item)
                    else:
                        sync_broadcast("post_found", f"Ditemukan postingan: {item.get('post_date', '')} ({item.get('post_likes', 0)} likes)", item)

                sync_broadcast("status", f"Menghubungkan ke profil TikTok @{req.target}...")
                posts = get_tiktok_posts_in_range(req.target, s_dt, e_dt, progress_callback=on_tiktok_post)

                if not posts:
                    sync_broadcast("error", "Tidak ada postingan ditemukan dalam rentang tanggal ini.")
                    return

                sync_broadcast("status", f"Ditemukan {len(posts)} postingan. Mengambil komentar...")

                def on_tiktok_comm_progress(curr, total, item, count):
                    sync_broadcast("comment_progress", f"Mengambil komentar postingan {curr}/{total}: {count} komentar", {
                        "current": curr,
                        "total": total,
                        "count": count
                    })

                all_comments = get_all_tiktok_comments(posts, start_date=s_dt, end_date=e_dt, progress_callback=on_tiktok_comm_progress)

            elif req.platform.lower() == "instagram":
                clean_target = req.target.replace("@", "").strip()
                if not req.ig_username or not req.ig_password:
                    sync_broadcast("error", "Instagram memerlukan username & password login akun.")
                    return

                def on_ig_login_log(msg: str):
                    sync_broadcast("status", msg)
                    sync_broadcast("log", msg)

                sync_broadcast("status", f"Menghubungkan ke Instagram sebagai @{req.ig_username}...")
                cl = create_client()

                try:
                    logged_in = login_instagram(cl, req.ig_username, req.ig_password, progress_callback=on_ig_login_log)
                except IGLoginRequiredError as le:
                    sync_broadcast("error", str(le))
                    return
                except Exception as e:
                    sync_broadcast("error", f"Gagal login Instagram: {str(e)}")
                    return

                if not logged_in:
                    sync_broadcast("error", "Login Instagram gagal. Periksa username dan password Anda.")
                    return

                def on_ig_post_log(msg: Any):
                    if isinstance(msg, str):
                        sync_broadcast("status", msg)
                        sync_broadcast("log", msg)
                    else:
                        post_date = msg.taken_at.strftime("%d-%m-%Y %H:%M") if hasattr(msg, 'taken_at') and msg.taken_at else ""
                        sync_broadcast("post_found", f"Ditemukan postingan: {post_date} ({getattr(msg, 'like_count', 0)} likes)", {"id": str(msg.pk)})

                try:
                    sync_broadcast("status", f"Mengambil daftar postingan @{clean_target}...")
                    posts = get_ig_posts_in_range(cl, clean_target, s_dt, e_dt, progress_callback=on_ig_post_log)
                except IGLoginRequiredError as le:
                    sync_broadcast("error", str(le))
                    return
                except Exception as e:
                    sync_broadcast("error", f"Gagal mengambil postingan Instagram: {str(e)}")
                    return

                if not posts:
                    sync_broadcast("error", f"Tidak ada postingan Instagram ditemukan untuk @{clean_target} dalam rentang tanggal {req.start_date} s/d {req.end_date}.")
                    return

                sync_broadcast("status", f"Ditemukan {len(posts)} postingan dalam rentang tanggal. Mengambil komentar...")

                def on_ig_comm_progress(curr, total, media, total_comms, msg_text=""):
                    display_text = msg_text or f"Mengambil komentar postingan {curr}/{total} (Total: {total_comms} komentar)"
                    sync_broadcast("comment_progress", display_text, {
                        "current": curr,
                        "total": total,
                        "count": total_comms
                    })
                    if msg_text:
                        sync_broadcast("log", msg_text)

                all_comments = get_ig_comments(cl, posts, progress_callback=on_ig_comm_progress)

            # Analisis data
            sync_broadcast("status", "Menghitung peringkat top commenters & statistik...")
            top_commenters = count_top_commenters(all_comments, req.top_n)
            summary = get_summary_stats(all_comments, len(posts))
            top_usernames = [c["username"] for c in top_commenters]
            detail_comments = get_detailed_comments_by_user(all_comments, top_usernames)

            # Kirim hasil lengkap ke frontend
            sync_broadcast("completed", "Analisis berhasil selesai!", {
                "top_commenters": top_commenters,
                "summary": summary,
                "detail_comments": detail_comments,
                "total_posts": len(posts),
                "total_comments": len(all_comments),
            })

        except Exception as e:
            sync_broadcast("error", f"Terjadi kesalahan: {str(e)}")

    thread = threading.Thread(target=run_task, daemon=True)
    thread.start()

    return {"status": "started", "message": "Proses analisis telah dimulai"}


@app.post("/api/export")
def export_results(req: ExportRequest):
    """Export hasil analisis ke file Excel."""
    try:
        date_str = f"{req.start_date}_{req.end_date}".replace("-", "")
        safe_plat = req.platform.lower().replace(" ", "_")
        safe_user = req.target_username.replace("@", "").strip() or "target"
        default_name = req.filename or f"top_commenters_{safe_plat}_{safe_user}_{date_str}.xlsx"
        
        # Simpan di folder desktop_app/exports
        export_dir = APP_DIR / "exports"
        export_dir.mkdir(exist_ok=True)
        file_path = export_dir / default_name

        saved_path = export_to_excel(
            top_commenters=req.top_commenters,
            detail_comments=req.detail_comments,
            summary_stats=req.summary_stats,
            target_username=safe_user,
            start_date=req.start_date,
            end_date=req.end_date,
            platform=req.platform,
            filename=str(file_path),
        )

        return {
            "status": "success",
            "file_path": str(saved_path),
            "filename": Path(saved_path).name
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal export Excel: {str(e)}")


@app.post("/api/open-folder")
def open_folder(path: Optional[str] = None):
    """Buka file explorer di folder output."""
    try:
        target_dir = Path(path).parent if path else APP_DIR / "exports"
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
        
        if sys.platform == "win32":
            os.startfile(str(target_dir))
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/open-url")
def open_external_url(req: OpenUrlRequest):
    """Buka URL postingan atau profil di browser default sistem."""
    try:
        url = req.url.strip()
        if not url:
            raise HTTPException(status_code=400, detail="URL tidak boleh kosong")
        
        if not (url.startswith("http://") or url.startswith("https://")):
            url = f"https://{url}"

        webbrowser.open(url)
        return {"status": "success", "url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal membuka URL: {str(e)}")


# Mount frontend static distribution
FRONTEND_DIST = APP_DIR / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8008)
