"""
scraper_tiktok.py — Modul otomatis untuk scraping TikTok (postingan gambar/video & komentar).

Menggunakan Playwright browser dengan persistent profile, auto-detection puzzle captcha,
dynamic scrolling, dan ekstraksi timestamp Snowflake 64-bit untuk menyaring postingan
secara presisi sesuai rentang tanggal.

v2: Enrichment via Playwright untuk data presisi — mengatasi masalah kolom kosong
pada export Excel akibat TikTok anti-bot memblokir request HTTP standar.
"""

import os
import json
import re
import time
import requests
from html import unescape
from datetime import datetime
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


def _find_post_item(obj, post_id: str) -> Optional[dict]:
    """Cari item post yang ID-nya tepat, bukan item pertama di payload TikTok."""
    target_id = str(post_id)
    fallback = None
    stack = [obj]

    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            item_struct = current.get("itemStruct")
            if isinstance(item_struct, dict):
                stack.append(item_struct)

            looks_like_post = (
                ("desc" in current or "description" in current)
                and ("createTime" in current or "uploadDate" in current)
            )
            if looks_like_post:
                current_id = current.get("id") or current.get("aweme_id")
                if current_id is not None and str(current_id) == target_id:
                    return current
                # Payload JSON-LD kadang tidak menyertakan ID. Item tanpa ID
                # masih aman sebagai fallback; item dengan ID berbeda tidak.
                if current_id is None and fallback is None:
                    fallback = current

            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)

    return fallback


def _decode_json_string(value: str) -> str:
    """Decode escape JSON pada hasil regex caption dengan aman."""
    try:
        return str(json.loads(f'"{value}"')).strip()
    except (json.JSONDecodeError, TypeError):
        return unescape(str(value)).strip()


def _apply_post_item(result: dict, item: dict) -> None:
    """Salin metadata item TikTok ke result dan tandai caption terverifikasi."""
    c_time = _safe_int(item.get("createTime", 0))
    if c_time:
        result["post_date"] = datetime.fromtimestamp(c_time).strftime("%Y-%m-%d %H:%M:%S")

    if "desc" in item or "description" in item:
        result["post_caption"] = str(item.get("desc", item.get("description", "")) or "").strip()
        # Nilai kosong tetap valid: beberapa postingan memang tidak memiliki caption.
        result["_caption_resolved"] = True

    stats = item.get("stats", {}) or item.get("statsV2", {}) or {}
    result["post_likes"] = _safe_int(stats.get("diggCount", 0))
    result["post_shares"] = _safe_int(stats.get("shareCount", 0))
    result["post_comments_count"] = _safe_int(stats.get("commentCount", 0))
    result["post_views"] = _safe_int(stats.get("playCount", 0))


def _extract_caption_from_meta(html_content: str) -> str:
    """Fallback caption dari metadata SEO TikTok pada halaman post."""
    for tag in re.findall(r"<meta\b[^>]*>", html_content, re.IGNORECASE | re.DOTALL):
        attrs = {
            key.lower(): unescape(value).strip()
            for key, _, value in re.findall(
                r"([:\w-]+)\s*=\s*(['\"])(.*?)\2",
                tag,
                re.DOTALL,
            )
        }
        meta_name = (attrs.get("property") or attrs.get("name") or "").lower()
        if meta_name not in {"og:description", "twitter:description", "description"}:
            continue

        content = attrs.get("content", "").strip()
        if not content:
            continue

        # Deskripsi SEO TikTok umumnya membungkus caption dengan tanda kutip ini.
        quoted_caption = re.search(r"[“‘](.*?)[”’]", content, re.DOTALL)
        if quoted_caption:
            return quoted_caption.group(1).strip()

    return ""


