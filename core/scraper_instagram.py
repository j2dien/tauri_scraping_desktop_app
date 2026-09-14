"""
scraper_instagram.py — Modul untuk scraping data Instagram menggunakan instagrapi.

Mengambil postingan (feed + reels) dan komentar dari profil Instagram
dalam rentang waktu tertentu secara cepat, aman, dan non-blocking.
"""

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable

from instagrapi import Client
from instagrapi.extractors import extract_media_gql, extract_media_v1
from instagrapi.exceptions import (
    LoginRequired,
    UserNotFound,
    ClientError,
)
from playwright.sync_api import sync_playwright

SESSION_DIR = Path.home() / ".instagram_sessions"

# Query web yang masih dipakai Instagram pada September 2026. Berbeda dengan
# private_graphql_clips_profile(), keduanya dikirim ke www.instagram.com dan
# dapat memakai cookie sessionid browser tanpa autentikasi endpoint mobile.
PROFILE_WEB_DOC_ID = "34579740524958711"
REELS_WEB_DOC_ID = "27234427476213202"
MEDIA_LIKERS_WEB_DOC_ID = "27928626103504365"
INSTAGRAM_SNOWFLAKE_EPOCH_MS = 1_314_220_021_721


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


def _refresh_instagram_app_profile(cl: Client) -> None:
    """Segarkan versi aplikasi dari instagrapi tanpa mengganti UUID perangkat."""
    saved_device = dict(getattr(cl, "device_settings", {}) or {})
    # Nilai ini cepat kedaluwarsa dan tidak boleh dipulihkan terus-menerus dari
    # file session lama. set_device akan mengisinya dari profil default terbaru.
    for key in ("app_version", "version_code", "bloks_versioning_id"):
        saved_device.pop(key, None)
    cl.set_device(saved_device, hydrate_app_profile=True)


def _session_user_id(sessionid: str) -> str:
    """Ambil ID pemilik sesi dari prefix cookie sessionid Instagram."""
    match = re.match(r"^(\d+)", sessionid or "")
    return match.group(1) if match else ""


def _sync_web_cookies(cl: Client) -> None:
    """Salin cookie autentikasi ke session web instagrapi."""
    for cookie in cl.private.cookies:
        cl.public.cookies.set(cookie.name, cookie.value)

    owner_id = (
        str((getattr(cl, "authorization_data", {}) or {}).get("ds_user_id") or "")
        or _session_user_id(getattr(cl, "sessionid", "") or "")
    )
    if owner_id:
        cl.public.cookies.set("ds_user_id", owner_id)


def _configure_web_session(cl: Client, sessionid: str, extra_cookies=None) -> str:
    """Pasang sessionid sebagai sesi web tanpa memvalidasi lewat API mobile."""
    owner_id = _session_user_id(sessionid)
    if not owner_id:
        raise LoginRequiredError(
            "Format Cookie Session ID tidak valid. Nilai sessionid harus diawali ID numerik akun."
        )

    cookies = dict(extra_cookies or {})
    cookies.update({"sessionid": sessionid, "ds_user_id": owner_id})
    cl.private.cookies.clear()
    cl.public.cookies.clear()
    for name, value in cookies.items():
        if value:
            cl.private.cookies.set(name, str(value))
            cl.public.cookies.set(name, str(value))

    cl.authorization_data = {
        "ds_user_id": owner_id,
        "sessionid": sessionid,
        "should_use_header_over_cookies": True,
    }
    cl.private.headers.update(cl.base_headers)
    cl.private.headers.update({"Authorization": cl.authorization})
    setattr(cl, "_instagram_web_session", True)
    return owner_id


def _validate_web_session(cl: Client) -> bool:
    """Validasi cookie melalui GraphQL web yang tidak terkena needs_upgrade."""
    sessionid = getattr(cl, "sessionid", "") or ""
    owner_id = _session_user_id(sessionid)
    if not owner_id:
        return False

    _sync_web_cookies(cl)
    data = cl.public_doc_id_graphql_request(
        REELS_WEB_DOC_ID,
        {
            "data": {
                "include_feed_video": True,
                "page_size": 1,
                "target_user_id": owner_id,
            }
        },
        referer="https://www.instagram.com/",
    )
    # Envelope ``xdt_viewer`` juga dikirim untuk pengunjung anonim dengan nilai
    # ``user: null``. Feed/Reels publik tetap dapat dibaca pada kondisi itu,
    # tetapi daftar liker tidak. Cookie baru dianggap valid jika Instagram
    # benar-benar mengembalikan identitas viewer yang sedang login.
    viewer = data.get("xdt_viewer") if isinstance(data, dict) else None
    viewer_user = viewer.get("user") if isinstance(viewer, dict) else None
    if not isinstance(viewer_user, dict) or not viewer_user:
        return False

    viewer_id = str(viewer_user.get("pk") or viewer_user.get("id") or "")
    return not viewer_id or viewer_id == owner_id


