"""
server.py — Local REST & WebSocket API Backend untuk Desktop App.
Menyediakan antarmuka async untuk scraping TikTok & Instagram serta export data.
"""

import os
import sys
import asyncio
import copy
import re
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
from pydantic import BaseModel, Field

from core.scraper_tiktok import get_tiktok_posts_in_range, get_all_tiktok_comments
from core.scraper_instagram import (
    create_client, login_instagram, login_by_sessionid,
    get_posts_in_range as get_ig_posts_in_range,
    get_all_comments as get_ig_comments,
    get_instagram_media_url,
    LoginRequiredError as IGLoginRequiredError,
    InstagramCommentFetchError,
    get_session_info as get_ig_session_info,
    clear_session as clear_ig_session,
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

# Flag pembatalan proses scraping (thread-safe)
cancel_event = threading.Event()

# Lindungi state dan proses start agar dua request /api/analyze tidak dapat
# memulai job secara bersamaan. RLock dipakai karena reset_task_state juga
# dipanggil dari blok yang telah memegang lock ini.
task_state_lock = threading.RLock()

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

# Backend hanya bind ke loopback. Batasi CORS ke origin UI lokal yang memang
# digunakan Vite/Tauri agar situs lain tidak dapat membaca hasil atau mengirim
# kredensial Instagram ke endpoint desktop ini.
LOCAL_UI_ORIGINS = [
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
]
LOCAL_WEBSOCKET_ORIGINS = set(LOCAL_UI_ORIGINS) | {
    "http://localhost:8008",
    "http://127.0.0.1:8008",
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_UI_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def reset_task_state(status_msg: str):
    """Reset state task global sebelum memulai proses scraping baru agar data lama langsung terhapus."""
    global current_task_state, main_loop
    now_str = datetime.now().isoformat()
    with task_state_lock:
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

    with task_state_lock:
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
        elif event_type == "cancelled":
            current_task_state["is_running"] = False
            current_task_state["error"] = message
            current_task_state["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "text": f"⊘ {message}",
                "type": "cancelled"
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
    with task_state_lock:
        return copy.deepcopy(current_task_state)


@app.post("/api/cancel")
def cancel_analysis():
    """Batalkan proses scraping yang sedang berjalan."""
    with task_state_lock:
        is_running = current_task_state["is_running"]
    if is_running:
        cancel_event.set()
        return {"status": "cancelling", "message": "Permintaan pembatalan telah dikirim."}
    return {"status": "idle", "message": "Tidak ada proses yang berjalan."}


class AnalyzeRequest(BaseModel):
    platform: str  # "instagram" | "tiktok"
    target: str
    start_date: str  # "DD-MM-YYYY"
    end_date: str    # "DD-MM-YYYY"
    top_n: int = 10
    ig_username: Optional[str] = None
    ig_password: Optional[str] = None
    ig_session_id: Optional[str] = None


class ClearSessionRequest(BaseModel):
    username: str


class ExportRequest(BaseModel):
    top_commenters: List[Dict[str, Any]] = Field(default_factory=list)
    detail_comments: Any = Field(default_factory=list)
    all_comments: List[Dict[str, Any]] = Field(default_factory=list)
    scraped_posts: List[Dict[str, Any]] = Field(default_factory=list)
    summary_stats: Dict[str, Any] = Field(default_factory=dict)
    analysis_diagnostics: Dict[str, Any] = Field(default_factory=dict)
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
    origin = websocket.headers.get("origin")
    if origin and origin not in LOCAL_WEBSOCKET_ORIGINS:
        await websocket.close(code=1008, reason="Origin tidak diizinkan")
        return
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

    platform = req.platform.strip().lower()
    if platform not in {"instagram", "tiktok"}:
        raise HTTPException(status_code=400, detail="Platform harus Instagram atau TikTok")

    if not req.target.strip():
        raise HTTPException(status_code=400, detail="Target akun tidak boleh kosong")

    if platform == "instagram":
        has_session = bool(req.ig_session_id and req.ig_session_id.strip())
        has_username = bool(req.ig_username and req.ig_username.strip())
        has_password = bool(req.ig_password)
        if not has_session and not (has_username and has_password):
            raise HTTPException(
                status_code=400,
                detail="Gunakan Cookie Session ID atau pasangan username dan password Instagram.",
            )
        if not has_session and has_username != has_password:
            raise HTTPException(
                status_code=400,
                detail="Username dan password Instagram harus diisi bersama.",
            )

    init_msg = f"Memulai analisis {platform.upper()} untuk target: @{req.target}"
    with task_state_lock:
        if current_task_state["is_running"]:
            raise HTTPException(
                status_code=409,
                detail="Masih ada proses analisis yang berjalan. Tunggu hingga selesai atau batalkan terlebih dahulu.",
            )
        reset_task_state(init_msg)
        cancel_event.clear()  # Reset flag pembatalan

    def check_cancelled():
        """Cek apakah proses telah dibatalkan oleh user."""
        if cancel_event.is_set():
            raise InterruptedError("Proses dibatalkan oleh pengguna.")

    def run_task():
        try:
            posts = []
            all_comments = []
            analysis_diagnostics = {}

            if platform == "tiktok":
                def on_tiktok_post(item):
                    check_cancelled()
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
                    check_cancelled()
                    sync_broadcast("comment_progress", f"Mengambil komentar postingan {curr}/{total}: {count} komentar", {
                        "current": curr,
                        "total": total,
                        "count": count
                    })

                all_comments = get_all_tiktok_comments(posts, start_date=s_dt, end_date=e_dt, progress_callback=on_tiktok_comm_progress)

            elif platform == "instagram":
                clean_target = req.target.replace("@", "").strip()
                cl = create_client()

                def on_ig_login_log(msg: str):
                    check_cancelled()
                    sync_broadcast("log", msg)

                # 1. Login via Cookie Session ID jika disediakan
                if req.ig_session_id and req.ig_session_id.strip():
                    user_tag = req.ig_username.replace("@", "").strip() if req.ig_username else "session_user"
                    sync_broadcast("status", "Menghubungkan ke Instagram via Cookie Session ID...")
                    try:
                        login_by_sessionid(cl, req.ig_session_id, username=user_tag, progress_callback=on_ig_login_log)
                    except IGLoginRequiredError as le:
                        sync_broadcast("error", str(le))
                        return
                    except Exception as e:
                        sync_broadcast("error", f"Gagal login via Session ID: {str(e)}")
                        return

                # 2. Login via Username & Password
                elif req.ig_username and req.ig_password:
                    clean_user = req.ig_username.replace("@", "").strip()
                    sync_broadcast("status", f"Menghubungkan ke Instagram sebagai @{clean_user}...")
                    try:
                        logged_in = login_instagram(cl, clean_user, req.ig_password, progress_callback=on_ig_login_log)
                    except IGLoginRequiredError as le:
                        sync_broadcast("error", str(le))
                        return
                    except Exception as e:
                        sync_broadcast("error", f"Gagal login Instagram: {str(e)}")
                        return

                    if not logged_in:
                        sync_broadcast("error", "Login Instagram gagal. Periksa username dan password Anda.")
                        return
                else:
                    sync_broadcast("error", "Instagram memerlukan Username & Password ATAU Cookie Session ID untuk login.")
                    return

                def on_ig_post_log(msg: Any):
                    check_cancelled()
                    if isinstance(msg, str):
                        sync_broadcast("log", msg)
                    else:
                        post_date = msg.taken_at.strftime("%d-%m-%Y %H:%M") if hasattr(msg, 'taken_at') and msg.taken_at else ""
                        raw_likes = getattr(msg, "like_count", None)
                        like_text = str(raw_likes) if raw_likes is not None else "tidak tersedia"
                        sync_broadcast("post_found", f"Ditemukan postingan: {post_date} ({like_text} likes)", {"id": str(msg.pk)})

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
                    check_cancelled()
                    display_text = msg_text or f"Mengambil komentar postingan {curr}/{total} (Total: {total_comms} komentar)"
                    sync_broadcast("comment_progress", display_text, {
                        "current": curr,
                        "total": total,
                        "count": total_comms
                    })

                try:
                    all_comments = get_ig_comments(cl, posts, progress_callback=on_ig_comm_progress)
                except IGLoginRequiredError as exc:
                    sync_broadcast("error", str(exc))
                    return
                except InstagramCommentFetchError as exc:
                    sync_broadcast("error", str(exc))
                    return
                analysis_diagnostics = getattr(cl, "_instagram_job_diagnostics", {}) or {}

            # Analisis data
            sync_broadcast("status", "Menghitung peringkat top commenters & statistik...")
            top_commenters = count_top_commenters(all_comments, req.top_n)
            summary = get_summary_stats(all_comments, len(posts), posts=posts)
            top_usernames = [c["username"] for c in top_commenters]
            detail_comments = get_detailed_comments_by_user(all_comments, top_usernames)

            # Serialisasi daftar postingan ke format dict seragam
            scraped_posts = []
            for p in posts:
                if isinstance(p, dict):
                    # TikTok posts sudah berupa dict
                    scraped_posts.append({
                        "post_url": p.get("post_url", ""),
                        "post_likes": p.get("post_likes"),
                        "post_date": p.get("post_date", "N/A"),
                        "post_caption": p.get("post_caption", ""),
                    })
                else:
                    # Instagram media objects
                    post_code = getattr(p, 'code', '') or str(getattr(p, 'pk', ''))
                    taken_at_str = p.taken_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(p, 'taken_at') and p.taken_at else "N/A"
                    caption = getattr(p, 'caption_text', '') or ''
                    scraped_posts.append({
                        "post_url": get_instagram_media_url(p),
                        "post_likes": getattr(p, "like_count", None),
                        "post_date": taken_at_str,
                        "post_caption": caption,
                    })

            # Kirim hasil lengkap ke frontend
            sync_broadcast("completed", "Analisis berhasil selesai!", {
                "top_commenters": top_commenters,
                "summary": summary,
                "detail_comments": detail_comments,
                "all_comments": all_comments,
                "scraped_posts": scraped_posts,
                "total_posts": len(posts),
                "total_comments": len(all_comments),
                "diagnostics": analysis_diagnostics,
            })

        except InterruptedError:
            sync_broadcast("cancelled", "Proses scraping dibatalkan oleh pengguna.")
        except Exception as e:
            sync_broadcast("error", f"Terjadi kesalahan: {str(e)}")

    thread = threading.Thread(target=run_task, daemon=True)
    thread.start()

    return {"status": "started", "message": "Proses analisis telah dimulai"}


@app.post("/api/export")
def export_results(req: ExportRequest):
    """Export hasil analisis ke file Excel."""
    try:
        date_str = re.sub(r"[^0-9_]", "", f"{req.start_date}_{req.end_date}".replace("-", ""))
        safe_plat = re.sub(r"[^\w-]", "_", req.platform.lower()).strip("_-") or "platform"
        safe_user = re.sub(
            r"[^\w-]",
            "_",
            req.target_username.replace("@", "").strip(),
        ).strip("_-") or "target"
        default_name = (
            Path(req.filename).name
            if req.filename
            else f"top_commenters_{safe_plat}_{safe_user}_{date_str}.xlsx"
        )
        if not default_name or default_name in {".", ".."}:
            raise HTTPException(status_code=400, detail="Nama file export tidak valid")
        
        # Simpan di folder desktop_app/exports
        export_dir = APP_DIR / "exports"
        export_dir.mkdir(exist_ok=True)
        file_path = export_dir / default_name

        saved_path = export_to_excel(
            top_commenters=req.top_commenters,
            detail_comments=req.detail_comments,
            all_comments=req.all_comments,
            scraped_posts=req.scraped_posts,
            summary_stats=req.summary_stats,
            target_username=safe_user,
            start_date=req.start_date,
            end_date=req.end_date,
            platform=req.platform,
            filename=str(file_path),
            analysis_diagnostics=req.analysis_diagnostics,
        )

        return {
            "status": "success",
            "file_path": str(saved_path),
            "filename": Path(saved_path).name
        }
    except HTTPException:
        raise
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


@app.get("/api/instagram/session-status")
def check_ig_session_status(username: str):
    """Cek apakah ada file sesi Instagram yang tersimpan untuk username ini."""
    try:
        return get_ig_session_info(username)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/instagram/clear-session")
def clear_ig_session_endpoint(req: ClearSessionRequest):
    """Hapus file sesi Instagram untuk username tertentu."""
    try:
        cleared = clear_ig_session(req.username)
        return {"status": "success", "cleared": cleared, "message": f"Sesi untuk @{req.username} telah dihapus"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Mount frontend static distribution
FRONTEND_DIST = APP_DIR / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8008)