def _extract_caption_from_page(page) -> str:
    """Ambil caption yang sudah dirender dari selector video maupun photo TikTok."""
    selectors = [
        '[data-e2e="browse-video-desc"]',
        '[data-e2e="video-desc"]',
        'h1[data-e2e*="desc"]',
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count() > 0:
                caption = locator.first.inner_text(timeout=1500).strip()
                if caption:
                    return caption
        except Exception:
            pass
    return ""


def _extract_post_data_from_html(html_content: str, post_id: str, approx_dt: Optional[datetime] = None) -> dict:
    """
    Parse data postingan dari konten HTML halaman TikTok.
    Mencari JSON rehydration data terlebih dahulu, lalu fallback ke regex.

    Args:
        html_content: Konten HTML halaman postingan.
        post_id: ID postingan.
        approx_dt: Datetime perkiraan dari Snowflake ID (fallback).

    Returns:
        Dict berisi post_date, post_caption, post_likes, post_shares,
        post_comments_count, post_views.
    """
    approx_str = approx_dt.strftime("%Y-%m-%d %H:%M:%S") if approx_dt else "N/A"
    result = {
        "post_date": approx_str,
        "post_caption": "",
        "post_likes": 0,
        "post_shares": 0,
        "post_comments_count": 0,
        "post_views": 0,
        "_caption_resolved": False,
    }

    # 1. Parse JSON rehydration data jika ada
    match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html_content, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            scope = data.get("__DEFAULT_SCOPE__", data)
            item = _find_post_item(scope, post_id)
            if item:
                _apply_post_item(result, item)
                return result
        except Exception:
            pass

    # 2. Coba parse dari SIGI_STATE atau script JSON lain
    for pattern in [
        r'<script id="SIGI_STATE"[^>]*>(.*?)</script>',
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    ]:
        alt_match = re.search(pattern, html_content, re.DOTALL)
        if alt_match:
            try:
                alt_data = json.loads(alt_match.group(1))

                found = _find_post_item(alt_data, post_id)
                if found:
                    _apply_post_item(result, found)
                    return result
            except Exception:
                pass

    # 3. Regex fallback untuk data individual
    # Caption
    escaped_id = re.escape(str(post_id))
    json_string = r'((?:\\.|[^"\\])*)'
    item_struct_match = (
        re.search(rf'"id":"?{escaped_id}"?.{{0,5000}}?"desc":"{json_string}"', html_content, re.DOTALL)
        or re.search(rf'"desc":"{json_string}".{{0,5000}}?"id":"?{escaped_id}"?', html_content, re.DOTALL)
    )
    if item_struct_match:
        result["post_caption"] = _decode_json_string(item_struct_match.group(1))
        result["_caption_resolved"] = True

    if not result["_caption_resolved"]:
        meta_caption = _extract_caption_from_meta(html_content)
        if meta_caption:
            result["post_caption"] = meta_caption
            result["_caption_resolved"] = True

    # createTime
    c_time_match = re.search(r'"createTime":"?(\d+)"?', html_content)
    if c_time_match:
        c_time = int(c_time_match.group(1))
        if c_time:
            result["post_date"] = datetime.fromtimestamp(c_time).strftime("%Y-%m-%d %H:%M:%S")

    # diggCount (likes)
    digg_match = re.search(r'"diggCount":\s*(\d+)', html_content)
    if digg_match:
        result["post_likes"] = int(digg_match.group(1))

    # shareCount
    share_match = re.search(r'"shareCount":\s*(\d+)', html_content)
    if share_match:
        result["post_shares"] = int(share_match.group(1))

    # commentCount
    comment_count_match = re.search(r'"commentCount":\s*(\d+)', html_content)
    if comment_count_match:
        result["post_comments_count"] = int(comment_count_match.group(1))

    # playCount (views)
    play_match = re.search(r'"playCount":\s*(\d+)', html_content)
    if play_match:
        result["post_views"] = int(play_match.group(1))

    return result


def _safe_int(val) -> int:
    """Konversi nilai ke int dengan aman, return 0 jika gagal."""
    try:
        if val is None:
            return 0
        return int(val)
    except (ValueError, TypeError):
        return 0


def get_post_details_via_playwright(page, post_url: str, post_id: str, approx_dt: Optional[datetime] = None) -> dict:
    """
    Ambil detail postingan TikTok menggunakan Playwright page yang sudah aktif.
    Ini mengatasi masalah anti-bot TikTok karena menggunakan browser sungguhan.

    Args:
        page: Playwright page instance yang sudah aktif.
        post_url: URL lengkap postingan.
        post_id: ID postingan.
        approx_dt: Datetime perkiraan dari Snowflake ID (fallback).

    Returns:
        Dict berisi post_date, post_caption, post_likes, post_shares,
        post_comments_count, post_views.
    """
    approx_str = approx_dt.strftime("%Y-%m-%d %H:%M:%S") if approx_dt else "N/A"
    empty_result = {
        "post_date": approx_str,
        "post_caption": "",
        "post_likes": 0,
        "post_shares": 0,
        "post_comments_count": 0,
        "post_views": 0,
        "_caption_resolved": False,
    }

    for attempt in range(2):
        try:
            page.goto(post_url, wait_until="domcontentloaded", timeout=20000)
            # Tunggu sebentar agar data ter-render
            time.sleep(1.5 + attempt * 1.0)

            # Ambil HTML dari page
            html_content = page.content()
            if not html_content or len(html_content) < 500:
                time.sleep(2)
                html_content = page.content()

            result = _extract_post_data_from_html(html_content, post_id, approx_dt)
            if not result.get("_caption_resolved"):
                dom_caption = _extract_caption_from_page(page)
                if dom_caption:
                    result["post_caption"] = dom_caption
                    result["_caption_resolved"] = True

            # Jika berhasil mendapatkan data yang bermakna, return
            if result["post_date"] != approx_str or result["post_caption"] or result["post_likes"] > 0:
                return result

            # Jika attempt pertama gagal, coba tunggu lebih lama
            if attempt == 0:
                time.sleep(2)
                html_content = page.content()
                result = _extract_post_data_from_html(html_content, post_id, approx_dt)
                if not result.get("_caption_resolved"):
                    dom_caption = _extract_caption_from_page(page)
                    if dom_caption:
                        result["post_caption"] = dom_caption
                        result["_caption_resolved"] = True
                if result["post_date"] != approx_str or result["post_caption"] or result["post_likes"] > 0:
                    return result

        except Exception:
            if attempt == 0:
                time.sleep(1.5)
                continue

    return empty_result


def get_tiktok_post_details(session: requests.Session, post_id: str, post_type: str = "video", username: str = "user") -> dict:
    """
    Ambil tanggal publish asli, caption, dan jumlah like dari satu postingan TikTok.
    Versi HTTP fallback — digunakan jika Playwright enrichment tidak tersedia.

    Args:
        session: Instance requests.Session.
        post_id: ID postingan.
        post_type: Tipe postingan ('photo' atau 'video').
        username: Username pembuat postingan.

    Returns:
        Dict berisi post_date, post_caption, post_likes, post_shares,
        post_comments_count, post_views.
    """
    url = f"https://www.tiktok.com/@{username}/{post_type}/{post_id}"
    approx_dt = extract_timestamp_from_post_id(post_id)

    for attempt in range(2):
        try:
            headers = dict(DESKTOP_HEADERS)
            headers["Referer"] = f"https://www.tiktok.com/@{username}"
            r = session.get(url, timeout=15, headers=headers)
            if r.status_code != 200:
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                break

            result = _extract_post_data_from_html(r.text, post_id, approx_dt)

            # Jika mendapatkan data bermakna, return
            if result["post_caption"] or result["post_likes"] > 0:
                return result

            # Retry jika data masih kosong
            if attempt == 0:
                time.sleep(1.5)
                continue

            return result

        except Exception:
            if attempt == 0:
                time.sleep(1.5)
                continue

    # Fallback terakhir: gunakan Snowflake timestamp
    approx_str = approx_dt.strftime("%Y-%m-%d %H:%M:%S") if approx_dt else "N/A"
    return {
        "post_date": approx_str,
        "post_caption": "",
        "post_likes": 0,
        "post_shares": 0,
        "post_comments_count": 0,
        "post_views": 0,
        "_caption_resolved": False,
    }


def get_tiktok_caption_via_oembed(
    session: requests.Session,
    post_url: str,
) -> Optional[str]:
    """Ambil caption melalui endpoint oEmbed resmi TikTok sebagai fallback terakhir."""
    try:
        response = session.get(
            "https://www.tiktok.com/oembed",
            params={"url": post_url},
            timeout=12,
            headers=DESKTOP_HEADERS,
        )
        if response.status_code != 200:
            return None

        payload = response.json()
        if "title" not in payload:
            return None
        return str(payload.get("title") or "").strip()
    except Exception:
        return None


def auto_scrape_tiktok_profile_posts(
    username: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    progress_callback: Optional[callable] = None,
    return_context: bool = False,
) -> list[dict] | tuple:
    """
    Scrape seluruh postingan (video & foto) dari profil TikTok secara otomatis menggunakan Playwright.
    Mendukung browser persistent context dan auto-detection puzzle slider captcha.

    Args:
        username: Username TikTok target (tanpa @).
        start_date: Tanggal mulai filter (opsional, untuk early termination saat scroll).
        end_date: Tanggal akhir filter (opsional).
        progress_callback: Callback(msg) saat proses scanning & scroll berlangsung.
        return_context: Jika True, return (posts, context, page, playwright_instance)
                        agar bisa dipakai untuk enrichment tanpa membuka browser baru.

    Returns:
        Jika return_context=False: List of dict containing post info.
        Jika return_context=True: Tuple (posts_list, context, page, playwright_instance).
    """
    clean_username = username.strip().lstrip("@")
    url = f"https://www.tiktok.com/@{clean_username}"
    
    # Gunakan direktori .tiktok_browser_profile utama di workspace root aplikasi
    app_root_dir = Path(__file__).resolve().parent.parent
    user_data_dir = str(app_root_dir / ".tiktok_browser_profile")

    posts_dict = {}

    def is_captcha_present(page) -> bool:
        """Cek apakah puzzle captcha muncul di body utama, iframe, atau selector khusus."""
        try:
            cur_body = page.inner_text("body").lower()
            cur_html = page.content().lower()
            if (
                "tarik penggeser" in cur_body
                or "drag the slider" in cur_body
                or "puzzle" in cur_body
                or "fit the puzzle" in cur_body
                or "secsdk-captcha" in cur_html
                or "captcha_verify" in cur_html
                or "verify-ele" in cur_html
            ):
                return True

            # Cek selector elemen dan iframe yang sering dipakai TikTok captcha
            captcha_selectors = [
                'iframe[src*="captcha"]',
                'iframe[id*="secsdk"]',
                '#secsdk-captcha-drag-wrapper',
                '.captcha_verify_container',
                '.verify-wrap',
                '#tiktok-verify-ele',
                '.captcha-verify-image',
                '.secsdk_captcha_modal',
            ]
            for sel in captcha_selectors:
                try:
                    if page.locator(sel).count() > 0:
                        return True
                except Exception:
                    pass

            # Periksa teks di dalam seluruh child iframe
            for frame in page.frames:
                try:
                    frame_url = frame.url.lower()
                    if "captcha" in frame_url or "secsdk" in frame_url or "verify" in frame_url:
                        return True
                    frame_text = frame.inner_text("body").lower()
                    if "drag the slider" in frame_text or "tarik penggeser" in frame_text or "puzzle" in frame_text:
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    def collect_from_page(page) -> list[dict]:
        """Kumpulkan postingan baru yang terlihat pada halaman saat ini."""
        new_posts = []
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
                        new_posts.append(posts_dict[p_id])

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
                    new_posts.append(posts_dict[pid])

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
                    new_posts.append(posts_dict[pid])
        except Exception:
            pass
        return new_posts

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

    def _switch_to_visual_and_solve_captcha(playwright_inst, current_ctx, target_url: str, progress_cb=None):
        """Tutup context headless dan buka browser visual agar pengguna dapat menyelesaikan puzzle captcha."""
        if progress_cb:
            progress_cb("! Terdeteksi verifikasi puzzle TikTok -- Membuka browser visual...")

        try:
            current_ctx.close()
        except Exception:
            pass

        # Beri jeda agar proses Chromium sebelumnya melepaskan file lock user_data_dir
        time.sleep(1.5)

        new_ctx = _launch_browser_context(playwright_inst, is_headless=False)
        new_page = new_ctx.new_page()
        try:
            new_page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass

        if progress_cb:
            progress_cb("! Silakan geser puzzle slider pada jendela browser Chrome/Edge yang muncul...")

        # Tunggu verifikasi diselesaikan pengguna (hingga 90 detik)
        captcha_solved = False
        for i in range(45):
            time.sleep(2)
            if not is_captcha_present(new_page):
                captcha_solved = True
                if progress_cb:
                    progress_cb("✓ Puzzle captcha berhasil diselesaikan! Melanjutkan pengambilan postingan...")
                # Berikan sedikit jeda untuk reload feed TikTok setelah puzzle selesai
                time.sleep(2.5)
                break
            else:
                if progress_cb and i % 3 == 0:
                    remaining_secs = 90 - (i * 2)
                    progress_cb(f"! Silakan geser puzzle slider di jendela browser ({remaining_secs}s tersisa)...")

        if not captcha_solved:
            try:
                new_ctx.close()
            except Exception:
                pass
            raise TikTokScraperError(
                "Verifikasi puzzle captcha TikTok belum diselesaikan tepat waktu. "
                "Silakan geser slider puzzle pada jendela browser yang terbuka, atau gunakan fitur Paste Link Video TikTok."
            )

        return new_ctx, new_page

    # ─────── Variabel untuk menyimpan context agar bisa di-return ───────
    _playwright_inst_ref = None
    _context_ref = None
    _page_ref = None

    try:
        p = sync_playwright().start()
        _playwright_inst_ref = p

        if progress_callback:
            progress_callback("Membuka browser Playwright...")

        context = _launch_browser_context(p, is_headless=True)
        _context_ref = context
        page = context.new_page()
        _page_ref = page

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=35000)
        except Exception:
            pass

        # Polling hingga 12 detik di awal untuk mendeteksi apakah TikTok memicu puzzle captcha
        # (fresh launch tanpa cache membutuhkan lebih lama untuk captcha muncul)
        has_captcha = False
        for poll_i in range(6):
            time.sleep(2)
            if is_captcha_present(page):
                has_captcha = True
                break
            # Cek apakah feed sudah mulai terload (postingan muncul)
            # Jika sudah, tidak perlu menunggu captcha lebih lama
            try:
                visible_posts = page.evaluate("""() => {
                    return document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]').length;
                }""")
                if visible_posts > 0:
                    break
            except Exception:
                pass

        # Jika terdeteksi puzzle captcha di awal, buka browser visible
        if has_captcha:
            context, page = _switch_to_visual_and_solve_captcha(p, context, url, progress_callback)
            _context_ref = context
            _page_ref = page

        # Kumpulkan postingan awal
        initial_posts = collect_from_page(page)

        # Jika tidak ada postingan ditemukan DAN captcha tidak terdeteksi di polling awal,
        # lakukan pengecekan captcha ulang yang lebih intensif (untuk kasus fresh launch lambat)
        if not initial_posts and not has_captcha:
            if progress_callback:
                progress_callback("Belum ada postingan ditemukan, memeriksa kemungkinan captcha tersembunyi...")
            time.sleep(3)
            if is_captcha_present(page):
                context, page = _switch_to_visual_and_solve_captcha(p, context, url, progress_callback)
                _context_ref = context
                _page_ref = page
                collect_from_page(page)
            else:
                # Cek apakah halaman blank/kosong (mungkin TikTok memblokir tanpa captcha)
                try:
                    body_text = page.inner_text("body").strip()
                    if len(body_text) < 100:
                        if progress_callback:
                            progress_callback("Halaman profil kosong, mencoba reload...")
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                        time.sleep(3)
                        if is_captcha_present(page):
                            context, page = _switch_to_visual_and_solve_captcha(p, context, url, progress_callback)
                            _context_ref = context
                            _page_ref = page
                        collect_from_page(page)
                except Exception:
                    pass

        # Lakukan dynamic scrolling
        max_scrolls = 50
        scroll_count = 0
        no_new_count = 0
        # ID TikTok mengandung timestamp publish, sehingga dapat dipakai untuk
        # menghentikan scroll tanpa membuka detail setiap postingan terlebih dulu.
        cutoff_date = start_date
        consecutive_older_posts = 0

        while scroll_count < max_scrolls:
            scroll_count += 1
            page.evaluate("window.scrollBy(0, 1500)")
            time.sleep(1.2)

            new_posts = collect_from_page(page)
            new_found = len(new_posts)

            if progress_callback:
                progress_callback(f"Scroll #{scroll_count}: {len(posts_dict)} postingan terdeteksi...")

            if new_found == 0:
                no_new_count += 1
                if no_new_count == 2:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(2.0)
                    collect_from_page(page)
                elif no_new_count >= 4 and len(posts_dict) == 0:
                    # Jika sama sekali tidak ada postingan, cek apakah captcha baru muncul di tengah scroll
                    if is_captcha_present(page):
                        # Alih-alih melempar error, segera buka browser visual untuk verifikasi!
                        context, page = _switch_to_visual_and_solve_captcha(p, context, url, progress_callback)
                        _context_ref = context
                        _page_ref = page
                        collect_from_page(page)
                        no_new_count = 0
                        continue
                    elif no_new_count >= 6:
                        break
                elif no_new_count >= 3 and len(posts_dict) > 0:
                    # Cek captcha juga saat sudah ada beberapa post (captcha bisa muncul di tengah scroll)
                    if is_captcha_present(page):
                        context, page = _switch_to_visual_and_solve_captcha(p, context, url, progress_callback)
                        _context_ref = context
                        _page_ref = page
                        collect_from_page(page)
                        no_new_count = 0
                        continue
                    elif no_new_count >= 5:
                        # 5x scroll berturut-turut tanpa postingan baru -> akhir halaman profil
                        break
            else:
                no_new_count = 0

            # Postingan pinned yang lama dapat muncul di bagian paling atas profil.
            # Karena itu, keputusan berhenti hanya memakai batch BARU hasil scroll,
            # bukan seluruh DOM. Enam post lama berturut-turut cukup menjadi bukti
            # feed sudah bergerak melewati tanggal awal yang diminta.
            if cutoff_date and new_posts:
                dated_new_posts = [p for p in new_posts if p.get("approx_date")]
                if dated_new_posts and all(p["approx_date"] < cutoff_date for p in dated_new_posts):
                    consecutive_older_posts += len(dated_new_posts)
                elif any(p["approx_date"] >= cutoff_date for p in dated_new_posts):
                    consecutive_older_posts = 0

                if consecutive_older_posts >= 6:
                    if progress_callback:
                        progress_callback(
                            "Batas awal periode sudah terlewati; menghentikan scroll profil."
                        )
                    break

        # Selalu kembalikan kandidat dari posting terbaru ke terlama. Urutan DOM
        # TikTok tidak dapat dipercaya karena postingan pinned dapat berada di atas.
        posts_list = sorted(
            posts_dict.values(),
            key=lambda item: item.get("approx_date") or datetime.min,
            reverse=True,
        )

        # Jika return_context = True, jangan tutup browser context (akan dipakai enrichment)
        if return_context:
            return posts_list, _context_ref, _page_ref, _playwright_inst_ref
        else:
            try:
                context.close()
            except Exception:
                pass
            try:
                p.stop()
            except Exception:
                pass

    except TikTokScraperError:
        if not return_context:
            try:
                if _context_ref:
                    _context_ref.close()
            except Exception:
                pass
            try:
                if _playwright_inst_ref:
                    _playwright_inst_ref.stop()
            except Exception:
                pass
        raise
    except Exception as e:
        if progress_callback:
            progress_callback(f"Playwright: {e}")
        if not return_context:
            try:
                if _context_ref:
                    _context_ref.close()
            except Exception:
                pass
            try:
                if _playwright_inst_ref:
                    _playwright_inst_ref.stop()
            except Exception:
                pass
        raise TikTokScraperError(f"Gagal memuat profil TikTok: {str(e)}")

    return posts_list