def _login_instagram_web(
    cl: Client,
    username: str,
    password: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """Login melalui endpoint web Instagram sebagai pengganti mobile login."""
    if progress_callback:
        progress_callback("Endpoint mobile tidak tersedia; mencoba login Instagram Web...")

    session = cl.public
    session.cookies.clear()
    common_headers = {
        "User-Agent": cl.public_user_agent,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.8",
        "Origin": "https://www.instagram.com",
        "Referer": "https://www.instagram.com/",
        "X-IG-App-ID": "936619743392459",
        "X-Instagram-AJAX": "1",
        "X-Requested-With": "XMLHttpRequest",
    }

    init_response = session.get(
        "https://www.instagram.com/",
        headers=common_headers,
        timeout=cl.request_timeout,
    )
    init_response.raise_for_status()
    csrf_token = session.cookies.get("csrftoken")
    if not csrf_token:
        match = re.search(r'"csrf_token":"([^"]+)"', init_response.text)
        csrf_token = match.group(1) if match else ""
    if not csrf_token:
        raise LoginRequiredError("Instagram Web tidak memberikan CSRF token. Coba gunakan Cookie Session ID.")

    login_headers = dict(common_headers)
    login_headers.update(
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRFToken": csrf_token,
        }
    )
    response = session.post(
        "https://www.instagram.com/api/v1/web/accounts/login/ajax/",
        headers=login_headers,
        data={
            "username": username,
            "enc_password": f"#PWD_INSTAGRAM_BROWSER:0:{int(time.time())}:{password}",
            "queryParams": "{}",
            "optIntoOneTap": "false",
        },
        timeout=cl.request_timeout,
    )
    try:
        result = response.json()
    except ValueError as exc:
        raise LoginRequiredError(
            f"Login Instagram Web gagal (HTTP {response.status_code}). Coba gunakan Cookie Session ID."
        ) from exc

    if result.get("two_factor_required"):
        raise LoginRequiredError(
            "Login Instagram Web memerlukan kode 2FA. Login di browser lalu gunakan Cookie Session ID."
        )
    if result.get("checkpoint_url") or result.get("checkpoint_required"):
        raise LoginRequiredError(
            "Instagram meminta checkpoint keamanan. Konfirmasi 'Ini Saya' di aplikasi Instagram, lalu coba lagi."
        )
    if not result.get("authenticated"):
        message = result.get("message") or result.get("error_type") or "autentikasi ditolak"
        raise LoginRequiredError(f"Login Instagram Web gagal: {message}")

    sessionid = session.cookies.get("sessionid") or ""
    if not sessionid:
        raise LoginRequiredError("Login web berhasil tetapi Instagram tidak memberikan cookie sessionid.")

    _configure_web_session(cl, sessionid, session.cookies.get_dict())
    cl.username = username
    return True


def _media_datetime_from_pk(media_id) -> Optional[datetime]:
    """Turunkan waktu publish dari Snowflake media ID untuk light-media Reels."""
    try:
        numeric_id = int(str(media_id).split("_", 1)[0])
        timestamp_ms = (numeric_id >> 23) + INSTAGRAM_SNOWFLAKE_EPOCH_MS
        result = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        if 2010 <= result.year <= datetime.now(timezone.utc).year + 1:
            return result
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    return None


def _enrich_reel_captions_oembed(
    cl: Client,
    reels: list,
    progress_callback: Optional[Callable[[Any], None]] = None,
) -> None:
    """Isi caption light-media Reels melalui oEmbed publik Instagram."""
    missing = [reel for reel in reels if not (getattr(reel, "caption_text", "") or "").strip()]
    enriched = 0
    for reel in missing:
        try:
            response = cl.public.get(
                "https://www.instagram.com/api/v1/oembed/",
                params={"url": get_instagram_media_url(reel)},
                headers={"User-Agent": cl.public_user_agent},
                timeout=min(int(cl.request_timeout or 20), 20),
            )
            response.raise_for_status()
            caption = str((response.json() or {}).get("title") or "").strip()
            if caption:
                reel.caption_text = caption
                enriched += 1
        except Exception:
            # Caption tidak boleh menggagalkan pengambilan post dan komentar.
            continue

    if progress_callback and missing:
        progress_callback(f"Caption reels: {enriched}/{len(missing)} berhasil dilengkapi via oEmbed.")


def _media_id(media) -> str:
    """Ambil ID media dalam bentuk string agar deduplikasi lintas endpoint konsisten."""
    # `id` hasil GraphQL kadang berbentuk "mediaId_userId", sedangkan `pk`
    # tetap mediaId murni dan cocok dengan hasil Private API.
    value = getattr(media, "pk", None) or getattr(media, "id", None)
    return str(value) if value is not None else ""


def _media_date(media) -> Optional[datetime]:
    """Normalisasi waktu Instagram menjadi waktu lokal naive untuk filter tanggal UI."""
    taken_at = getattr(media, "taken_at", None)
    if not taken_at:
        return None
    if taken_at.tzinfo is not None:
        taken_at = taken_at.astimezone()
    return taken_at.replace(tzinfo=None)


def _is_reel(media) -> bool:
    """Identifikasi reels/clips tanpa menganggap semua video feed sebagai reel."""
    product_type = str(getattr(media, "product_type", "") or "").lower()
    return product_type in {"clips", "reels"}


def get_instagram_media_url(media) -> str:
    """Bentuk URL kanonis `/reel/` atau `/p/` sesuai tipe media."""
    code = getattr(media, "code", "") or _media_id(media)
    path = "reel" if _is_reel(media) else "p"
    return f"https://www.instagram.com/{path}/{code}/"


