"""
scraper_tiktok.py — Modul otomatis untuk scraping TikTok (postingan gambar/video & komentar).

Menggunakan Playwright browser dengan persistent profile, auto-detection puzzle captcha,
dynamic scrolling, dan ekstraksi timestamp Snowflake 64-bit untuk menyaring postingan
secara presisi sesuai rentang tanggal.
"""

import os
import json
import re
import time
import requests
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path

from playwright.sync_api import sync_playwright


class TikTokScraperError(Exception):
    """Raised jika terjadi error saat scraping TikTok."""
    pass


MOBILE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
}

DESKTOP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
}


def extract_video_id(input_str: str) -> Optional[tuple[str, str]]:
    """
    Ekstrak post ID dan post type ('photo' atau 'video') dari URL atau string.

    Returns:
        Tuple (post_id, post_type) atau None.
    """
    match_photo = re.search(r'/photo/(\d+)', input_str)
    if match_photo:
        return match_photo.group(1), "photo"

    match_video = re.search(r'/video/(\d+)', input_str)
    if match_video:
        return match_video.group(1), "video"

    if input_str.strip().isdigit():
        return input_str.strip(), "video"

    return None


def extract_timestamp_from_post_id(post_id: str | int) -> Optional[datetime]:
    """
    Ekstrak estimasi waktu publish dari ID postingan TikTok 64-bit Snowflake.
    ID TikTok menyimpan Unix epoch timestamp (dalam detik) pada 32 bit paling signifikan.

    Args:
        post_id: ID postingan TikTok (angka 64-bit).

    Returns:
        datetime object waktu posting dibuat, atau None jika gagal.
    """
    try:
        pid = int(str(post_id).strip())
        ts = pid >> 32
        # Validasi rentang timestamp yang masuk akal: 2016-01-01 s/d 2035-01-01
        if 1451606400 <= ts <= 2051222400:
            return datetime.fromtimestamp(ts)
    except Exception:
        pass
    return None


def get_tiktok_post_details(session: requests.Session, post_id: str, post_type: str = "video", username: str = "user") -> dict:
    """
    Ambil tanggal publish asli, caption, dan jumlah like dari satu postingan TikTok.

    Args:
        session: Instance requests.Session.
        post_id: ID postingan.
        post_type: Tipe postingan ('photo' atau 'video').
        username: Username pembuat postingan.

    Returns:
        Dict berisi post_date, post_caption, dan post_likes.
    """
    url = f"https://www.tiktok.com/@{username}/{post_type}/{post_id}"
    approx_dt = extract_timestamp_from_post_id(post_id)
    approx_str = approx_dt.strftime("%Y-%m-%d %H:%M:%S") if approx_dt else "N/A"

    try:
        r = session.get(url, timeout=10)
        if r.status_code != 200:
            return {"post_date": approx_str, "post_caption": "", "post_likes": 0}

        post_likes = 0

        # 1. Parse JSON rehydration data jika ada
        match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                scope = data.get("__DEFAULT_SCOPE__", {})

                def find_item_struct(obj):
                    if isinstance(obj, dict):
                        if "itemStruct" in obj and isinstance(obj["itemStruct"], dict):
                            return obj["itemStruct"]
                        if "desc" in obj and "createTime" in obj and ("id" in obj or "author" in obj):
                            return obj
                        for v in obj.values():
                            res = find_item_struct(v)
                            if res:
                                return res
                    elif isinstance(obj, list):
                        for elem in obj:
                            res = find_item_struct(elem)
                            if res:
                                return res
                    return None

                item = find_item_struct(scope)
                if item:
                    c_time = int(item.get("createTime", 0))
                    dt_str = datetime.fromtimestamp(c_time).strftime("%Y-%m-%d %H:%M:%S") if c_time else approx_str
                    caption = item.get("desc", "") or ""
                    stats = item.get("stats", {}) or item.get("statsV2", {})
                    post_likes = int(stats.get("diggCount", 0)) if stats.get("diggCount") else 0
                    return {"post_date": dt_str, "post_caption": caption, "post_likes": post_likes}
            except Exception:
                pass

        # 2. Regex khusus itemStruct desc
        item_struct_match = re.search(r'"itemStruct":\{[^{}]*"desc":"([^"]+)"', r.text) or re.search(r'"desc":"([^"]+)"[^{}]*"createTime"', r.text)
        caption = item_struct_match.group(1) if item_struct_match else ""

        # Filter jika caption hasil regex adalah reply comment
        if "comment:" in caption.lower():
            desc_all = re.findall(r'"desc":"([^"]+)"', r.text)
            for d in desc_all:
                if "comment:" not in d.lower() and "point of view" not in d.lower() and "Lihat video" not in d:
                    caption = d
                    break

        c_time_match = re.search(r'"createTime":"?(\d+)"?', r.text)
        c_time = int(c_time_match.group(1)) if c_time_match else 0
        dt_str = datetime.fromtimestamp(c_time).strftime("%Y-%m-%d %H:%M:%S") if c_time else approx_str

        digg_match = re.search(r'"diggCount":\s*(\d+)', r.text)
        if digg_match:
            post_likes = int(digg_match.group(1))

        return {"post_date": dt_str, "post_caption": caption, "post_likes": post_likes}
    except Exception:
        return {"post_date": approx_str, "post_caption": "", "post_likes": 0}