def get_tiktok_posts_in_range(
    target_input: str,
    start_date: datetime,
    end_date: datetime,
    progress_callback: Optional[callable] = None,
) -> list[dict]:
    """
    Ambil postingan TikTok (video/foto) yang dipublish dalam rentang waktu start_date s/d end_date.

    Setiap postingan di-enrich dengan data lengkap (caption, likes, tanggal asli, shares, views)
    menggunakan Playwright browser untuk melewati anti-bot TikTok, sebelum difilter
    berdasarkan rentang tanggal secara presisi.

    Args:
        target_input: Username TikTok atau Link/ID postingan.
        start_date: Tanggal mulai.
        end_date: Tanggal akhir.
        progress_callback: Callback(item) saat postingan valid ditemukan.

    Returns:
        List of dict item postingan yang valid dan sesuai rentang tanggal.
    """
    candidate_posts = []
    pw_context = None
    pw_page = None
    pw_instance = None

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
        # Gunakan return_context=True agar browser tetap terbuka untuk enrichment
        result = auto_scrape_tiktok_profile_posts(
            clean_username,
            start_date=start_date,
            end_date=end_date,
            progress_callback=progress_callback,
            return_context=True,
        )
        if isinstance(result, tuple):
            candidate_posts, pw_context, pw_page, pw_instance = result
        else:
            candidate_posts = result

    if not candidate_posts:
        # Tutup browser jika ada
        _cleanup_playwright(pw_context, pw_instance)
        return []

    # Snowflake ID TikTok menyimpan waktu publish pada 32 bit teratas. Gunakan
    # timestamp ini untuk membuang post di luar periode SEBELUM enrichment
    # Playwright yang mahal. Kandidat tanpa timestamp tetap dipertahankan agar
    # dapat diverifikasi dari metadata asli halaman post.
    candidate_posts.sort(
        key=lambda item: item.get("approx_date") or datetime.min,
        reverse=True,
    )
    detected_count = len(candidate_posts)
    candidate_posts = [
        item for item in candidate_posts
        if not item.get("approx_date") or start_date <= item["approx_date"] <= end_date
    ]

    if progress_callback:
        skipped_count = detected_count - len(candidate_posts)
        progress_callback(
            f"Seleksi cepat periode: {len(candidate_posts)} postingan akan diproses"
            f" ({skipped_count} di luar periode dilewati)."
        )

    if not candidate_posts:
        _cleanup_playwright(pw_context, pw_instance)
        return []

    # ── FASE ENRICHMENT: Ambil data lengkap dari setiap post via Playwright ──
    if progress_callback:
        progress_callback(f"Mengambil detail lengkap {len(candidate_posts)} postingan dari TikTok...")

    # Jika belum punya browser context (direct input), buka baru untuk enrichment
    if pw_context is None or pw_page is None:
        try:
            pw_instance = sync_playwright().start()
            app_root_dir = Path(__file__).resolve().parent.parent
            user_data_dir = str(app_root_dir / ".tiktok_browser_profile")

            if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ or os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0":
                local_app = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(local_app, "ms-playwright")

            browser_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ]

            channels = ["msedge", "chrome", None]
            for ch in channels:
                try:
                    launch_opts = {
                        "user_data_dir": user_data_dir,
                        "headless": True,
                        "args": browser_args,
                        "viewport": {"width": 1280, "height": 900},
                        "locale": "id-ID",
                        "user_agent": DESKTOP_HEADERS["User-Agent"],
                    }
                    if ch:
                        launch_opts["channel"] = ch
                    pw_context = pw_instance.chromium.launch_persistent_context(**launch_opts)
                    pw_context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                    pw_page = pw_context.new_page()
                    break
                except Exception:
                    continue
        except Exception:
            pw_context = None
            pw_page = None

    enriched_posts = []
    session_fallback = requests.Session()
    session_fallback.headers.update(DESKTOP_HEADERS)

    for idx, post_item in enumerate(candidate_posts):
        p_id = str(post_item["id"])
        p_type = post_item.get("post_type", "video")
        p_user = post_item.get("target_username", "user")
        post_url = post_item.get("post_url") or f"https://www.tiktok.com/@{p_user}/{p_type}/{p_id}"

        if progress_callback:
            progress_callback(f"Detail post {idx+1}/{len(candidate_posts)} (ID: {p_id})...")

        details = None

        # Metode 1: Enrichment via Playwright (prioritas utama)
        if pw_page is not None:
            try:
                approx_dt = post_item.get("approx_date") or extract_timestamp_from_post_id(p_id)
                details = get_post_details_via_playwright(pw_page, post_url, p_id, approx_dt)
            except Exception:
                details = None

        # Metode 2: fallback juga wajib berjalan bila caption belum berhasil
        # diverifikasi, meskipun jumlah like sudah ditemukan oleh Playwright.
        caption_unresolved = bool(details) and not details.get(
            "_caption_resolved", bool(details.get("post_caption"))
        )
        if not details or caption_unresolved:
            try:
                details_http = get_tiktok_post_details(session_fallback, p_id, p_type, p_user)
                # Gabungkan: pakai data terbaik dari kedua sumber
                if details:
                    for key in ["post_caption", "post_likes", "post_shares", "post_comments_count", "post_views"]:
                        if not details.get(key) and details_http.get(key):
                            details[key] = details_http[key]
                    if details.get("post_date", "N/A") == "N/A" and details_http.get("post_date", "N/A") != "N/A":
                        details["post_date"] = details_http["post_date"]
                    if details_http.get("_caption_resolved"):
                        details["post_caption"] = details_http.get("post_caption", "")
                        details["_caption_resolved"] = True
                else:
                    details = details_http
            except Exception:
                pass

        # Metode 3: endpoint resmi oEmbed menyediakan caption pada field title.
        # Hanya dipanggil untuk post yang caption-nya masih belum terverifikasi.
        if details and not details.get("_caption_resolved", bool(details.get("post_caption"))):
            oembed_caption = get_tiktok_caption_via_oembed(session_fallback, post_url)
            if oembed_caption is not None:
                details["post_caption"] = oembed_caption
                details["_caption_resolved"] = True

        # Jika seluruh metode gagal, gunakan data minimal
        if not details:
            approx_dt = post_item.get("approx_date") or extract_timestamp_from_post_id(p_id)
            approx_str = approx_dt.strftime("%Y-%m-%d %H:%M:%S") if approx_dt else "N/A"
            details = {
                "post_date": approx_str,
                "post_caption": "",
                "post_likes": 0,
                "post_shares": 0,
                "post_comments_count": 0,
                "post_views": 0,
            }

        # Gunakan tanggal asli dari enrichment jika tersedia
        real_date_str = details.get("post_date", "N/A")
        real_date = None
        if real_date_str and real_date_str != "N/A":
            try:
                real_date = datetime.strptime(real_date_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                real_date = None

        # Fallback ke snowflake approx_date jika enrichment tidak mengembalikan tanggal
        if not real_date:
            real_date = post_item.get("approx_date") or extract_timestamp_from_post_id(p_id)
            if real_date:
                real_date_str = real_date.strftime("%Y-%m-%d %H:%M:%S")

        # Update post_item dengan data lengkap
        post_item["post_date"] = real_date_str if real_date_str else "N/A"
        post_item["post_caption"] = details.get("post_caption", "") or post_item.get("post_caption", "")
        post_item["post_likes"] = details.get("post_likes", 0) or post_item.get("post_likes", 0)
        post_item["post_shares"] = details.get("post_shares", 0)
        post_item["post_comments_count"] = details.get("post_comments_count", 0)
        post_item["post_views"] = details.get("post_views", 0)
        post_item["real_date"] = real_date  # datetime object untuk filter presisi
        post_item["start_date"] = start_date
        post_item["end_date"] = end_date

        enriched_posts.append(post_item)

        # Delay kecil antar request untuk menghindari rate limiting
        if idx < len(candidate_posts) - 1:
            time.sleep(0.8)

    # Tutup browser context setelah enrichment selesai
    _cleanup_playwright(pw_context, pw_instance)

    # ── FASE FILTER: Filter presisi berdasarkan tanggal ASLI dari enrichment ──
    filtered_posts = []
    for post_item in enriched_posts:
        filter_date = post_item.get("real_date")
        if not filter_date:
            continue  # Skip jika tidak ada tanggal sama sekali

        if start_date <= filter_date <= end_date:
            filtered_posts.append(post_item)
            if progress_callback:
                progress_callback(post_item)

    if progress_callback:
        progress_callback(
            f"✓ {len(filtered_posts)} dari {len(enriched_posts)} postingan sesuai rentang tanggal "
            f"({start_date.strftime('%d-%m-%Y')} s/d {end_date.strftime('%d-%m-%Y')})."
        )

    # Urutkan postingan berdasarkan tanggal publish terbaru (descending)
    filtered_posts.sort(key=lambda x: x.get("post_date", ""), reverse=True)

    return filtered_posts


def _cleanup_playwright(context, playwright_instance):
    """Tutup browser context dan hentikan Playwright instance dengan aman."""
    try:
        if context:
            context.close()
    except Exception:
        pass
    try:
        if playwright_instance:
            playwright_instance.stop()
    except Exception:
        pass


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
        start_date: Tanggal mulai filter postingan.
        end_date: Tanggal akhir filter postingan.

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
    post_shares = item.get("post_shares", 0)
    post_comments_count = item.get("post_comments_count", 0)
    post_views = item.get("post_views", 0)

    # Pengaman untuk pemanggilan langsung fungsi ini: jangan meminta komentar
    # apabila timestamp ID sudah memastikan post berada di luar periode.
    item_date = item.get("real_date") or item.get("approx_date") or extract_timestamp_from_post_id(v_id)
    if item_date:
        if start_date and item_date < start_date:
            return []
        if end_date and item_date > end_date:
            return []

    if not post_date_str or post_date_str == "N/A":
        post_info = get_tiktok_post_details(session, v_id, p_type, username)
        post_date_str = post_info.get("post_date", "N/A")
        post_caption_str = post_info.get("post_caption", "")
        post_likes = post_info.get("post_likes", 0)
        post_shares = post_info.get("post_shares", 0)
        post_comments_count = post_info.get("post_comments_count", 0)
        post_views = post_info.get("post_views", 0)

    comments = []
    cursor = 0
    max_comments = 500
    consecutive_errors = 0

    while len(comments) < max_comments:
        comm_url = f"https://www.tiktok.com/api/comment/list/?aid=1988&aweme_id={v_id}&count=50&cursor={cursor}"
        try:
            r = session.get(comm_url, timeout=15)
            if r.status_code != 200:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    break
                time.sleep(1.5)
                continue

            consecutive_errors = 0
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
                    "post_shares": post_shares,
                    "post_comments_count": post_comments_count,
                    "post_views": post_views,
                    "post_date": post_date_str,
                    "post_caption": post_caption_str or "",
                })

            cursor = data.get("cursor", 0)
            has_more = data.get("has_more", 0)
            if not has_more:
                break

            # Delay antar request untuk menghindari rate limiting
            time.sleep(0.5)

        except Exception:
            consecutive_errors += 1
            if consecutive_errors >= 3:
                break
            time.sleep(1.5)

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
        start_date: Tanggal mulai filter postingan.
        end_date: Tanggal akhir filter postingan.
        progress_callback: Callback (current, total, item, comment_count).

    Returns:
        List of dict komentar.
    """
    session = requests.Session()
    session.headers.update(MOBILE_HEADERS)

    all_comments = []
    # Pastikan komentar dikoleksi per postingan dari yang terbaru ke terlama,
    # terlepas dari urutan input yang diberikan pemanggil.
    ordered_posts = sorted(
        posts,
        key=lambda item: item.get("real_date") or item.get("approx_date") or datetime.min,
        reverse=True,
    )
    total = len(ordered_posts)

    for i, item in enumerate(ordered_posts):
        s_date = start_date or item.get("start_date")
        e_date = end_date or item.get("end_date")

        comments = get_tiktok_comments_from_post(session, item, start_date=s_date, end_date=e_date)
        all_comments.extend(comments)

        if progress_callback:
            progress_callback(i + 1, total, item, len(comments))

        # Delay antar post untuk menghindari rate limiting
        if i < total - 1:
            time.sleep(0.5)

    return all_comments
