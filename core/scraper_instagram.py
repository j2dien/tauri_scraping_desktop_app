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
    cl.request_timeout = 15
    cl.challenge_code_handler = _no_interactive_challenge
    cl.change_password_handler = _no_interactive_password
    return cl


def _session_path(username: str) -> Path:
    """Path file session untuk username tertentu."""
    clean_user = re.sub(r"[^\w\-]", "_", username.lower()).strip("_")
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR / f"{clean_user}.json"


def login_instagram(
    cl: Client,
    username: str,
    password: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Login ke Instagram dengan session persistence dan feedback real-time.
    Non-blocking (tidak akan freeze jika terkena challenge).
    """
    clean_user = username.replace("@", "").strip()
    session_file = _session_path(clean_user)

    cl.challenge_code_handler = _no_interactive_challenge
    cl.change_password_handler = _no_interactive_password
    cl.request_timeout = 15

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
                progress_callback("Sesi tersimpan sudah kedaluwarsa, melakukan login ulang...")
            session_file.unlink(missing_ok=True)
            cl.set_settings({})

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
        raise LoginRequiredError(
            f"Instagram meminta verifikasi 2FA/Challenge untuk @{clean_user}: {str(e)}.\n"
            "Buka aplikasi Instagram di ponsel dan konfirmasi 'Ini Saya' untuk mengizinkan login."
        )
    except Exception as e:
        err_msg = str(e)
        err_lower = err_msg.lower()
        if "bad_password" in err_lower or "password" in err_lower:
            raise LoginRequiredError("Password Instagram yang dimasukkan salah. Periksa kembali password Anda.")
        if "challenge" in err_lower or "checkpoint" in err_lower or "two_factor" in err_lower:
            raise LoginRequiredError(
                f"Instagram meminta verifikasi keamanan (2FA/Challenge) untuk @{clean_user}.\n"
                "Silakan buka Instagram di HP, lakukan verifikasi/konfirmasi login, lalu coba kembali."
            )
        if "rate" in err_lower or "429" in err_lower or "feedback_required" in err_lower:
            raise LoginRequiredError("Instagram membatasi permintaan login (Rate Limit). Tunggu 10-15 menit sebelum mencoba lagi.")
        raise LoginRequiredError(f"Gagal login Instagram: {err_msg}")


def get_posts_in_range(
    cl: Client,
    target_username: str,
    start_date: datetime,
    end_date: datetime,
    progress_callback: Optional[Callable[[Any], None]] = None,
) -> list:
    """Ambil postingan (feed + reels) dari profil Instagram dalam rentang waktu."""
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
        all_medias = []
        if progress_callback:
            progress_callback(f"Mengambil feed postingan @{clean_target}...")

        try:
            feed_medias = cl.user_medias(user_id, amount=60)
            all_medias.extend(feed_medias)
            if progress_callback:
                progress_callback(f"Ditemukan {len(feed_medias)} postingan feed.")
        except Exception as e:
            if progress_callback:
                progress_callback(f"Catatan feed: {str(e)}")

        try:
            if progress_callback:
                progress_callback(f"Mengambil video reels @{clean_target}...")
            clips = cl.user_clips(user_id, amount=40)
            all_medias.extend(clips)
            if progress_callback:
                progress_callback(f"Ditemukan {len(clips)} postingan reels.")
        except Exception as e:
            if progress_callback:
                progress_callback(f"Catatan reels: {str(e)}")

        if not all_medias:
            return []

        # Hilangkan duplikat media berdasarkan ID
        seen_ids = set()
        unique_medias = []
        for media in all_medias:
            if media.id not in seen_ids:
                seen_ids.add(media.id)
                unique_medias.append(media)

        # Urutkan berdasarkan waktu publish terbaru
        unique_medias.sort(key=lambda m: m.taken_at, reverse=True)

        if progress_callback:
            progress_callback(f"Memfilter {len(unique_medias)} total postingan berdasarkan rentang tanggal...")

        for media in unique_medias:
            post_date = media.taken_at.replace(tzinfo=None)

            # Jika lebih baru dari end_date, lewati
            if post_date > end_date:
                continue

            # Jika lebih lama dari start_date, lewati (tidak gunakan break agar aman dari pinned posts)
            if post_date < start_date:
                continue

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
    """Ambil komentar dari satu postingan dan periksa status like komentator."""
    comments = []
    try:
        caption = getattr(media, 'caption_text', '') or ''
        post_likes = getattr(media, 'like_count', 0) or 0
        post_code = getattr(media, 'code', '') or str(getattr(media, 'id', ''))
        taken_at_str = media.taken_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(media, 'taken_at') and media.taken_at else "N/A"

        # Ambil komentar dari postingan (maksimal 100 komentar per post)
        media_comments = cl.media_comments(media.id, amount=100)
        if not media_comments:
            return []

        # Ambil daftar likers (hanya jika ada like dan komentar)
        likers_set = set()
        if fetch_likers and post_likes > 0:
            try:
                media_likers = cl.media_likers(media.id)
                likers_set = {u.username.lower() for u in media_likers if u and getattr(u, 'username', None)}
            except Exception:
                # Jika likers gagal atau dibatasi Instagram, jangan gagalkan pengambilan komentar
                pass

        for comment in media_comments:
            comment_date = "N/A"
            if hasattr(comment, 'created_at_utc') and comment.created_at_utc:
                comment_date = comment.created_at_utc.strftime("%Y-%m-%d %H:%M:%S")
            elif hasattr(comment, 'created_at') and comment.created_at:
                comment_date = comment.created_at.strftime("%Y-%m-%d %H:%M:%S")

            comment_likes = getattr(comment, 'like_count', 0) or getattr(comment, 'like_count_display', 0) or 0
            commenter_user = comment.user.username if comment.user else "unknown"

            # Periksa apakah komentator me-like post
            if likers_set:
                has_liked = "Ya" if commenter_user.lower() in likers_set else "Tidak"
            else:
                has_liked = "N/A"

            comment_data = {
                "commenter_username": commenter_user,
                "comment_text": comment.text or "",
                "has_liked_post": has_liked,
                "comment_likes": comment_likes,
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