def _extract_reels_from_graphql_payload(payload: dict) -> list:
    """Konversi node media pada respons ClipsProfileQuery menjadi objek Media."""
    reels = []
    seen_ids = set()
    stack = [payload]

    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            candidate = current.get("media") if isinstance(current.get("media"), dict) else current
            raw_id = candidate.get("pk") or candidate.get("id")
            if raw_id and not (candidate.get("taken_at") or candidate.get("taken_at_timestamp")):
                derived_date = _media_datetime_from_pk(raw_id)
                if derived_date:
                    candidate = dict(candidate)
                    candidate["taken_at"] = int(derived_date.timestamp())
            has_timestamp = bool(candidate.get("taken_at") or candidate.get("taken_at_timestamp"))
            has_media_shape = "media_type" in candidate or "__typename" in candidate

            if raw_id and has_timestamp and has_media_shape and str(raw_id) not in seen_ids:
                extracted = None
                extractors = (
                    (extract_media_gql, extract_media_v1)
                    if "__typename" in candidate
                    else (extract_media_v1, extract_media_gql)
                )
                for extractor in extractors:
                    try:
                        extracted = extractor(candidate)
                        break
                    except Exception:
                        continue

                if extracted:
                    # Query ini khusus tab reels; light-media GraphQL kadang tidak
                    # menyertakan product_type sehingga extractor menandainya feed.
                    extracted.product_type = "clips"
                    normalized_id = _media_id(extracted)
                    if normalized_id and normalized_id not in seen_ids:
                        seen_ids.add(str(raw_id))
                        seen_ids.add(normalized_id)
                        reels.append(extracted)

            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)

    return reels


def _get_reels_gql_in_range(
    cl: Client,
    user_id: str,
    start_date: datetime,
    end_date: datetime,
    target_username: str = "",
    already_found_ids: Optional[set] = None,
    progress_callback: Optional[Callable[[Any], None]] = None,
) -> list:
    """Ambil tab Reels melalui GraphQL web doc_id dengan pagination tanggal."""
    del already_found_ids  # Deduplikasi dilakukan setelah Reels diprioritaskan atas feed.
    _sync_web_cookies(cl)
    collected = []
    collected_ids = set()
    cursor = None
    seen_cursors = set()
    total_detected = 0
    page = 0

    while True:
        page += 1
        query_data = {
            "include_feed_video": True,
            "page_size": 50,
            "target_user_id": str(user_id),
        }
        if cursor:
            query_data["max_id"] = cursor

        payload = cl.public_doc_id_graphql_request(
            REELS_WEB_DOC_ID,
            {"data": query_data},
            referer=f"https://www.instagram.com/{target_username}/reels/"
            if target_username
            else "https://www.instagram.com/",
        )
        connection = (payload or {}).get("xdt_api__v1__clips__user__connection_v2") or {}
        edges = connection.get("edges") or []
        if not edges:
            break

        extracted = _extract_reels_from_graphql_payload({"edges": edges})
        total_detected += len(extracted)
        page_dates = []
        added = 0
        for reel in extracted:
            reel_date = _media_date(reel)
            reel_id = _media_id(reel)
            if not reel_date or not reel_id:
                continue
            page_dates.append(reel_date)
            if start_date <= reel_date <= end_date and reel_id not in collected_ids:
                reel.product_type = "clips"
                collected_ids.add(reel_id)
                collected.append(reel)
                added += 1

        if progress_callback:
            progress_callback(
                f"GraphQL Web reels halaman {page}: {len(edges)} node, "
                f"+{added} dalam rentang tanggal."
            )

        # Reels terurut terbaru ke terlama. Begitu item tertua melewati batas
        # awal, halaman berikutnya tidak lagi diperlukan.
        if page_dates and min(page_dates) < start_date:
            break

        page_info = connection.get("page_info") or {}
        next_cursor = page_info.get("end_cursor")
        if not page_info.get("has_next_page") or not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    collected.sort(key=_media_date, reverse=True)
    _enrich_reel_captions_oembed(cl, collected, progress_callback)
    if progress_callback:
        progress_callback(
            f"GraphQL Web reels: {total_detected} terdeteksi, "
            f"{len(collected)} dalam rentang tanggal."
        )
    return collected


def _get_profile_web_payload(
    cl: Client,
    target_username: str,
    page_size: int = 50,
    end_cursor: Optional[str] = None,
) -> dict:
    """Ambil profil/feed melalui Polaris web doc_id yang aktif."""
    _sync_web_cookies(cl)
    query_data = {
        "count": min(max(int(page_size), 1), 50),
        "include_relationship_info": True,
        "latest_besties_reel_media": True,
        "latest_reel_media": True,
    }
    if end_cursor:
        query_data["max_id"] = end_cursor

    return cl.public_doc_id_graphql_request(
        PROFILE_WEB_DOC_ID,
        {
            "data": query_data,
            "username": target_username,
            "__relay_internal__pv__PolarisFeedShareMenurelayprovider": False,
        },
        referer=f"https://www.instagram.com/{target_username}/",
    )


def _find_user_id_in_payload(payload: dict, target_username: str) -> str:
    """Cari user ID yang username-nya tepat sama di payload profil."""
    expected = target_username.lower()
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            username = str(current.get("username") or "").lower()
            user_id = current.get("pk") or current.get("id")
            if username == expected and user_id:
                return str(user_id)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return ""


