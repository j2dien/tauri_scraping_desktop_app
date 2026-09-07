"""
scraper_instagram.py — Modul untuk scraping data Instagram menggunakan instagrapi.

Mengambil postingan (feed + reels) dan komentar dari profil Instagram
dalam rentang waktu tertentu secara cepat, aman, dan non-blocking.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable

from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    ChallengeRequired,
    UserNotFound,
    ClientError,
    FeedbackRequired,
    RateLimitError,
)

SESSION_DIR = Path.home() / ".instagram_sessions"


class LoginRequiredError(Exception):
    """Raised ketika Instagram memblokir akses atau memerlukan interaksi/verifikasi."""
    pass


def _no_interactive_challenge(username: str, choice=None):
    """Handler non-blocking saat Instagram meminta verifikasi Challenge/2FA."""
    raise LoginRequiredError(
        f"Akun @{username} memerlukan verifikasi keamanan (Challenge/2FA via {choice or 'SMS/Email/Aplikasi'}).\n"
        "Silakan buka aplikasi Instagram di ponsel Anda, setujui konfirmasi 'Ini Saya', lalu coba lagi."
    )


def _no_interactive_password(username: str):
    """Handler non-blocking saat Instagram meminta penggantian password."""
    raise LoginRequiredError(
        f"Instagram meminta reset/pergantian password untuk akun @{username}.\n"
        "Harap perbarui password melalui aplikasi Instagram di ponsel Anda terlebih dahulu."
    )


def create_client() -> Client:
    """Buat instance Client instagrapi dengan konfigurasi aman & non-blocking."""
    cl = Client()
    cl.delay_range = [1, 2]
    cl.request_timeout = 20
    cl.challenge_code_handler = _no_interactive_challenge
    cl.change_password_handler = _no_interactive_password
    return cl


def _session_path(username: str) -> Path:
    """Path file session untuk username tertentu."""
    clean_user = re.sub(r"[^\w\-]", "_", username.lower()).strip("_")
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR / f"{clean_user}.json"


def get_session_info(username: str) -> dict:
    """Periksa apakah ada sesi tersimpan untuk username ini."""
    clean_user = username.replace("@", "").strip().lower()
    session_file = _session_path(clean_user)
    return {
        "has_saved_session": session_file.exists(),
        "username": clean_user,
        "session_path": str(session_file)
    }


def clear_session(username: str) -> bool:
    """Hapus file sesi untuk username tertentu."""
    clean_user = username.replace("@", "").strip().lower()
    session_file = _session_path(clean_user)
    if session_file.exists():
        session_file.unlink(missing_ok=True)
        return True
    return False


def login_by_sessionid(
    cl: Client,
    sessionid: str,
    username: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Login ke Instagram langsung menggunakan cookie sessionid dari browser.
    Metode ini bebas dari Challenge / Checkpoint karena menggunakan token web yang sah.
    """
    clean_sid = sessionid.strip().strip('"').strip("'")
    if not clean_sid:
        raise LoginRequiredError("Session ID tidak boleh kosong.")

    clean_user = username.replace("@", "").strip().lower() if username else "ig_session_user"
    session_file = _session_path(clean_user)

    if progress_callback:
        progress_callback("Menghubungkan ke Instagram via Cookie Session ID...")

    # Load existing device settings if any to stay consistent
    if session_file.exists():
        try:
            cl.load_settings(session_file)
        except Exception:
            pass

    try:
        cl.login_by_sessionid(clean_sid)
        cl.dump_settings(session_file)
        if progress_callback:
            progress_callback("✓ Berhasil login Instagram via Session ID! Sesi telah disimpan.")
        return True
    except Exception as e:
        raise LoginRequiredError(
            f"Gagal login menggunakan Session ID: {str(e)}.\n"
            "Pastikan cookie sessionid disalin lengkap dari browser tempat Anda login ke Instagram."
        )