def auto_scrape_tiktok_profile_posts(
    username: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    progress_callback: Optional[callable] = None,
) -> list[dict]:
    """
    Scrape seluruh postingan (video & foto) dari profil TikTok secara otomatis menggunakan Playwright.
    Mendukung browser persistent context dan auto-detection puzzle slider captcha.

    Args:
        username: Username TikTok target (tanpa @).
        start_date: Tanggal mulai filter (opsional, untuk early termination saat scroll).
        end_date: Tanggal akhir filter (opsional).
        progress_callback: Callback(msg) saat proses scanning & scroll berlangsung.

    Returns:
        List of dict containing post info (id, post_type, post_url, target_username, approx_date).
    """
    clean_username = username.strip().lstrip("@")
    url = f"https://www.tiktok.com/@{clean_username}"
    
    # Gunakan direktori .tiktok_browser_profile utama di workspace root
    base_proj_dir = Path(__file__).resolve().parents[2] if len(Path(__file__).resolve().parents) > 2 else Path(__file__).resolve().parent
    user_data_dir = str(base_proj_dir / ".tiktok_browser_profile")

    posts_dict = {}

    def is_captcha_present(page) -> bool:
        try:
            cur_body = page.inner_text("body").lower()
            cur_html = page.content().lower()
            return (
                "tarik penggeser" in cur_body
                or "drag the slider" in cur_body
                or "puzzle" in cur_body
                or "fit the puzzle" in cur_body
                or "secsdk-captcha" in cur_html
            )
        except Exception:
            return False

    def collect_from_page(page) -> int:
        new_count = 0
        try:
            # 1. Dari seluruh anchor tag di DOM
            hrefs = page.evaluate("""() => {
                const anchors = Array.from(document.querySelectorAll('a'));
                return anchors.map(a => a.href).filter(h => h && (h.includes('/video/') || h.includes('/photo/')));
            }""")

            for h in hrefs:
                res = extract_video_id(h)
                if res:
                    p_id, p_type = res
                    if p_id not in posts_dict:
                        approx_dt = extract_timestamp_from_post_id(p_id)
                        posts_dict[p_id] = {
                            "id": p_id,
                            "post_type": p_type,
                            "post_url": f"https://www.tiktok.com/@{clean_username}/{p_type}/{p_id}",
                            "target_username": clean_username,
                            "approx_date": approx_dt,
                        }
                        new_count += 1

            # 2. Dari konten HTML regex (menangkap video/photo IDs yang belum berupa rendered link)
            content = page.content()
            for pid in re.findall(r'/photo/(\d+)', content):
                if pid not in posts_dict:
                    approx_dt = extract_timestamp_from_post_id(pid)
                    posts_dict[pid] = {
                        "id": pid,
                        "post_type": "photo",
                        "post_url": f"https://www.tiktok.com/@{clean_username}/photo/{pid}",
                        "target_username": clean_username,
                        "approx_date": approx_dt,
                    }
                    new_count += 1

            for pid in re.findall(r'/video/(\d+)', content):
                if pid not in posts_dict:
                    approx_dt = extract_timestamp_from_post_id(pid)
                    posts_dict[pid] = {
                        "id": pid,
                        "post_type": "video",
                        "post_url": f"https://www.tiktok.com/@{clean_username}/video/{pid}",
                        "target_username": clean_username,
                        "approx_date": approx_dt,
                    }
                    new_count += 1
        except Exception:
            pass
        return new_count

    def _launch_browser_context(playwright_inst, is_headless: bool):
        """Luncurkan browser context dengan fallback channel (msedge -> chrome -> chromium)."""
        if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ or os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0":
            local_app = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(local_app, "ms-playwright")

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ]
        browser_viewport = {"width": 1280, "height": 900}
        browser_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

        channels = ["msedge", "chrome", None]
        last_error = None

        for ch in channels:
            try:
                launch_opts = {
                    "user_data_dir": user_data_dir,
                    "headless": is_headless,
                    "args": browser_args,
                    "viewport": browser_viewport,
                    "locale": "id-ID",
                    "user_agent": browser_ua,
                }
                if ch:
                    launch_opts["channel"] = ch
                ctx = playwright_inst.chromium.launch_persistent_context(**launch_opts)
                ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                return ctx
            except Exception as e:
                last_error = e
                continue

        raise TikTokScraperError(f"Gagal meluncurkan browser (Edge/Chrome/Chromium): {str(last_error)}")

    try:
        with sync_playwright() as p:
            # Percobaan 1: Headless persistent context
            if progress_callback:
                progress_callback("Membuka browser Playwright...")

            context = _launch_browser_context(p, is_headless=True)
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=35000)
            except Exception:
                pass

            time.sleep(3)
            has_captcha = is_captcha_present(page)

            # Jika terdeteksi puzzle captcha, buka browser visible agar user bisa menyelesaikan puzzle
            if has_captcha:
                if progress_callback:
                    progress_callback("! Terdeteksi verifikasi puzzle TikTok -- Membuka browser visual...")
                context.close()

                context = _launch_browser_context(p, is_headless=False)
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=40000)
                except Exception:
                    pass

                # Tunggu verifikasi diselesaikan (hingga 60 detik)
                captcha_solved = False
                for i in range(30):
                    time.sleep(2)
                    if not is_captcha_present(page):
                        captcha_solved = True
                        if progress_callback:
                            progress_callback("✓ Puzzle captcha berhasil diselesaikan! Melanjutkan pengambilan postingan...")
                        break
                    else:
                        if progress_callback and i % 3 == 0:
                            progress_callback("! Silakan geser puzzle slider di jendela Chrome yang muncul...")

                if not captcha_solved:
                    context.close()
                    raise TikTokScraperError(
                        "Verifikasi puzzle captcha TikTok belum diselesaikan tepat waktu. "
                        "Silakan geser slider puzzle pada jendela Chrome yang terbuka, atau gunakan fitur Paste Link Video TikTok."
                    )

            # Kumpulkan postingan awal
            collect_from_page(page)

            # Lakukan dynamic scrolling
            max_scrolls = 50
            scroll_count = 0
            no_new_count = 0
            cutoff_date = (start_date - timedelta(days=1)) if start_date else None

            while scroll_count < max_scrolls:
                scroll_count += 1
                page.evaluate("window.scrollBy(0, 1500)")
                time.sleep(1.2)

                new_found = collect_from_page(page)

                if progress_callback:
                    progress_callback(f"Scroll #{scroll_count}: {len(posts_dict)} postingan terdeteksi...")

                if new_found == 0:
                    no_new_count += 1
                    if no_new_count == 2:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        time.sleep(2.0)
                        collect_from_page(page)
                    elif no_new_count >= 5 and len(posts_dict) > 0:
                        # 5x scroll berturut-turut tanpa postingan baru -> akhir halaman profil
                        break
                    elif no_new_count >= 5 and len(posts_dict) == 0:
                        # Jika sama sekali tidak ada postingan, cek apakah terhalang captcha
                        if is_captcha_present(page):
                            context.close()
                            raise TikTokScraperError("TikTok memblokir permintaan dengan puzzle captcha. Silakan coba lagi dan selesaikan puzzle di browser visual.")
                        break
                else:
                    no_new_count = 0

                # Early termination check: jika postingan paling lama sudah melewati start_date
                if cutoff_date and len(posts_dict) >= 15:
                    dated_posts = [p for p in posts_dict.values() if p.get("approx_date")]
                    if len(dated_posts) >= 12:
                        dated_posts.sort(key=lambda x: x["approx_date"], reverse=True)
                        oldest_posts = dated_posts[-6:]
                        if all(p["approx_date"] < cutoff_date for p in oldest_posts):
                            break

            context.close()

    except TikTokScraperError:
        raise
    except Exception as e:
        if progress_callback:
            progress_callback(f"Playwright: {e}")
        raise TikTokScraperError(f"Gagal memuat profil TikTok: {str(e)}")

    return list(posts_dict.values())