def _resolve_user_id_web(cl: Client, target_username: str) -> str:
    """Resolve username dan cache halaman feed pertama dari GraphQL web."""
    payload = _get_profile_web_payload(cl, target_username)
    user_id = _find_user_id_in_payload(payload, target_username)
    if not user_id:
        raise UserNotFound("User not found", username=target_username)

    cache = getattr(cl, "_instagram_profile_payload_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(cl, "_instagram_profile_payload_cache", cache)
    cache[target_username.lower()] = payload
    return user_id


def _paginate_feed_web_by_date(
    cl: Client,
    target_username: str,
    start_date: datetime,
    end_date: datetime,
    progress_callback: Optional[Callable[[Any], None]] = None,
) -> list:
    """Ambil feed web per halaman; node Reels di grid tetap bertipe clips."""
    cache = getattr(cl, "_instagram_profile_payload_cache", {})
    payload = cache.get(target_username.lower()) if isinstance(cache, dict) else None
    cursor = None
    seen_cursors = set()
    seen_ids = set()
    collected = []
    page = 0

    while True:
        page += 1
        if payload is None:
            payload = _get_profile_web_payload(cl, target_username, end_cursor=cursor)

        connection = (payload or {}).get("xdt_api__v1__feed__user_timeline_graphql_connection") or {}
        edges = connection.get("edges") or []
        if not edges:
            break

        page_dates = []
        added = 0
        for edge in edges:
            node = (edge or {}).get("node") or {}
            candidate = node.get("media") if isinstance(node.get("media"), dict) else node
            media = None
            for extractor in (extract_media_v1, extract_media_gql):
                try:
                    media = extractor(candidate)
                    break
                except Exception:
                    continue
            if not media:
                continue

            post_date = _media_date(media)
            media_id = _media_id(media)
            if not post_date or not media_id:
                continue
            page_dates.append(post_date)
            if start_date <= post_date <= end_date and media_id not in seen_ids:
                seen_ids.add(media_id)
                collected.append(media)
                added += 1

        if progress_callback:
            progress_callback(
                f"GraphQL Web feed halaman {page}: {len(edges)} node, "
                f"+{added} dalam rentang tanggal."
            )

        # Jangan berhenti hanya karena ada pinned post lama. Berhenti ketika
        # seluruh media bertanggal pada halaman sudah lebih lama dari batas.
        if page_dates and max(page_dates) < start_date:
            break

        page_info = connection.get("page_info") or {}
        next_cursor = page_info.get("end_cursor")
        if not page_info.get("has_next_page") or not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        payload = None

    collected.sort(key=_media_date, reverse=True)
    return collected


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
            _refresh_instagram_app_profile(cl)
        except Exception:
            pass

    try:
        _configure_web_session(cl, clean_sid)
        if not _validate_web_session(cl):
            raise LoginRequiredError(
                "Cookie Session ID tidak lagi terautentikasi. Feed/Reels publik masih dapat terlihat tanpa login, "
                "tetapi status 'Sudah Like Post?' memerlukan sessionid aktif. Salin sessionid terbaru dari browser."
            )
        cl.dump_settings(session_file)
        if progress_callback:
            progress_callback("✓ Cookie Session ID valid melalui Instagram Web! Sesi telah disimpan.")
        return True
    except LoginRequiredError:
        raise
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
            _refresh_instagram_app_profile(cl)
            _sync_web_cookies(cl)
            # Validasi lewat host web agar session browser tidak ditolak oleh
            # endpoint mobile dengan login_required/needs_upgrade.
            if not _validate_web_session(cl):
                raise LoginRequiredError("Sesi web sudah kedaluwarsa.")
            setattr(cl, "_instagram_web_session", True)
            cl.username = clean_user
            if progress_callback:
                progress_callback(f"✓ Sesi web @{clean_user} masih valid! Melanjutkan...")
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

    # 2. Login baru melalui endpoint web. Endpoint mobile Instagram saat ini
    # menolak login instagrapi dengan needs_upgrade walau kredensial benar.
    try:
        if progress_callback:
            progress_callback(f"Mengirim permintaan login web untuk @{clean_user} ke Instagram...")
        _login_instagram_web(cl, clean_user, password, progress_callback)
        cl.dump_settings(session_file)
        if progress_callback:
            progress_callback("✓ Login Instagram Web berhasil! Sesi baru telah disimpan.")
        return True
    except LoginRequiredError:
        raise
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
    seen_cursors = set()
    page = 0

    while True:
        page += 1
        medias, next_cursor = cl.user_medias_paginated(
            user_id,
            amount=PAGE_SIZE,
            end_cursor=end_cursor,
        )

        if not medias:
            break

        past_range_count = 0
        dated_count = 0
        for media in medias:
            post_date = _media_date(media)
            if not post_date:
                continue
            dated_count += 1

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
        if dated_count > 0 and past_range_count == dated_count:
            if progress_callback:
                progress_callback(f"Feed: seluruh halaman {page} di luar rentang tanggal, menghentikan pagination.")
            break

        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        end_cursor = next_cursor

    return collected


def _paginate_clips_by_date(
    cl: Client,
    user_id: str,
    start_date: datetime,
    end_date: datetime,
    already_found_ids: set = None,
    progress_callback: Optional[Callable[[Any], None]] = None,
) -> list:
    """Ambil reels per halaman sampai melewati tanggal awal yang diminta."""
    del already_found_ids  # Reels diprioritaskan saat deduplikasi final.

    PAGE_SIZE = 50
    collected = []
    collected_ids = set()
    end_cursor = ""
    seen_cursors = set()
    page = 0

    while True:
        page += 1
        clips, next_cursor = cl.user_clips_paginated_v1(
            user_id,
            amount=PAGE_SIZE,
            end_cursor=end_cursor,
        )
        if not clips:
            break

        past_range_count = 0
        dated_count = 0
        added_count = 0

        for clip in clips:
            post_date = _media_date(clip)
            if not post_date:
                continue
            dated_count += 1

            if post_date < start_date:
                past_range_count += 1
                continue
            if post_date > end_date:
                continue

            clip_id = _media_id(clip)
            if not clip_id or clip_id in collected_ids:
                continue

            clip.product_type = "clips"
            collected_ids.add(clip_id)
            collected.append(clip)
            added_count += 1

        if progress_callback:
            progress_callback(
                f"Halaman reels {page}: +{added_count} reels baru, "
                f"{len(collected)} dalam rentang tanggal"
            )

        # Endpoint clips terurut terbaru → terlama. Satu halaman penuh yang
        # lebih lama dari start_date berarti halaman berikutnya juga tidak relevan.
        if dated_count > 0 and past_range_count == dated_count:
            if progress_callback:
                progress_callback(
                    f"Reels: seluruh halaman {page} melewati batas awal; pagination dihentikan."
                )
            break

        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        end_cursor = next_cursor

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
        if getattr(cl, "_instagram_web_session", False):
            user_id = _resolve_user_id_web(cl, clean_target)
        else:
            try:
                user_id = cl.user_id_from_username(clean_target)
            except Exception:
                user_id = _resolve_user_id_web(cl, clean_target)
    except UserNotFound:
        raise LoginRequiredError(f"Profil @{clean_target} tidak ditemukan. Periksa ejaan username target.")
    except (LoginRequired, ClientError) as e:
        raise LoginRequiredError(f"Gagal mengakses profil @{clean_target}: {e}")

    filtered_posts = []

    try:
        feed_posts = []
        clips_posts = []

        # ── 1. FEED: Pagination presisi per halaman ──
        if progress_callback:
            progress_callback(f"Mengambil feed postingan @{clean_target} (pagination presisi)...")

        feed_fetched = False
        if getattr(cl, "_instagram_web_session", False):
            try:
                feed_posts = _paginate_feed_web_by_date(
                    cl, clean_target, start_date, end_date, progress_callback
                )
                feed_fetched = True
                if progress_callback:
                    progress_callback(
                        f"✓ Ditemukan {len(feed_posts)} postingan grid via GraphQL Web."
                    )
            except Exception as e:
                if progress_callback:
                    progress_callback(
                        f"GraphQL Web feed gagal ({str(e)[:80]}), mencoba fallback lain..."
                    )
        else:
            try:
                feed_posts = _paginate_feed_by_date(cl, user_id, start_date, end_date, progress_callback)
                feed_fetched = True
                if progress_callback:
                    progress_callback(f"✓ Ditemukan {len(feed_posts)} postingan feed dalam rentang tanggal.")
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Private API feed gagal ({str(e)[:80]}), mencoba via GraphQL Web...")

        # Fallback utama memakai doc_id web yang masih aktif.
        if not feed_fetched:
            try:
                feed_posts = _paginate_feed_web_by_date(
                    cl, clean_target, start_date, end_date, progress_callback
                )
                feed_fetched = True
                if progress_callback:
                    progress_callback(f"✓ Ditemukan {len(feed_posts)} postingan grid via GraphQL Web.")
            except Exception as e:
                if progress_callback:
                    progress_callback(f"⚠ Gagal mengambil feed: {str(e)[:100]}")

        # ── 2. REELS/CLIPS: Ambil reels dengan filter tanggal presisi ──
        if progress_callback:
            progress_callback(f"Mengambil video reels @{clean_target}...")

        private_reels_error = None
        if not getattr(cl, "_instagram_web_session", False):
            try:
                clips_posts = _paginate_clips_by_date(
                    cl, user_id, start_date, end_date, progress_callback=progress_callback
                )
            except Exception as e:
                private_reels_error = e
                if progress_callback:
                    progress_callback(
                        f"Private API reels gagal ({str(e)[:80]}), mencoba GraphQL Web reels..."
                    )

        # Cookie Session ID adalah sesi web; endpoint mobile reels dapat menolak
        # dengan login_required walaupun login valid. Gunakan query web khusus
        # tab reels ketika Private API gagal atau mengembalikan data kosong.
        if not clips_posts:
            try:
                clips_posts = _get_reels_gql_in_range(
                    cl,
                    user_id,
                    start_date,
                    end_date,
                    target_username=clean_target,
                    progress_callback=progress_callback,
                )
            except Exception as gql_error:
                if progress_callback:
                    private_info = f"; Private API: {str(private_reels_error)[:60]}" if private_reels_error else ""
                    progress_callback(
                        f"GraphQL Web reels gagal ({str(gql_error)[:80]}){private_info}."
                    )

        if progress_callback:
            progress_callback(f"✓ Ditemukan {len(clips_posts)} video reels dalam rentang tanggal.")

        # Reels harus lebih dahulu agar media yang juga tampil di grid tetap
        # diklasifikasikan dan diekspor sebagai URL /reel/, bukan /p/.
        all_medias = clips_posts + feed_posts

        if not all_medias:
            if progress_callback:
                progress_callback("Tidak ada postingan yang ditemukan dalam rentang tanggal yang diminta.")
            return []

        # ── 3. Filter final dan deduplikasi berdasarkan media ID ──
        seen_ids = set()
        unique_medias = []
        for media in all_medias:
            post_date = _media_date(media)
            if not post_date or not (start_date <= post_date <= end_date):
                continue

            media_id = _media_id(media)
            if media_id and media_id not in seen_ids:
                seen_ids.add(media_id)
                unique_medias.append(media)

        # Urutkan berdasarkan waktu publish terbaru
        unique_medias.sort(key=_media_date, reverse=True)

        reel_count = sum(1 for media in unique_medias if _is_reel(media))
        feed_count = len(unique_medias) - reel_count

        if progress_callback:
            progress_callback(
                f"Total {len(unique_medias)} postingan unik dalam rentang tanggal "
                f"(feed: {feed_count}, reels: {reel_count})."
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


def _liker_identity_sets(users) -> tuple[set[str], set[str]]:
    """Normalisasi hasil liker GraphQL (dict) dan Private API (UserShort)."""
    usernames: set[str] = set()
    user_ids: set[str] = set()

    for item in users or []:
        source = item.get("user") or item if isinstance(item, dict) else item
        if not source:
            continue

        if isinstance(source, dict):
            username = source.get("username")
            user_id = source.get("pk") or source.get("id")
        else:
            username = getattr(source, "username", None)
            user_id = getattr(source, "pk", None) or getattr(source, "id", None)

        if username:
            usernames.add(str(username).strip().lower())
        if user_id:
            user_ids.add(str(user_id))

    return usernames, user_ids


class _InstagramLikerBrowser:
    """Ambil daftar liker dari dialog web dengan satu browser per pekerjaan.

    Instagram saat ini menyajikan PolarisPostLikedByListDialogQuery melalui
    ``/api/graphql`` dengan token dinamis halaman. Query yang sama pada endpoint
    publik lama dapat mengembalikan ``likers_connection=null``, padahal dialog
    akun yang login masih menampilkan daftar lengkapnya.
    """

    def __init__(self, cl: Client):
        self.cl = cl
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def _start(self) -> None:
        if self.page is not None:
            return

        _sync_web_cookies(self.cl)
        self.playwright = sync_playwright().start()
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ]
        last_error = None
        for channel in ("msedge", "chrome", None):
            try:
                options = {"headless": True, "args": launch_args}
                if channel:
                    options["channel"] = channel
                self.browser = self.playwright.chromium.launch(**options)
                break
            except Exception as exc:
                last_error = exc

        if self.browser is None:
            self.close()
            raise RuntimeError(f"Chrome/Edge tidak dapat dibuka: {last_error}")

        self.context = self.browser.new_context(locale="id-ID")
        browser_cookies = []
        for name, value in self.cl.public.cookies.items():
            if value:
                browser_cookies.append(
                    {
                        "name": str(name),
                        "value": str(value),
                        "domain": ".instagram.com",
                        "path": "/",
                        "secure": True,
                        "sameSite": "Lax",
                    }
                )
        if browser_cookies:
            self.context.add_cookies(browser_cookies)
        self.page = self.context.new_page()

    def lookup(self, media) -> tuple[set[str], set[str], bool, str]:
        """Buka dialog liked_by dan baca node liker dari respons GraphQL-nya."""
        try:
            self._start()
            # Tampilan /p/ tersedia untuk feed maupun Reel dan memuat kontrol
            # liked_by secara konsisten. URL ekspor tetap mengikuti tipe media.
            post_code = getattr(media, "code", "") or _media_id(media)
            post_url = f"https://www.instagram.com/p/{post_code}/"
            self.page.goto(post_url, wait_until="domcontentloaded", timeout=45_000)

            liked_by_link = self.page.locator('a[href$="/liked_by/"]')
            if liked_by_link.count() == 0:
                # Sebagian post memakai anchor ``#`` berteks "4 lainnya"
                # alih-alih URL /liked_by/. Batasi ke pola numerik agar tidak
                # salah mengklik "Lihat Postingan Lainnya".
                liked_by_link = self.page.locator('a[href="#"]').filter(
                    has_text=re.compile(r"^\s*\d+\s+(?:lainnya|others)\s*$", re.I)
                )
            try:
                liked_by_link.first.wait_for(state="attached", timeout=10_000)
            except Exception:
                if "/accounts/login" in self.page.url:
                    return set(), set(), False, "browser_session_expired"
                return set(), set(), False, "hidden_by_instagram"

            def is_liker_response(response) -> bool:
                try:
                    return (
                        response.url.rstrip("/").endswith("/api/graphql")
                        and response.request.headers.get("x-fb-friendly-name")
                        == "PolarisPostLikedByListDialogQuery"
                    )
                except Exception:
                    return False

            captured_responses = []

            def capture_response(response) -> None:
                if is_liker_response(response):
                    captured_responses.append(response)

            self.page.on("response", capture_response)
            try:
                liked_by_link.first.click()
                dialog = self.page.locator('[role="dialog"]')
                dialog.wait_for(state="visible", timeout=10_000)
                # Beri kesempatan singkat pada respons GraphQL untuk selesai.
                self.page.wait_for_timeout(1_500)
            finally:
                self.page.remove_listener("response", capture_response)

            connection = None
            if captured_responses:
                payload = captured_responses[-1].json()
                media_node = ((payload or {}).get("data") or {}).get("fetch__XDTMediaDict") or {}
                connection = media_node.get("likers_connection")

            if connection is not None:
                browser_likers = list((connection or {}).get("nodes") or [])
                for edge in (connection or {}).get("edges") or []:
                    node = (edge or {}).get("node") if isinstance(edge, dict) else None
                    if node:
                        browser_likers.append(node)

                usernames, user_ids = _liker_identity_sets(browser_likers)
                if browser_likers:
                    return usernames, user_ids, True, "browser_web"

            # Beberapa daftar liker sudah ada di payload halaman sehingga klik
            # tidak membuat request baru. Ambil username unik dari href profil
            # yang dirender di dalam dialog.
            profile_hrefs = dialog.locator('a[href^="/"]').evaluate_all(
                "elements => elements.map(el => el.getAttribute('href') || '')"
            )
            usernames = set()
            for href in profile_hrefs:
                match = re.fullmatch(r"/([A-Za-z0-9._]+)/?", str(href))
                if match:
                    usernames.add(match.group(1).lower())
            if usernames:
                return usernames, set(), True, "browser_dom"

            if connection is None:
                return set(), set(), False, "hidden_by_instagram"
            return set(), set(), False, "browser_empty"
        except Exception as exc:
            return set(), set(), False, f"browser_error:{type(exc).__name__}"

    def close(self) -> None:
        for resource in (self.page, self.context, self.browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        self.page = None
        self.context = None
        self.browser = None
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None


def _get_media_liker_identities(
    cl: Client,
    media,
    post_likes: int,
    liker_browser: Optional[_InstagramLikerBrowser] = None,
) -> tuple[set[str], set[str], bool, str]:
    """Ambil liker melalui sesi web, lalu fallback ke endpoint private/mobile.

    Nilai boolean menandakan apakah lookup benar-benar berhasil. Ini penting
    karena set kosong dapat berarti post memang tidak memiliki like, bukan
    selalu berarti request gagal.
    """
    likes_hidden = bool(getattr(media, "like_and_view_counts_disabled", False))
    if post_likes <= 0 and not likes_hidden:
        return set(), set(), True, "no_likes"

    media_id = str(getattr(media, "id", "") or getattr(media, "pk", ""))
    media_pk = media_id.split("_", 1)[0]
    errors = []

    # Query lama instagrapi (doc_id 24452425501069647) sudah ditolak Instagram.
    # PolarisPostLikedByListDialogQuery adalah query yang dipakai web Instagram
    # saat ini dan berjalan melalui endpoint /graphql/query yang sama dengan
    # pengambilan feed/Reels.
    if getattr(cl, "_instagram_web_session", False):
        try:
            _sync_web_cookies(cl)
            data = cl.public_doc_id_graphql_request(
                MEDIA_LIKERS_WEB_DOC_ID,
                {"media_id": media_pk},
                referer=get_instagram_media_url(media).replace("/reel/", "/reels/"),
                headers={"X-FB-Friendly-Name": "PolarisPostLikedByListDialogQuery"},
            )
            media_node = (data or {}).get("fetch__XDTMediaDict") or {}
            connection = media_node.get("likers_connection")
            if connection is not None:
                web_likers = list((connection or {}).get("nodes") or [])
                for edge in (connection or {}).get("edges") or []:
                    node = (edge or {}).get("node") if isinstance(edge, dict) else None
                    if node:
                        web_likers.append(node)

                usernames, user_ids = _liker_identity_sets(web_likers)
                if web_likers:
                    return usernames, user_ids, True, "web_graphql"
            errors.append("GraphQL publik tidak memberikan user liker")
        except Exception as exc:
            errors.append(f"GraphQL web: {type(exc).__name__}")

        # Endpoint /api/graphql yang dipakai UI memerlukan token Comet dinamis.
        # Browser dibuka sekali dan digunakan ulang untuk semua post dalam job.
        if liker_browser is not None:
            usernames, user_ids, ok, source = liker_browser.lookup(media)
            if ok:
                return usernames, user_ids, True, source
            errors.append(source)
            if source == "hidden_by_instagram":
                return set(), set(), False, source

        # Jangan lanjut ke endpoint mobile untuk sesi browser: selain pasti
        # ditolak, setiap retry dapat menambah puluhan detik per post.
        return set(), set(), False, "; ".join(errors)

    try:
        # Hindari media_id() lookup tambahan jika owner ID sudah ada di objek.
        private_media_id = media_id
        media_user = getattr(media, "user", None)
        owner_id = getattr(media_user, "pk", None) or getattr(media_user, "id", None)
        if "_" not in private_media_id and owner_id:
            private_media_id = f"{media_pk}_{owner_id}"

        private_likers = cl.media_likers(private_media_id)
        usernames, user_ids = _liker_identity_sets(private_likers)
        if private_likers:
            return usernames, user_ids, True, "private_api"
        errors.append("Private API mengembalikan daftar kosong")
    except Exception as exc:
        errors.append(f"Private API: {type(exc).__name__}")

    return set(), set(), False, "; ".join(errors)


def get_comments_from_post(
    cl: Client,
    media,
    fetch_likers: bool = True,
    liker_browser: Optional[_InstagramLikerBrowser] = None,
) -> list[dict]:
    """Ambil komentar dari satu postingan dan periksa status like komentator (Mendukung GraphQL & Private API)."""
    comments = []
    setattr(cl, "_last_liker_lookup", {"ok": False, "source": "not_checked", "count": 0})
    try:
        caption = getattr(media, 'caption_text', '') or ''
        post_likes = getattr(media, 'like_count', 0) or 0
        post_code = getattr(media, 'code', '') or str(getattr(media, 'id', ''))
        post_url = get_instagram_media_url(media)
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
            setattr(cl, "_last_liker_lookup", {"ok": True, "source": "no_comments", "count": 0})
            return []

        # Ambil daftar liker tanpa menggagalkan data komentar jika Instagram
        # sedang membatasi endpoint tersebut.
        liker_usernames: set[str] = set()
        liker_user_ids: set[str] = set()
        liker_lookup_ok = False
        liker_source = "disabled"
        if fetch_likers:
            liker_usernames, liker_user_ids, liker_lookup_ok, liker_source = _get_media_liker_identities(
                cl, media, int(post_likes), liker_browser=liker_browser
            )

        setattr(
            cl,
            "_last_liker_lookup",
            {
                "ok": liker_lookup_ok,
                "source": liker_source,
                "count": max(len(liker_usernames), len(liker_user_ids)),
                "post_url": post_url,
            },
        )

        for comment in media_comments:
            # Parsing data komentar, menangani baik bentuk dict (GraphQL) maupun objek Comment (Private API)
            if isinstance(comment, dict):
                user_obj = comment.get("user") or {}
                if isinstance(user_obj, dict):
                    commenter_user = user_obj.get("username") or user_obj.get("id") or "unknown"
                    commenter_user_id = str(user_obj.get("pk") or user_obj.get("id") or "")
                else:
                    commenter_user = getattr(user_obj, "username", "unknown")
                    commenter_user_id = str(
                        getattr(user_obj, "pk", None) or getattr(user_obj, "id", None) or ""
                    )

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
                commenter_user_id = str(
                    getattr(user_obj, "pk", None) or getattr(user_obj, "id", None) or ""
                ) if user_obj else ""
                comment_text = getattr(comment, 'text', '') or ""
                comment_date = "N/A"
                if hasattr(comment, 'created_at_utc') and comment.created_at_utc:
                    comment_date = comment.created_at_utc.strftime("%Y-%m-%d %H:%M:%S")
                elif hasattr(comment, 'created_at') and comment.created_at:
                    comment_date = comment.created_at.strftime("%Y-%m-%d %H:%M:%S")
                # FIX: Gunakan c_likes (bukan comment_likes) agar konsisten dengan dict output
                c_likes = getattr(comment, 'like_count', 0) or getattr(comment, 'like_count_display', 0) or 0

            # Periksa apakah komentator me-like post
            if liker_lookup_ok:
                commenter_username_key = str(commenter_user).strip().lower()
                has_liked = "Ya" if (
                    commenter_username_key in liker_usernames
                    or (commenter_user_id and commenter_user_id in liker_user_ids)
                ) else "Tidak"
            elif liker_source == "hidden_by_instagram":
                has_liked = "Disembunyikan Instagram"
            else:
                has_liked = "Tidak dapat dicek"

            comment_data = {
                "commenter_username": commenter_user,
                "comment_text": comment_text,
                "has_liked_post": has_liked,
                "comment_likes": int(c_likes) if c_likes else 0,
                "comment_date": comment_date,
                "post_shortcode": post_code,
                "post_url": post_url,
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
    liker_browser = (
        _InstagramLikerBrowser(cl)
        if getattr(cl, "_instagram_web_session", False)
        else None
    )

    try:
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

            comments = get_comments_from_post(cl, media, liker_browser=liker_browser)
            all_comments.extend(comments)

            liker_lookup = getattr(cl, "_last_liker_lookup", {}) or {}
            if liker_lookup.get("ok"):
                if liker_lookup.get("source") == "no_comments":
                    liker_message = "; tidak ada komentar untuk diperiksa"
                else:
                    liker_message = (
                        f"; status like via {liker_lookup.get('source')} "
                        f"({liker_lookup.get('count', 0)} liker terbaca)"
                    )
            elif liker_lookup.get("source") == "hidden_by_instagram":
                liker_message = "; daftar liker disembunyikan oleh pemilik post di Instagram"
            else:
                liker_message = "; status like tidak dapat dicek oleh Instagram"

            # Notifikasi setelah postingan selesai diproses
            if progress_callback:
                progress_callback(
                    i + 1,
                    total,
                    media,
                    len(all_comments),
                    f"Post {i+1}/{total} selesai: +{len(comments)} komentar "
                    f"(Total: {len(all_comments)}){liker_message}"
                )
    finally:
        if liker_browser is not None:
            liker_browser.close()

    return all_comments