def login_instagram(
    cl: Client,
    username: str,
    password: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Login ke Instagram dengan persistensi device identity dan feedback real-time.
    Saat terjadi challenge 'Ini Saya' di ponsel, device settings dipertahankan agar percobaan berikutnya dikenali.
    """
    clean_user = username.replace("@", "").strip().lower()
    session_file = _session_path(clean_user)

    cl.challenge_code_handler = _no_interactive_challenge
    cl.change_password_handler = _no_interactive_password
    cl.request_timeout = 20

    # 1. Coba gunakan sesi tersimpan terlebih dahulu jika ada
    if session_file.exists():
        try:
            if progress_callback:
                progress_callback(f"Mengecek sesi login tersimpan untuk @{clean_user}...")
            cl.load_settings(session_file)
            # Verifikasi apakah sesi masih aktif
            cl.get_timeline_feed()
            if progress_callback:
                progress_callback(f"✓ Sesi @{clean_user} masih valid! Melanjutkan...")
            return True
        except Exception:
            if progress_callback:
                progress_callback("Sesi token kedaluwarsa, mempertahankan device settings dan mencoba login ulang...")
            # PENTING: Jangan hapus device settings! Simpan device settings saat ini agar UUID & Device ID tetap sama
            try:
                cl.dump_settings(session_file)
            except Exception:
                pass
    else:
        # Jika belum ada file sesi, dump device settings awal agar device ID konsisten sejak awal
        try:
            cl.dump_settings(session_file)
        except Exception:
            pass

    # 2. Login baru dengan username & password
    try:
        if progress_callback:
            progress_callback(f"Mengirim permintaan login untuk @{clean_user} ke Instagram...")
        cl.login(clean_user, password)
        cl.dump_settings(session_file)
        if progress_callback:
            progress_callback("✓ Login berhasil! Sesi baru telah disimpan.")
        return True
    except ChallengeRequired as e:
        # PENTING: Selalu simpan device settings saat terkena challenge!
        try:
            cl.dump_settings(session_file)
        except Exception:
            pass
        raise LoginRequiredError(
            f"Instagram meminta verifikasi keamanan (Challenge/2FA) untuk @{clean_user}.\n"
            "Langkah penyelesaian:\n"
            "1. Buka aplikasi Instagram di ponsel Anda.\n"
            "2. Ketuk notifikasi atau banner keamanan, lalu pilih 'Ini Saya' (This Was Me).\n"
            "3. Kembali ke aplikasi desktop ini dan klik 'Mulai Scraping & Analisis' lagi (identitas perangkat Anda telah tersimpan).\n"
            "💡 Tips: Anda juga dapat menggunakan opsi 'Cookie Session ID' jika verifikasi di HP terkendala."
        )
    except Exception as e:
        err_msg = str(e)
        err_lower = err_msg.lower()

        # Selalu dump settings jika error berkaitan dengan challenge/checkpoint
        if "challenge" in err_lower or "checkpoint" in err_lower or "native challenge" in err_lower or "two_factor" in err_lower:
            try:
                cl.dump_settings(session_file)
            except Exception:
                pass
            raise LoginRequiredError(
                f"Instagram meminta verifikasi Challenge untuk @{clean_user}.\n"
                "Langkah penyelesaian:\n"
                "1. Buka aplikasi Instagram di ponsel Anda dan konfirmasi 'Ini Saya'.\n"
                "2. Klik 'Mulai Scraping & Analisis' kembali di aplikasi ini (identitas perangkat telah tersimpan).\n"
                "💡 Atau gunakan opsi 'Cookie Session ID' dari browser Chrome/Edge untuk bypass challenge 100%."
            )

        if "bad_password" in err_lower or "password" in err_lower:
            raise LoginRequiredError("Password Instagram yang dimasukkan salah. Periksa kembali password Anda.")
        if "rate" in err_lower or "429" in err_lower or "feedback_required" in err_lower:
            raise LoginRequiredError("Instagram membatasi permintaan login (Rate Limit). Tunggu 10-15 menit atau gunakan opsi Cookie Session ID.")
        raise LoginRequiredError(f"Gagal login Instagram: {err_msg}")


def _paginate_feed_by_date(
    cl: Client,
    user_id: str,
    start_date: datetime,
    end_date: datetime,
    progress_callback: Optional[Callable[[Any], None]] = None,
) -> list:
    """Ambil postingan feed secara presisi per halaman, berhenti begitu melewati start_date.

    Menggunakan user_medias_paginated() untuk mengambil 33 item per halaman.
    Instagram mengembalikan postingan dari terbaru → terlama, sehingga kita bisa
    berhenti begitu semua postingan sudah melewati start_date.
    """
    PAGE_SIZE = 33
    collected = []
    end_cursor = None
    page = 0

    while True:
        page += 1
        try:
            medias, end_cursor = cl.user_medias_paginated(user_id, amount=PAGE_SIZE, end_cursor=end_cursor)
        except Exception:
            break

        if not medias:
            break

        past_range_count = 0
        for media in medias:
            post_date = media.taken_at.replace(tzinfo=None)

            # Lewati yang lebih baru dari end_date
            if post_date > end_date:
                continue

            # Hitung berturut-turut yang melewati start_date
            if post_date < start_date:
                past_range_count += 1
                continue

            collected.append(media)

        if progress_callback:
            progress_callback(f"Halaman feed {page}: +{len(medias)} media, {len(collected)} dalam rentang tanggal")

        # Hentikan pagination jika semua item di halaman ini sudah melewati start_date
        # atau jika tidak ada cursor lagi
        if past_range_count == len(medias):
            if progress_callback:
                progress_callback(f"Feed: seluruh halaman {page} di luar rentang tanggal, menghentikan pagination.")
            break

        if not end_cursor:
            break

    return collected


def _paginate_clips_by_date(
    cl: Client,
    user_id: str,
    start_date: datetime,
    end_date: datetime,
    already_found_ids: set = None,
    progress_callback: Optional[Callable[[Any], None]] = None,
) -> list:
    """Ambil reels/clips dari terbaru ke terlama, hanya ambil yang dalam rentang tanggal.

    Strategi cepat:
    - Hitung estimasi jumlah reels berdasarkan lebar rentang tanggal (misal 30 hari ≈ 60 reels max).
    - Ambil sekali dengan jumlah terbatas, lalu filter dari terbaru → terlama.
    - Berhenti iterasi begitu ketemu reels yang sudah melewati start_date.
    - Skip reels yang sudah ditemukan di feed (via already_found_ids).
    """
    if already_found_ids is None:
        already_found_ids = set()

    # Estimasi jumlah reels yang perlu diambil berdasarkan rentang tanggal
    # Asumsi: rata-rata akun posting ~2 reels/hari
    days_span = max((end_date - start_date).days, 1)
    estimated_amount = min(max(days_span * 2, 30), 200)  # Min 30, max 200

    if progress_callback:
        progress_callback(f"Mengambil ~{estimated_amount} reels terbaru untuk filter tanggal...")

    try:
        clips = cl.user_clips(user_id, amount=estimated_amount)
    except Exception:
        return []

    if not clips:
        return []

    # Filter dari terbaru → terlama, berhenti jika sudah melewati start_date
    collected = []
    past_count = 0

    for clip in clips:
        post_date = clip.taken_at.replace(tzinfo=None)

        # Skip jika sudah ada di feed
        clip_id = getattr(clip, 'id', None) or getattr(clip, 'pk', None)
        if clip_id and clip_id in already_found_ids:
            continue

        # Lewati yang lebih baru dari end_date
        if post_date > end_date:
            continue

        # Hitung yang melewati start_date
        if post_date < start_date:
            past_count += 1
            # Jika sudah 5 berturut-turut di luar rentang → berhenti (data sudah terurut terbaru-terlama)
            if past_count >= 5:
                break
            continue

        past_count = 0  # Reset counter
        collected.append(clip)

    return collected


def get_posts_in_range(
    cl: Client,
    target_username: str,
    start_date: datetime,
    end_date: datetime,
    progress_callback: Optional[Callable[[Any], None]] = None,
) -> list:
    """Ambil postingan (feed + reels) dari profil Instagram secara PRESISI dalam rentang waktu.

    Menggunakan pagination per halaman untuk feed (berhenti begitu melewati start_date)
    dan pengambilan penuh untuk reels dengan filter tanggal yang ketat.
    Dilengkapi fallback GQL jika Private API gagal (umum untuk sesi web cookie).
    """
    clean_target = target_username.replace("@", "").strip()

    try:
        if progress_callback:
            progress_callback(f"Mencari profil target @{clean_target} di Instagram...")
        user_id = cl.user_id_from_username(clean_target)
    except UserNotFound:
        raise LoginRequiredError(f"Profil @{clean_target} tidak ditemukan. Periksa ejaan username target.")
    except LoginRequired:
        raise LoginRequiredError(f"Instagram membatasi akses profil @{clean_target}. Login akun Anda mungkin kedaluwarsa.")
    except ClientError as e:
        raise LoginRequiredError(f"Gagal mengakses profil @{clean_target}: {e}")

    filtered_posts = []

    try:
        feed_posts = []
        clips_posts = []

        # ── 1. FEED: Pagination presisi per halaman ──
        if progress_callback:
            progress_callback(f"Mengambil feed postingan @{clean_target} (pagination presisi)...")

        feed_fetched = False
        try:
            feed_posts = _paginate_feed_by_date(cl, user_id, start_date, end_date, progress_callback)
            feed_fetched = True
            if progress_callback:
                progress_callback(f"✓ Ditemukan {len(feed_posts)} postingan feed dalam rentang tanggal.")
        except Exception as e:
            if progress_callback:
                progress_callback(f"Private API feed gagal ({str(e)[:80]}), mencoba via GraphQL...")

        # Fallback ke GraphQL jika Private API gagal
        if not feed_fetched:
            try:
                all_gql = cl.user_medias_gql(user_id, amount=0)
                for m in all_gql:
                    pd = m.taken_at.replace(tzinfo=None)
                    if start_date <= pd <= end_date:
                        feed_posts.append(m)
                feed_fetched = True
                if progress_callback:
                    progress_callback(f"✓ Ditemukan {len(feed_posts)} postingan feed via GraphQL.")
            except Exception as e:
                if progress_callback:
                    progress_callback(f"⚠ Gagal mengambil feed: {str(e)[:100]}")

        # ── 2. REELS/CLIPS: Ambil reels dengan filter tanggal presisi ──
        if progress_callback:
            progress_callback(f"Mengambil video reels @{clean_target}...")

        # Simpan ID feed agar reels tidak duplikat
        found_ids = {getattr(m, 'id', None) or getattr(m, 'pk', None) for m in feed_posts}

        clips_fetched = False
        try:
            clips_posts = _paginate_clips_by_date(cl, user_id, start_date, end_date, already_found_ids=found_ids, progress_callback=progress_callback)
            clips_fetched = True
            if progress_callback:
                progress_callback(f"✓ Ditemukan {len(clips_posts)} video reels baru dalam rentang tanggal.")
        except Exception as e:
            if progress_callback:
                progress_callback(f"Private API reels gagal ({str(e)[:80]}), reels dari feed sudah ter-cover.")

        # Gabungkan feed + clips
        all_medias = feed_posts + clips_posts

        if not all_medias:
            if progress_callback:
                progress_callback("Tidak ada postingan yang ditemukan dalam rentang tanggal yang diminta.")
            return []

        # ── 3. Deduplikasi berdasarkan media ID ──
        seen_ids = set()
        unique_medias = []
        for media in all_medias:
            media_id = getattr(media, 'id', None) or getattr(media, 'pk', None)
            if media_id and media_id not in seen_ids:
                seen_ids.add(media_id)
                unique_medias.append(media)

        # Urutkan berdasarkan waktu publish terbaru
        unique_medias.sort(key=lambda m: m.taken_at, reverse=True)

        if progress_callback:
            progress_callback(
                f"Total {len(unique_medias)} postingan unik dalam rentang tanggal "
                f"(feed: {len(feed_posts)}, reels: {len(clips_posts)})."
            )

        # ── 4. Kirim setiap postingan ke progress callback ──
        for media in unique_medias:
            filtered_posts.append(media)
            if progress_callback:
                progress_callback(media)

    except LoginRequired:
        raise LoginRequiredError("Sesi Instagram kedaluwarsa saat mengambil postingan. Silakan coba lagi.")
    except Exception as e:
        if "login" in str(e).lower() or "401" in str(e):
            raise LoginRequiredError(f"Instagram membatasi akses: {e}")
        raise

    return filtered_posts


def get_comments_from_post(cl: Client, media, fetch_likers: bool = True) -> list[dict]:
    """Ambil komentar dari satu postingan dan periksa status like komentator (Mendukung GraphQL & Private API)."""
    comments = []
    try:
        caption = getattr(media, 'caption_text', '') or ''
        post_likes = getattr(media, 'like_count', 0) or 0
        post_code = getattr(media, 'code', '') or str(getattr(media, 'id', ''))
        taken_at_str = media.taken_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(media, 'taken_at') and media.taken_at else "N/A"
        media_id = str(getattr(media, 'id', '')) or str(getattr(media, 'pk', ''))

        media_comments = []

        # 1. Coba ambil via GraphQL Web API terlebih dahulu (sangat stabil untuk sesi web / Cookie Session ID)
        try:
            media_comments = cl.media_comments_gql(media_id, amount=100)
        except Exception:
            pass

        # 2. Fallback: Coba via Private API v1 jika GraphQL belum menghasilkan data
        if not media_comments:
            try:
                media_comments = cl.media_comments(media_id, amount=100)
            except Exception:
                pass

        # 3. Fallback kedua: Coba via Threaded GraphQL
        if not media_comments:
            try:
                media_comments = cl.media_comments_threaded_gql(media_id, amount=100)
            except Exception:
                pass

        if not media_comments:
            return []

        # Ambil daftar likers jika ada (opsional, jangan sampai menggagalkan komentar)
        likers_set = set()
        if fetch_likers and post_likes > 0:
            try:
                media_likers = cl.media_likers(media_id)
                likers_set = {u.username.lower() for u in media_likers if u and getattr(u, 'username', None)}
            except Exception:
                pass

        for comment in media_comments:
            # Parsing data komentar, menangani baik bentuk dict (GraphQL) maupun objek Comment (Private API)
            if isinstance(comment, dict):
                user_obj = comment.get("user") or {}
                if isinstance(user_obj, dict):
                    commenter_user = user_obj.get("username") or user_obj.get("id") or "unknown"
                else:
                    commenter_user = getattr(user_obj, "username", "unknown")

                comment_text = comment.get("text") or comment.get("caption") or ""
                c_likes = comment.get("comment_like_count") or comment.get("like_count") or 0
                c_ts = comment.get("created_at") or comment.get("created_at_utc")
                if c_ts:
                    try:
                        comment_date = datetime.fromtimestamp(int(c_ts)).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        comment_date = str(c_ts)
                else:
                    comment_date = "N/A"
            else:
                user_obj = getattr(comment, 'user', None)
                commenter_user = getattr(user_obj, 'username', 'unknown') if user_obj else "unknown"
                comment_text = getattr(comment, 'text', '') or ""
                comment_date = "N/A"
                if hasattr(comment, 'created_at_utc') and comment.created_at_utc:
                    comment_date = comment.created_at_utc.strftime("%Y-%m-%d %H:%M:%S")
                elif hasattr(comment, 'created_at') and comment.created_at:
                    comment_date = comment.created_at.strftime("%Y-%m-%d %H:%M:%S")
                # FIX: Gunakan c_likes (bukan comment_likes) agar konsisten dengan dict output
                c_likes = getattr(comment, 'like_count', 0) or getattr(comment, 'like_count_display', 0) or 0

            # Periksa apakah komentator me-like post
            if likers_set:
                has_liked = "Ya" if commenter_user.lower() in likers_set else "Tidak"
            else:
                has_liked = "N/A"

            comment_data = {
                "commenter_username": commenter_user,
                "comment_text": comment_text,
                "has_liked_post": has_liked,
                "comment_likes": int(c_likes) if c_likes else 0,
                "comment_date": comment_date,
                "post_shortcode": post_code,
                "post_url": f"https://www.instagram.com/p/{post_code}/",
                "post_likes": post_likes,
                "post_date": taken_at_str,
                "post_caption": (caption[:100] + "...") if caption and len(caption) > 100 else caption,
            }
            comments.append(comment_data)

    except Exception:
        pass

    return comments


def get_all_comments(
    cl: Client,
    posts: list,
    progress_callback: Optional[Callable[[int, int, Any, int, str], None]] = None,
) -> list[dict]:
    """Ambil semua komentar dari daftar postingan dengan progress realtime."""
    all_comments = []
    total = len(posts)

    for i, media in enumerate(posts):
        post_code = getattr(media, 'code', '') or str(getattr(media, 'pk', i + 1))
        post_date = media.taken_at.strftime("%d-%m-%Y") if hasattr(media, 'taken_at') and media.taken_at else "N/A"

        # Notifikasi sebelum mulai mengambil komentar postingan
        if progress_callback:
            progress_callback(
                i + 1,
                total,
                media,
                len(all_comments),
                f"Mengambil komentar post {i+1}/{total} (ID: {post_code}, {post_date})..."
            )

        comments = get_comments_from_post(cl, media)
        all_comments.extend(comments)

        # Notifikasi setelah postingan selesai diproses
        if progress_callback:
            progress_callback(
                i + 1,
                total,
                media,
                len(all_comments),
                f"Post {i+1}/{total} selesai: +{len(comments)} komentar (Total: {len(all_comments)})"
            )

    return all_comments