def get_tiktok_posts_in_range(
    target_input: str,
    start_date: datetime,
    end_date: datetime,
    progress_callback: Optional[callable] = None,
) -> list[dict]:
    """
    Ambil postingan TikTok (video/foto) yang dipublish dalam rentang waktu start_date s/d end_date.

    Args:
        target_input: Username TikTok atau Link/ID postingan.
        start_date: Tanggal mulai.
        end_date: Tanggal akhir.
        progress_callback: Callback(item) saat postingan valid ditemukan.

    Returns:
        List of dict item postingan yang valid dan sesuai rentang tanggal.
    """
    session = requests.Session()
    session.headers.update(MOBILE_HEADERS)

    candidate_posts = []

    is_direct_input = (
        "/video/" in target_input
        or "/photo/" in target_input
        or target_input.strip().isdigit()
        or ("," in target_input and any(s.strip().isdigit() or "/video/" in s or "/photo/" in s for s in target_input.split(",")))
    )

    if is_direct_input:
        inputs = [v.strip() for v in target_input.split(",") if v.strip()]
        for inp in inputs:
            res = extract_video_id(inp)
            if res:
                p_id, p_type = res
                clean_username = "user"
                user_match = re.search(r'@([^/?#]+)', inp)
                if user_match:
                    clean_username = user_match.group(1)

                candidate_posts.append({
                    "id": p_id,
                    "post_type": p_type,
                    "post_url": f"https://www.tiktok.com/@{clean_username}/{p_type}/{p_id}",
                    "target_username": clean_username,
                    "approx_date": extract_timestamp_from_post_id(p_id),
                })
    else:
        clean_username = target_input.strip().lstrip("@")
        candidate_posts = auto_scrape_tiktok_profile_posts(
            clean_username,
            start_date=start_date,
            end_date=end_date,
            progress_callback=progress_callback,
        )

    filtered_posts = []
    # Filter postingan yang berada dalam rentang tanggal berdasarkan snowflake timestamp (akurasi detik)
    for post_item in candidate_posts:
        p_id = str(post_item["id"])
        approx_dt = post_item.get("approx_date") or extract_timestamp_from_post_id(p_id)
        if approx_dt and start_date <= approx_dt <= end_date:
            p_date_str = approx_dt.strftime("%Y-%m-%d %H:%M:%S")
            post_item["approx_date"] = approx_dt
            post_item["post_date"] = p_date_str
            post_item["post_caption"] = post_item.get("post_caption", "")
            post_item["post_likes"] = post_item.get("post_likes", 0)
            post_item["start_date"] = start_date
            post_item["end_date"] = end_date
            filtered_posts.append(post_item)
            if progress_callback:
                progress_callback(post_item)

    if progress_callback:
        progress_callback(f"Berhasil memilih {len(filtered_posts)} postingan dalam rentang tanggal.")

    # Urutkan postingan berdasarkan tanggal publish terbaru (descending)
    filtered_posts.sort(key=lambda x: x.get("post_date", ""), reverse=True)

    return filtered_posts


def get_tiktok_comments_from_post(
    session: requests.Session,
    item: dict,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> list[dict]:
    """
    Ambil komentar dari satu postingan TikTok (video atau foto).

    Args:
        session: Instance requests.Session.
        item: Dict item postingan (berisi id, post_type, post_url, target_username, post_date, post_caption, post_likes).
        start_date: Tanggal mulai filter.
        end_date: Tanggal akhir filter.

    Returns:
        List of dict data komentar.
    """
    v_id = str(item.get("id"))
    username = item.get("target_username", "user")
    p_type = item.get("post_type", "video")
    post_url = item.get("post_url") or f"https://www.tiktok.com/@{username}/{p_type}/{v_id}"

    # Gunakan metadata yang sudah di-fetch atau fetch baru jika belum ada
    post_date_str = item.get("post_date")
    post_caption_str = item.get("post_caption")
    post_likes = item.get("post_likes")

    if not post_date_str or post_date_str == "N/A":
        post_info = get_tiktok_post_details(session, v_id, p_type, username)
        post_date_str = post_info.get("post_date", "N/A")
        post_caption_str = post_info.get("post_caption", "")
        post_likes = post_info.get("post_likes", 0)

    comments = []
    cursor = 0
    max_comments = 150

    while len(comments) < max_comments:
        comm_url = f"https://www.tiktok.com/api/comment/list/?aid=1988&aweme_id={v_id}&count=50&cursor={cursor}"
        try:
            r = session.get(comm_url, timeout=10)
            if r.status_code != 200:
                break

            data = r.json()
            raw_comments = data.get("comments", [])
            if not raw_comments:
                break

            for c in raw_comments:
                user_obj = c.get("user", {}) or {}
                c_user = user_obj.get("unique_id") or user_obj.get("nickname") or "unknown"
                c_text = c.get("text", "")
                c_ts = c.get("create_time", 0)
                c_likes = int(c.get("digg_count", 0)) if c.get("digg_count") else 0
                c_date_str = datetime.fromtimestamp(c_ts).strftime("%Y-%m-%d %H:%M:%S") if c_ts else "N/A"

                comments.append({
                    "commenter_username": c_user,
                    "comment_text": c_text,
                    "has_liked_post": "N/A",
                    "comment_likes": c_likes,
                    "comment_date": c_date_str,
                    "post_shortcode": str(v_id),
                    "post_url": post_url,
                    "post_likes": post_likes,
                    "post_date": post_date_str,
                    "post_caption": (post_caption_str[:100] + "...") if len(post_caption_str) > 100 else post_caption_str,
                })

            cursor = data.get("cursor", 0)
            has_more = data.get("has_more", 0)
            if not has_more:
                break

        except Exception:
            break

    return comments


def get_all_tiktok_comments(
    posts: list[dict],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    progress_callback: Optional[callable] = None,
) -> list[dict]:
    """
    Ambil komentar dari semua postingan TikTok.

    Args:
        posts: List of post items.
        start_date: Tanggal mulai filter komentar.
        end_date: Tanggal akhir filter komentar.
        progress_callback: Callback (current, total, item, comment_count).

    Returns:
        List of dict komentar.
    """
    session = requests.Session()
    session.headers.update(MOBILE_HEADERS)

    all_comments = []
    total = len(posts)

    for i, item in enumerate(posts):
        s_date = start_date or item.get("start_date")
        e_date = end_date or item.get("end_date")

        comments = get_tiktok_comments_from_post(session, item, start_date=s_date, end_date=e_date)
        all_comments.extend(comments)

        if progress_callback:
            progress_callback(i + 1, total, item, len(comments))

    return all_comments
