"""
scraper_instagram.py — Modul untuk scraping data Instagram menggunakan instagrapi.

Mengambil postingan (feed + reels) dan komentar dari profil Instagram
dalam rentang waktu tertentu secara cepat, aman, dan non-blocking.
"""

import re
import time
from dataclasses import dataclass, field
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
LIKER_LOOKUP_FAILURE_LIMIT = 3
COMMENT_FETCH_FAILURE_LIMIT = 3
LIKER_LOOKUP_JOB_LIMIT = 20
COMMENTS_PER_POST_LIMIT = 100
OEMBED_CAPTION_ENRICH_LIMIT = 10
OEMBED_REQUEST_INTERVAL_SECONDS = 0.5
LIKER_BROWSER_MAX_SCROLLS = 6
LIKER_BROWSER_MIN_INTERVAL_SECONDS = 1.25


class LoginRequiredError(Exception):
    """Raised ketika Instagram memblokir akses atau memerlukan interaksi/verifikasi."""
    pass


class InstagramRateLimitError(LoginRequiredError):
    """Raised ketika Instagram meminta pekerjaan dihentikan karena throttling."""


class InstagramAuthenticationError(LoginRequiredError):
    """Raised ketika sesi tidak lagi terautentikasi."""


class InstagramCommentFetchError(RuntimeError):
    """Raised untuk kegagalan per-post non-terminal yang harus dicatat caller."""


@dataclass
class LikerLookupResult:
    """Hasil lookup liker beserta bukti kelengkapan yang dapat diaudit.

    Daftar liker parsial masih berguna untuk membuktikan status ``Ya``, tetapi
    tidak pernah cukup untuk menyimpulkan ``Tidak``.
    """

    usernames: set[str] = field(default_factory=set)
    user_ids: set[str] = field(default_factory=set)
    status: str = "unavailable"
    source: str = "none"
    reason: str = ""
    errors: list[str] = field(default_factory=list)
    expected_count: Optional[int] = None
    usernames_complete: bool = False
    user_ids_complete: bool = False

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    @property
    def observed_count(self) -> int:
        # Username dan ID umumnya mewakili user yang sama. ``max`` mencegah
        # penghitungan ganda tanpa membuang identity yang hanya punya salah satu.
        return max(len(self.usernames), len(self.user_ids))

    def diagnostics(self) -> dict:
        return {
            "ok": self.status in {"complete", "partial"},
            "status": self.status,
            "complete": self.complete,
            "source": self.source,
            "reason": self.reason,
            "errors": list(self.errors),
            "count": self.observed_count,
            "expected_count": self.expected_count,
            "usernames_complete": self.usernames_complete,
            "user_ids_complete": self.user_ids_complete,
        }


def _exception_http_status(exc: BaseException) -> Optional[int]:
    """Ambil HTTP status dari exception requests/instagrapi tanpa coupling ketat."""
    for candidate in (exc, getattr(exc, "response", None)):
        if candidate is None:
            continue
        status = getattr(candidate, "status_code", None) or getattr(candidate, "status", None)
        if status is None:
            continue
        try:
            return int(status)
        except (TypeError, ValueError):
            continue
    return None


def _classify_instagram_exception(exc: BaseException) -> str:
    """Klasifikasikan error terminal agar fallback tidak memperparah pembatasan."""
    if isinstance(exc, InstagramRateLimitError):
        return "rate_limited"
    if isinstance(exc, InstagramAuthenticationError):
        return "unauthenticated"

    status = _exception_http_status(exc)
    message = f"{type(exc).__name__}: {exc}".lower()
    if status == 429 or any(
        marker in message
        for marker in (
            "rate limit",
            "ratelimit",
            "throttl",
            "too many requests",
            "feedback_required",
            "feedbackrequired",
            "please wait a few minutes",
            "pleasewaitfewminutes",
            "temporarily blocked",
            "sentry_block",
            "spam",
        )
    ):
        return "rate_limited"
    if any(
        marker in message
        for marker in ("challenge", "checkpoint", "two_factor", "2fa", "verification required")
    ):
        return "unauthenticated"
    if isinstance(exc, (LoginRequiredError, LoginRequired)) or status in {401, 403} or any(
        marker in message
        for marker in (
            "login_required",
            "login required",
            "not logged in",
            "session expired",
            "session has expired",
            "accounts/login",
        )
    ):
        return "unauthenticated"
    return "error"


def _raise_if_terminal_instagram_error(exc: BaseException, action: str) -> None:
    """Hentikan request lanjutan untuk auth/rate-limit; error biasa boleh fallback."""
    category = _classify_instagram_exception(exc)
    if category == "rate_limited":
        raise InstagramRateLimitError(
            f"Instagram membatasi permintaan saat {action}. Pekerjaan dihentikan agar pembatasan tidak bertambah; "
            "tunggu sebelum mencoba kembali."
        ) from exc
    if category == "unauthenticated":
        raise InstagramAuthenticationError(
            f"Sesi Instagram tidak lagi dapat digunakan saat {action}. Login ulang atau perbarui Cookie Session ID."
        ) from exc


def _optional_count(value: Any) -> Optional[int]:
    """Normalisasi angka tanpa mengubah nilai yang hilang menjadi nol."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        text = str(value).strip().lower()
        abbreviated = re.search(
            r"(-?\d+(?:[.,]\d+)?)\s*(rb|ribu|k|jt|juta|miliar|m|b)\b",
            text,
            flags=re.I,
        )
        if abbreviated:
            number_text, suffix = abbreviated.groups()
            factors = {
                "rb": 1_000,
                "ribu": 1_000,
                "k": 1_000,
                "jt": 1_000_000,
                "juta": 1_000_000,
                "m": 1_000_000,
                "miliar": 1_000_000_000,
                "b": 1_000_000_000,
            }
            try:
                number = float(number_text.replace(",", "."))
                return max(int(number * factors[suffix.lower()]), 0)
            except (TypeError, ValueError, OverflowError):
                return None

        match = re.search(r"-?\d[\d.,]*", text)
        if not match:
            return None
        digits = re.sub(r"\D", "", match.group(0))
        return int(digits) if digits else None


def _no_interactive_challenge(username: str, choice=None):
    """Handler non-blocking saat Instagram meminta verifikasi Challenge/2FA."""
    raise LoginRequiredError(
        f"Akun @{username} memerlukan verifikasi keamanan (Challenge/2FA via {choice or 'SMS/Email/Aplikasi'}).\n"
        "Buka akun melalui aplikasi atau browser dan selesaikan petunjuk yang muncul. "
        "Instagram tidak selalu mengirim notifikasi 'Ini Saya'."
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


def _validate_viewer_identity(
    cl: Client,
    owner_id: str,
    expected_username: str,
    viewer_id: Any,
    viewer_username: Any,
) -> bool:
    """Cocokkan identitas sesi tanpa menyimpan data privat lain dari akun."""
    normalized_id = str(viewer_id or "").strip()
    normalized_username = str(viewer_username or "").strip().lower()
    if not normalized_id or normalized_id != owner_id or not normalized_username:
        return False

    expected = expected_username.replace("@", "").strip().lower()
    if expected and normalized_username != expected:
        raise InstagramAuthenticationError(
            f"Cookie Session ID milik @{normalized_username}, bukan @{expected}. "
            "Gunakan username akun pemilik cookie, bukan username target scraping."
        )

    setattr(
        cl,
        "_instagram_viewer_identity",
        {"id": normalized_id, "username": normalized_username},
    )
    return True


def _validate_web_session_via_account_form(
    cl: Client,
    owner_id: str,
    expected_username: str,
) -> Optional[bool]:
    """Validasi cookie lewat halaman privat akun; None berarti coba fallback GraphQL.

    Endpoint ini hanya mengembalikan ``form_data`` untuk browser yang sudah
    terautentikasi. Kita hanya membaca ID/username dan tidak menyimpan email,
    nomor telepon, atau field privat lain dari respons.
    """
    request = getattr(getattr(cl, "public", None), "get", None)
    if not callable(request):
        return None

    try:
        response = request(
            "https://www.instagram.com/api/v1/accounts/edit/web_form_data/",
            headers={
                "Accept": "*/*",
                "Referer": "https://www.instagram.com/accounts/edit/",
                "User-Agent": getattr(cl, "public_user_agent", ""),
                "X-IG-App-ID": "936619743392459",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=min(int(getattr(cl, "request_timeout", 20) or 20), 20),
        )
    except Exception as exc:
        _raise_if_terminal_instagram_error(exc, "memvalidasi Cookie Session ID melalui halaman akun")
        return None

    status_code = _optional_count(getattr(response, "status_code", None))
    if status_code == 429:
        raise InstagramRateLimitError(
            "Instagram membatasi validasi Cookie Session ID (HTTP 429). Hentikan percobaan dan tunggu."
        )
    if status_code in {401, 403}:
        return False
    if status_code is not None and status_code >= 400:
        return None

    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    message = str(payload.get("message") or payload.get("error_type") or "").strip()
    message_category = _classify_instagram_exception(RuntimeError(message)) if message else "error"
    if message_category == "rate_limited":
        raise InstagramRateLimitError(
            "Instagram membatasi validasi Cookie Session ID. Hentikan percobaan dan tunggu."
        )
    if message_category == "unauthenticated":
        # ``login_required`` adalah penolakan sesi yang definitif. Challenge
        # perlu ditampilkan sebagai kondisi keamanan, bukan cookie kedaluwarsa.
        lowered_message = message.lower()
        if "challenge" in lowered_message or "checkpoint" in lowered_message:
            raise InstagramAuthenticationError(
                "Cookie diterima, tetapi Instagram meminta challenge/checkpoint pada sesi browser tersebut."
            )
        return False

    form_data = payload.get("form_data")
    if not isinstance(form_data, dict) or not form_data.get("username"):
        return None

    # Sebagian varian respons tidak mengirim ID. Karena endpoint privat ini
    # sudah membuktikan cookie aktif, gunakan prefix sessionid sebagai owner ID.
    viewer_id = form_data.get("id") or form_data.get("pk") or owner_id
    return _validate_viewer_identity(
        cl,
        owner_id,
        expected_username,
        viewer_id,
        form_data.get("username"),
    )


def _validate_web_session(cl: Client, expected_username: str = "") -> bool:
    """Validasi cookie dan cocokkan identitas viewer dengan pemilik sessionid."""
    sessionid = getattr(cl, "sessionid", "") or ""
    owner_id = _session_user_id(sessionid)
    if not owner_id:
        return False

    _sync_web_cookies(cl)
    account_form_result = _validate_web_session_via_account_form(
        cl,
        owner_id,
        expected_username,
    )
    if account_form_result is not None:
        return account_form_result

    # Fallback untuk instalasi/region yang tidak menyediakan web_form_data.
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

    return _validate_viewer_identity(
        cl,
        owner_id,
        expected_username,
        viewer_user.get("pk") or viewer_user.get("id"),
        viewer_user.get("username"),
    )


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
    if response.status_code == 429:
        raise InstagramRateLimitError(
            "Instagram membatasi percobaan login (HTTP 429). Hentikan percobaan dan tunggu sebelum mencoba lagi."
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
            "Instagram menolak login otomatis dengan status checkpoint. Notifikasi 'Ini Saya' tidak selalu muncul. "
            "Login melalui browser, selesaikan verifikasi jika ada, lalu gunakan Cookie Session ID terbaru."
        )
    if not result.get("authenticated"):
        message = result.get("message") or result.get("error_type") or "autentikasi ditolak"
        if _classify_instagram_exception(RuntimeError(str(message))) == "rate_limited":
            raise InstagramRateLimitError(
                "Instagram membatasi percobaan login. Hentikan percobaan dan tunggu sebelum mencoba lagi."
            )
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
    attempts = 0
    consecutive_failures = 0
    last_request_started = 0.0
    for reel in missing[:OEMBED_CAPTION_ENRICH_LIMIT]:
        elapsed = time.monotonic() - last_request_started
        remaining = OEMBED_REQUEST_INTERVAL_SECONDS - elapsed
        if last_request_started and remaining > 0:
            time.sleep(remaining)
        last_request_started = time.monotonic()
        attempts += 1
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
            consecutive_failures = 0
        except Exception as exc:
            # Error biasa tidak menggagalkan data utama, tetapi auth/rate-limit
            # harus menghentikan job agar tidak menambah request yang ditolak.
            _raise_if_terminal_instagram_error(exc, "melengkapi caption Reels via oEmbed")
            consecutive_failures += 1
            if consecutive_failures >= 3:
                break

    if progress_callback and missing:
        skipped = max(len(missing) - attempts, 0)
        suffix = f"; {skipped} dilewati oleh batas keamanan" if skipped else ""
        progress_callback(
            f"Caption reels: {enriched}/{attempts} request berhasil dilengkapi via oEmbed{suffix}."
        )


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
    Cookie tetap dapat kedaluwarsa, dibatasi, atau memerlukan checkpoint.
    """
    clean_sid = sessionid.strip().strip('"').strip("'")
    if not clean_sid:
        raise LoginRequiredError("Session ID tidak boleh kosong.")

    clean_user = username.replace("@", "").strip().lower() if username else "ig_session_user"
    generic_usernames = {"ig_session_user", "session_user"}
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
        expected_username = "" if clean_user in generic_usernames else clean_user
        if not _validate_web_session(cl, expected_username=expected_username):
            raise LoginRequiredError(
                "Cookie Session ID tidak lagi terautentikasi. Feed/Reels publik masih dapat terlihat tanpa login, "
                "tetapi status 'Sudah Like Post?' memerlukan sessionid aktif. Salin sessionid terbaru dari browser."
            )
        viewer_identity = getattr(cl, "_instagram_viewer_identity", {}) or {}
        viewer_username = str(viewer_identity.get("username") or "").strip().lower()
        cl.username = viewer_username or expected_username or clean_user
        # Jika username tidak diberikan, simpan sesi di bawah identitas yang
        # diverifikasi alih-alih nama placeholder yang dapat tertukar.
        verified_session_file = _session_path(viewer_username or clean_user)
        cl.dump_settings(verified_session_file)
        if progress_callback:
            identity_text = f" sebagai @{viewer_username}" if viewer_username else ""
            progress_callback(f"✓ Cookie Session ID valid melalui Instagram Web{identity_text}. Sesi telah disimpan.")
        return True
    except LoginRequiredError:
        raise
    except Exception as e:
        _raise_if_terminal_instagram_error(e, "memvalidasi Cookie Session ID")
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
            if not _validate_web_session(cl, expected_username=clean_user):
                raise LoginRequiredError("Sesi web sudah kedaluwarsa.")
            setattr(cl, "_instagram_web_session", True)
            cl.username = clean_user
            if progress_callback:
                progress_callback(f"✓ Sesi web @{clean_user} masih valid! Melanjutkan...")
            return True
        except InstagramRateLimitError:
            raise
        except Exception as exc:
            if _classify_instagram_exception(exc) == "rate_limited":
                _raise_if_terminal_instagram_error(exc, "memvalidasi sesi tersimpan")
            saved_session_error = f"{type(exc).__name__}: {exc}".lower()
            if any(
                marker in saved_session_error
                for marker in ("challenge", "checkpoint", "two_factor", "2fa", "verification required")
            ):
                try:
                    cl.dump_settings(session_file)
                except Exception:
                    pass
                raise LoginRequiredError(
                    f"Sesi tersimpan @{clean_user} memerlukan verifikasi keamanan. "
                    "Konfirmasi 'Ini Saya'/checkpoint di aplikasi Instagram, lalu coba lagi; "
                    "login password otomatis tidak dilanjutkan agar tidak menambah percobaan."
                ) from exc
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
                "1. Login melalui aplikasi atau browser pada akun tersebut.\n"
                "2. Selesaikan petunjuk keamanan jika muncul; notifikasi 'Ini Saya' tidak selalu tersedia.\n"
                "3. Gunakan Cookie Session ID terbaru dari Chrome/Edge yang sudah terautentikasi."
            )

        if "bad_password" in err_lower or "password" in err_lower:
            raise LoginRequiredError("Password Instagram yang dimasukkan salah. Periksa kembali password Anda.")
        if "rate" in err_lower or "429" in err_lower or "feedback_required" in err_lower:
            raise InstagramRateLimitError(
                "Instagram membatasi permintaan login. Hentikan percobaan dan tunggu sebelum mencoba kembali."
            )
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
            except Exception as exc:
                _raise_if_terminal_instagram_error(exc, f"mencari profil @{clean_target}")
                user_id = _resolve_user_id_web(cl, clean_target)
    except UserNotFound:
        raise LoginRequiredError(f"Profil @{clean_target} tidak ditemukan. Periksa ejaan username target.")
    except (LoginRequired, ClientError) as e:
        _raise_if_terminal_instagram_error(e, f"mencari profil @{clean_target}")
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
                _raise_if_terminal_instagram_error(e, "mengambil feed melalui GraphQL Web")
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
                _raise_if_terminal_instagram_error(e, "mengambil feed melalui Private API")
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
                _raise_if_terminal_instagram_error(e, "mengambil fallback feed")
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
                _raise_if_terminal_instagram_error(e, "mengambil reels melalui Private API")
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
                _raise_if_terminal_instagram_error(gql_error, "mengambil reels melalui GraphQL Web")
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
        _raise_if_terminal_instagram_error(e, "mengambil postingan")
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


def _liker_connection_nodes(connection: Any) -> list:
    """Ambil node liker dari dua bentuk connection GraphQL tanpa duplikasi kasar."""
    if not isinstance(connection, dict):
        return []
    nodes = list(connection.get("nodes") or [])
    nodes.extend(
        edge.get("node")
        for edge in (connection.get("edges") or [])
        if isinstance(edge, dict) and edge.get("node")
    )
    return nodes


def _liker_connection_total(connection: Any) -> Optional[int]:
    if not isinstance(connection, dict):
        return None
    for key in ("total_count", "count"):
        if key in connection:
            parsed = _optional_count(connection.get(key))
            if parsed is not None:
                return parsed
    return None


def _liker_result_from_identities(
    usernames: set[str],
    user_ids: set[str],
    *,
    source: str,
    expected_count: Optional[int],
    reached_end: bool = False,
    connection_total: Optional[int] = None,
    reason: str = "",
    errors: Optional[list[str]] = None,
) -> LikerLookupResult:
    """Tentukan kelengkapan hanya jika ada bukti eksplisit, bukan karena request sukses."""
    observed = max(len(usernames), len(user_ids))
    # ``media.like_count`` tidak dapat dijadikan bukti completeness: nilainya
    # bisa disembunyikan, stale, atau tidak sejalan dengan dialog. Hanya sinyal
    # akhir pagination dari connection yang boleh menghasilkan status complete.
    # Akhir pagination saja belum cukup bila respons tidak memberi total:
    # sebagian node dapat kehilangan username/ID. Total connection yang cocok
    # membuktikan setidaknya satu namespace identity benar-benar lengkap.
    complete = (
        reached_end
        and connection_total is not None
        and observed >= connection_total
    )
    usernames_complete = bool(
        complete and (connection_total == 0 or len(usernames) >= connection_total)
    )
    user_ids_complete = bool(
        complete and (connection_total == 0 or len(user_ids) >= connection_total)
    )

    if complete:
        status = "complete"
        result_reason = reason or "Daftar liker terkonfirmasi lengkap."
    elif observed:
        status = "partial"
        result_reason = reason or "Sebagian liker terbaca, tetapi pagination/total belum terkonfirmasi lengkap."
    else:
        status = "unavailable"
        result_reason = reason or "Instagram tidak memberikan daftar liker yang dapat diverifikasi."

    return LikerLookupResult(
        usernames=set(usernames),
        user_ids=set(user_ids),
        status=status,
        source=source,
        reason=result_reason,
        errors=list(errors or []),
        expected_count=expected_count,
        usernames_complete=usernames_complete,
        user_ids_complete=user_ids_complete,
    )


def _merge_liker_results(
    results: list[LikerLookupResult],
    *,
    source: str,
    expected_count: Optional[int],
) -> LikerLookupResult:
    """Gabungkan identity dari beberapa jalur sambil mempertahankan bukti completeness."""
    usernames: set[str] = set()
    user_ids: set[str] = set()
    errors: list[str] = []
    reasons: list[str] = []
    for result in results:
        usernames.update(result.usernames)
        user_ids.update(result.user_ids)
        errors.extend(result.errors)
        if result.reason and result.reason not in reasons:
            reasons.append(result.reason)

    complete = any(result.complete for result in results)
    usernames_complete = any(result.usernames_complete for result in results)
    user_ids_complete = any(result.user_ids_complete for result in results)
    observed = max(len(usernames), len(user_ids))

    if complete:
        status = "complete"
    elif observed:
        status = "partial"
    else:
        # Pertahankan kategori paling actionable untuk circuit breaker/log.
        statuses = {result.status for result in results}
        status = next(
            (candidate for candidate in ("rate_limited", "unauthenticated", "error", "unavailable") if candidate in statuses),
            "unavailable",
        )

    return LikerLookupResult(
        usernames=usernames,
        user_ids=user_ids,
        status=status,
        source=source,
        reason="; ".join(reasons) or "Lookup liker tidak menghasilkan data.",
        errors=list(dict.fromkeys(errors)),
        expected_count=expected_count,
        usernames_complete=usernames_complete,
        user_ids_complete=user_ids_complete,
    )


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
        self._last_lookup_started = 0.0

    def _start(self) -> None:
        if self.page is not None:
            return

        _sync_web_cookies(self.cl)
        self.playwright = sync_playwright().start()
        last_error = None
        for channel in ("msedge", "chrome", None):
            try:
                # Gunakan sandbox dan fingerprint browser normal. Flag untuk
                # menyembunyikan automation justru rapuh dan meningkatkan risiko.
                options = {"headless": True}
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

    def _pace(self) -> None:
        """Batasi navigasi dialog agar satu job tidak membanjiri endpoint liker."""
        elapsed = time.monotonic() - self._last_lookup_started
        remaining = LIKER_BROWSER_MIN_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_lookup_started = time.monotonic()

    @staticmethod
    def _dialog_usernames(dialog) -> set[str]:
        try:
            profile_hrefs = dialog.locator('a[href^="/"]').evaluate_all(
                "elements => elements.map(el => el.getAttribute('href') || '')"
            )
        except Exception:
            return set()

        usernames: set[str] = set()
        for href in profile_hrefs:
            match = re.fullmatch(r"/([A-Za-z0-9._]+)/?", str(href))
            if match:
                usernames.add(match.group(1).lower())
        return usernames

    @staticmethod
    def _security_redirect_reason(page_url: Any) -> str:
        """Kenali halaman login/challenge sebelum melakukan request tambahan."""
        normalized = str(page_url or "").lower()
        if "/challenge/" in normalized or "/checkpoint/" in normalized:
            return "Instagram mengarahkan sesi ke halaman challenge/checkpoint."
        if "/accounts/login" in normalized:
            return "Sesi browser diarahkan ke halaman login."
        return ""

    def lookup(self, media, expected_count: Optional[int] = None) -> LikerLookupResult:
        """Buka dialog liked_by dengan pagination terbatas dan bukti completeness."""
        try:
            self._start()
            self._pace()
            # Tampilan /p/ tersedia untuk feed maupun Reel dan memuat kontrol
            # liked_by secara konsisten. URL ekspor tetap mengikuti tipe media.
            post_code = getattr(media, "code", "") or _media_id(media)
            post_url = f"https://www.instagram.com/p/{post_code}/"
            navigation = self.page.goto(post_url, wait_until="domcontentloaded", timeout=45_000)
            navigation_status = getattr(navigation, "status", None)
            if navigation_status == 429:
                return LikerLookupResult(
                    status="rate_limited",
                    source="browser_web",
                    reason="Instagram mengembalikan HTTP 429 pada halaman post.",
                    expected_count=expected_count,
                )
            redirect_reason = self._security_redirect_reason(self.page.url)
            if navigation_status in {401, 403} or redirect_reason:
                return LikerLookupResult(
                    status="unauthenticated",
                    source="browser_web",
                    reason=redirect_reason or f"Halaman post mengembalikan HTTP {navigation_status}.",
                    expected_count=expected_count,
                )

            # Locator utama harus diberi waktu hydration React. Mengecek count()
            # tepat setelah domcontentloaded dapat menghasilkan false negative.
            primary_liked_by_link = self.page.locator('a[href$="/liked_by/"]')
            try:
                primary_liked_by_link.first.wait_for(state="attached", timeout=8_000)
                liked_by_link = primary_liked_by_link
            except Exception:
                # Sebagian post memakai anchor ``#`` berteks "4 lainnya"
                # alih-alih URL /liked_by/. Batasi ke pola numerik agar tidak
                # salah mengklik "Lihat Postingan Lainnya".
                fallback_liked_by_link = self.page.locator('a[href="#"]').filter(
                    has_text=re.compile(r"^\s*\d+\s+(?:lainnya|others)\s*$", re.I)
                )
                try:
                    fallback_liked_by_link.first.wait_for(state="attached", timeout=3_000)
                    liked_by_link = fallback_liked_by_link
                except Exception:
                    redirect_reason = self._security_redirect_reason(self.page.url)
                    if redirect_reason:
                        return LikerLookupResult(
                            status="unauthenticated",
                            source="browser_web",
                            reason=redirect_reason,
                            expected_count=expected_count,
                        )
                    return LikerLookupResult(
                        status="unavailable",
                        source="browser_web",
                        reason="Kontrol daftar liker tidak tersedia pada tampilan post ini.",
                        expected_count=expected_count,
                    )

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

            dom_usernames: set[str] = set()
            self.page.on("response", capture_response)
            try:
                liked_by_link.first.click()
                dialog = self.page.locator('[role="dialog"]')
                dialog.wait_for(state="visible", timeout=10_000)
                self.page.wait_for_timeout(900)

                stable_rounds = 0
                previous_observed = -1
                for _ in range(LIKER_BROWSER_MAX_SCROLLS):
                    dom_usernames.update(self._dialog_usernames(dialog))
                    observed = len(dom_usernames) + len(captured_responses)
                    stable_rounds = stable_rounds + 1 if observed == previous_observed else 0
                    previous_observed = observed
                    if stable_rounds >= 2:
                        break

                    scroll_state = dialog.evaluate(
                        """root => {
                            const candidates = [root, ...root.querySelectorAll('div')]
                                .filter(el => el.scrollHeight > el.clientHeight + 4);
                            if (!candidates.length) return { moved: false };
                            const el = candidates.reduce((a, b) =>
                                a.scrollHeight >= b.scrollHeight ? a : b);
                            const before = el.scrollTop;
                            el.scrollTop = el.scrollHeight;
                            return { moved: el.scrollTop > before };
                        }"""
                    )
                    if not (scroll_state or {}).get("moved") and stable_rounds:
                        break
                    # Pagination sengaja diberi jeda dan dibatasi agar tidak
                    # menghasilkan burst request pada satu post.
                    self.page.wait_for_timeout(850)
                dom_usernames.update(self._dialog_usernames(dialog))
            except Exception:
                redirect_reason = self._security_redirect_reason(self.page.url)
                if redirect_reason:
                    return LikerLookupResult(
                        usernames=dom_usernames,
                        status="unauthenticated",
                        source="browser_web",
                        reason=redirect_reason,
                        expected_count=expected_count,
                    )
                response_statuses = {
                    getattr(response, "status", None) for response in captured_responses
                }
                if 429 in response_statuses:
                    return LikerLookupResult(
                        usernames=dom_usernames,
                        status="rate_limited",
                        source="browser_web",
                        reason="Endpoint liker mengembalikan HTTP 429 saat dialog dibuka.",
                        expected_count=expected_count,
                    )
                denied_status = next(
                    (status for status in (401, 403) if status in response_statuses),
                    None,
                )
                if denied_status is not None:
                    return LikerLookupResult(
                        usernames=dom_usernames,
                        status="unauthenticated",
                        source="browser_web",
                        reason=f"Endpoint liker mengembalikan HTTP {denied_status} saat dialog dibuka.",
                        expected_count=expected_count,
                    )
                raise
            finally:
                self.page.remove_listener("response", capture_response)

            graphql_nodes = []
            connection_totals: list[int] = []
            connection_seen = False
            reached_end = False
            response_errors: list[str] = []
            for response in captured_responses:
                status_code = getattr(response, "status", None)
                if status_code == 429:
                    return LikerLookupResult(
                        usernames=dom_usernames,
                        status="rate_limited",
                        source="browser_web",
                        reason="Endpoint liker mengembalikan HTTP 429.",
                        expected_count=expected_count,
                    )
                if status_code in {401, 403}:
                    return LikerLookupResult(
                        usernames=dom_usernames,
                        status="unauthenticated",
                        source="browser_web",
                        reason=f"Endpoint liker mengembalikan HTTP {status_code}.",
                        expected_count=expected_count,
                    )
                try:
                    payload = response.json()
                    media_node = ((payload or {}).get("data") or {}).get("fetch__XDTMediaDict") or {}
                    connection = media_node.get("likers_connection")
                    if connection is None:
                        continue
                    connection_seen = True
                    graphql_nodes.extend(_liker_connection_nodes(connection))
                    total_count = _liker_connection_total(connection)
                    if total_count is not None:
                        connection_totals.append(total_count)
                    page_info = connection.get("page_info") or {}
                    if page_info.get("has_next_page") is False:
                        reached_end = True
                except Exception as exc:
                    response_errors.append(f"Respons browser tidak dapat dibaca: {type(exc).__name__}")

            gql_usernames, gql_user_ids = _liker_identity_sets(graphql_nodes)
            connection_total = max(connection_totals) if connection_totals else None
            source = "browser_web" if captured_responses else "browser_dom"
            if connection_seen:
                return _liker_result_from_identities(
                    gql_usernames,
                    gql_user_ids,
                    source=source,
                    expected_count=expected_count,
                    reached_end=reached_end,
                    connection_total=connection_total,
                    errors=response_errors,
                )

            # DOM hanya dipakai sebagai bukti membership positif. Tanpa
            # connection/page_info terstruktur, ia tidak pernah membuktikan
            # bahwa user yang tidak terlihat benar-benar tidak memberi like.
            if dom_usernames:
                return LikerLookupResult(
                    usernames=dom_usernames,
                    status="partial",
                    source="browser_dom",
                    reason="Identity liker terlihat di dialog, tetapi kelengkapan pagination tidak dapat dibuktikan.",
                    errors=response_errors,
                    expected_count=expected_count,
                )

            if response_errors:
                return LikerLookupResult(
                    status="error",
                    source=source,
                    reason="Respons daftar liker tidak dapat diproses.",
                    errors=response_errors,
                    expected_count=expected_count,
                )
            return LikerLookupResult(
                status="unavailable",
                source=source,
                reason="Dialog terbuka tetapi Instagram tidak memberikan identity liker.",
                expected_count=expected_count,
            )
        except Exception as exc:
            category = _classify_instagram_exception(exc)
            return LikerLookupResult(
                status=category,
                source="browser_web",
                reason=f"Browser lookup gagal ({type(exc).__name__}).",
                errors=[f"{type(exc).__name__}: {str(exc)[:160]}"],
                expected_count=expected_count,
            )

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
    post_likes: Optional[int],
    liker_browser: Optional[_InstagramLikerBrowser] = None,
) -> LikerLookupResult:
    """Ambil liker dengan cache per media dan bukti kelengkapan eksplisit."""
    likes_hidden = bool(getattr(media, "like_and_view_counts_disabled", False))
    media_id = str(getattr(media, "id", "") or getattr(media, "pk", ""))
    media_pk = media_id.split("_", 1)[0]
    cache_key = (media_pk, post_likes, likes_hidden)
    cache = getattr(cl, "_instagram_liker_lookup_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(cl, "_instagram_liker_lookup_cache", cache)
    cached = cache.get(cache_key)
    if isinstance(cached, LikerLookupResult):
        return cached

    if post_likes == 0 and not likes_hidden:
        # Count media pernah terbukti tidak konsisten pada respons Instagram;
        # nol menghindarkan request tambahan, tetapi bukan bukti non-membership.
        result = LikerLookupResult(
            status="not_checked",
            source="post_like_count",
            reason="Jumlah like post bernilai 0, tetapi daftar identity liker tidak diverifikasi.",
            expected_count=0,
        )
        cache[cache_key] = result
        return result

    attempts: list[LikerLookupResult] = []

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
                referer=get_instagram_media_url(media),
                headers={"X-FB-Friendly-Name": "PolarisPostLikedByListDialogQuery"},
            )
            media_node = (data or {}).get("fetch__XDTMediaDict") or {}
            connection = media_node.get("likers_connection")
            if connection is not None:
                web_likers = _liker_connection_nodes(connection)
                usernames, user_ids = _liker_identity_sets(web_likers)
                page_info = connection.get("page_info") or {}
                attempts.append(
                    _liker_result_from_identities(
                        usernames,
                        user_ids,
                        source="web_graphql",
                        expected_count=post_likes,
                        reached_end=page_info.get("has_next_page") is False,
                        connection_total=_liker_connection_total(connection),
                    )
                )
            else:
                attempts.append(
                    LikerLookupResult(
                        status="unavailable",
                        source="web_graphql",
                        reason="GraphQL Web tidak menyediakan likers_connection.",
                        expected_count=post_likes,
                    )
                )
        except Exception as exc:
            _raise_if_terminal_instagram_error(exc, "mengambil daftar liker melalui GraphQL Web")
            attempts.append(
                LikerLookupResult(
                    status="error",
                    source="web_graphql",
                    reason="GraphQL Web gagal mengambil daftar liker.",
                    errors=[f"{type(exc).__name__}: {str(exc)[:160]}"],
                    expected_count=post_likes,
                )
            )

        if attempts and attempts[-1].complete:
            cache[cache_key] = attempts[-1]
            return attempts[-1]

        # Endpoint /api/graphql yang dipakai UI memerlukan token Comet dinamis.
        # Browser dibuka sekali dan digunakan ulang untuk semua post dalam job.
        if liker_browser is not None:
            browser_result = liker_browser.lookup(media, expected_count=post_likes)
            if browser_result.status == "rate_limited":
                raise InstagramRateLimitError(browser_result.reason)
            if browser_result.status == "unauthenticated":
                raise InstagramAuthenticationError(browser_result.reason)
            attempts.append(browser_result)

        # Jangan lanjut ke endpoint mobile untuk sesi browser: selain pasti
        # ditolak, setiap retry dapat menambah puluhan detik per post.
        result = _merge_liker_results(
            attempts,
            source=" + ".join(dict.fromkeys(item.source for item in attempts)),
            expected_count=post_likes,
        )
        cache[cache_key] = result
        return result

    try:
        # Hindari media_id() lookup tambahan jika owner ID sudah ada di objek.
        private_media_id = media_id
        media_user = getattr(media, "user", None)
        owner_id = getattr(media_user, "pk", None) or getattr(media_user, "id", None)
        if "_" not in private_media_id and owner_id:
            private_media_id = f"{media_pk}_{owner_id}"

        private_likers = cl.media_likers(private_media_id)
        usernames, user_ids = _liker_identity_sets(private_likers)
        attempts.append(
            _liker_result_from_identities(
                usernames,
                user_ids,
                source="private_api",
                expected_count=post_likes,
                reason=(
                    "Private API mengembalikan identity liker, tetapi kelengkapan hanya dapat "
                    "dipastikan bila jumlahnya cocok dengan total post."
                    if private_likers
                    else "Private API mengembalikan daftar kosong tanpa bukti bahwa post tidak memiliki like."
                ),
            )
        )
    except Exception as exc:
        _raise_if_terminal_instagram_error(exc, "mengambil daftar liker melalui Private API")
        attempts.append(
            LikerLookupResult(
                status="error",
                source="private_api",
                reason="Private API gagal mengambil daftar liker.",
                errors=[f"{type(exc).__name__}: {str(exc)[:160]}"],
                expected_count=post_likes,
            )
        )

    result = _merge_liker_results(attempts, source="private_api", expected_count=post_likes)
    cache[cache_key] = result
    return result


def get_comments_from_post(
    cl: Client,
    media,
    fetch_likers: bool = True,
    liker_browser: Optional[_InstagramLikerBrowser] = None,
    liker_skip_reason: str = "",
) -> list[dict]:
    """Ambil komentar serta status like dengan semantik tri-state yang aman."""
    comments = []
    setattr(
        cl,
        "_last_liker_lookup",
        LikerLookupResult(status="not_checked", source="not_checked", reason="Belum diperiksa.").diagnostics(),
    )
    caption = getattr(media, "caption_text", "") or ""
    post_likes = _optional_count(getattr(media, "like_count", None))
    post_code = getattr(media, "code", "") or str(getattr(media, "id", ""))
    post_url = get_instagram_media_url(media)
    taken_at = getattr(media, "taken_at", None)
    taken_at_str = taken_at.strftime("%Y-%m-%d %H:%M:%S") if taken_at else "N/A"
    media_id = str(getattr(media, "id", "") or getattr(media, "pk", ""))
    expected_comment_count = _optional_count(getattr(media, "comment_count", None))

    media_comments = []
    comment_errors: list[str] = []
    successful_comment_request = False
    comment_source = "none"
    # Endpoint kosong belum tentu error (post memang dapat tidak memiliki
    # komentar), sehingga fallback dicoba tanpa menghapus diagnostik error.
    comment_methods = [("graphql", getattr(cl, "media_comments_gql", None))]
    if not getattr(cl, "_instagram_web_session", False):
        comment_methods.append(("private_api", getattr(cl, "media_comments", None)))
    for source, method in comment_methods:
        if not callable(method):
            comment_errors.append(f"{source}: method tidak tersedia")
            continue
        try:
            candidate_comments = method(media_id, amount=COMMENTS_PER_POST_LIMIT) or []
            successful_comment_request = True
            if candidate_comments:
                media_comments = list(candidate_comments)
                comment_source = source
                break
            comment_source = source
            # Respons kosong adalah hasil final bila metadata juga nol/tidak
            # tersedia. Fallback tambahan hanya layak bila ada bukti komentar.
            if expected_comment_count in {None, 0}:
                break
        except Exception as exc:
            _raise_if_terminal_instagram_error(exc, f"mengambil komentar post {post_code}")
            comment_errors.append(f"{source}: {type(exc).__name__}: {str(exc)[:160]}")

    if not media_comments:
        expected_comments_missing = bool(expected_comment_count and expected_comment_count > 0)
        if not successful_comment_request or expected_comments_missing:
            setattr(cl, "_last_comment_lookup", {"ok": False, "source": "none", "errors": comment_errors})
            count_detail = (
                f"; metadata post menyebut {expected_comment_count} komentar tetapi endpoint mengembalikan kosong"
                if expected_comments_missing
                else ""
            )
            error_detail = "; ".join(comment_errors) or "endpoint mengembalikan data kosong"
            raise InstagramCommentFetchError(
                f"Komentar tidak dapat diverifikasi untuk post {post_code}{count_detail}: "
                + error_detail
            )
        no_comment_lookup = LikerLookupResult(
            status="not_checked",
            source="no_comments",
            reason="Tidak ada komentar sehingga daftar liker tidak diminta.",
            expected_count=post_likes,
        )
        diagnostics = no_comment_lookup.diagnostics()
        diagnostics["post_url"] = post_url
        setattr(cl, "_last_liker_lookup", diagnostics)
        setattr(
            cl,
            "_last_comment_lookup",
            {
                "ok": True,
                "source": comment_source or "empty",
                "status": "complete" if expected_comment_count == 0 else "unknown",
                "complete": expected_comment_count == 0,
                "expected_count": expected_comment_count,
                "observed_count": 0,
                "reason": (
                    "Metadata post memastikan tidak ada komentar."
                    if expected_comment_count == 0
                    else "Endpoint mengembalikan kosong dan metadata total komentar tidak tersedia."
                ),
                "errors": comment_errors,
            },
        )
        return []

    observed_comment_count = len(media_comments)
    if expected_comment_count is None:
        comment_lookup_status = "unknown"
        comment_lookup_complete = None
        comment_lookup_reason = "Jumlah komentar total tidak tersedia untuk membuktikan kelengkapan."
    elif observed_comment_count >= expected_comment_count:
        comment_lookup_status = "complete"
        comment_lookup_complete = True
        comment_lookup_reason = "Jumlah komentar terbaca memenuhi metadata total post."
    else:
        comment_lookup_status = "partial"
        comment_lookup_complete = False
        comment_lookup_reason = (
            f"{observed_comment_count}/{expected_comment_count} komentar terbaca; "
            f"pengambilan dibatasi {COMMENTS_PER_POST_LIMIT} komentar per post."
        )
    setattr(
        cl,
        "_last_comment_lookup",
        {
            "ok": True,
            "source": comment_source,
            "status": comment_lookup_status,
            "complete": comment_lookup_complete,
            "expected_count": expected_comment_count,
            "observed_count": observed_comment_count,
            "reason": comment_lookup_reason,
            "errors": comment_errors,
        },
    )

    if fetch_likers:
        liker_result = _get_media_liker_identities(
            cl, media, post_likes, liker_browser=liker_browser
        )
    else:
        liker_result = LikerLookupResult(
            status="not_checked",
            source="circuit_breaker" if liker_skip_reason else "disabled",
            reason=liker_skip_reason or "Pemeriksaan liker dinonaktifkan.",
            expected_count=post_likes,
        )

    diagnostics = liker_result.diagnostics()
    diagnostics["post_url"] = post_url
    setattr(cl, "_last_liker_lookup", diagnostics)

    for comment in media_comments:
        # Parsing data komentar, menangani dict GraphQL maupun objek Comment.
        if isinstance(comment, dict):
            user_obj = comment.get("user") or {}
            if isinstance(user_obj, dict):
                raw_commenter_username = user_obj.get("username")
                commenter_user = raw_commenter_username or user_obj.get("id") or "unknown"
                commenter_user_id = str(user_obj.get("pk") or user_obj.get("id") or "")
            else:
                raw_commenter_username = getattr(user_obj, "username", None)
                commenter_user = raw_commenter_username or "unknown"
                commenter_user_id = str(
                    getattr(user_obj, "pk", None) or getattr(user_obj, "id", None) or ""
                )

            comment_text = comment.get("text") or comment.get("caption") or ""
            if "comment_like_count" in comment:
                raw_comment_likes = comment.get("comment_like_count")
            elif "like_count" in comment:
                raw_comment_likes = comment.get("like_count")
            else:
                raw_comment_likes = None
            c_likes = _optional_count(raw_comment_likes)
            c_ts = comment.get("created_at") or comment.get("created_at_utc")
        else:
            user_obj = getattr(comment, "user", None)
            raw_commenter_username = getattr(user_obj, "username", None) if user_obj else None
            commenter_user = raw_commenter_username or "unknown"
            commenter_user_id = str(
                getattr(user_obj, "pk", None) or getattr(user_obj, "id", None) or ""
            ) if user_obj else ""
            comment_text = getattr(comment, "text", "") or ""
            c_ts = getattr(comment, "created_at_utc", None) or getattr(comment, "created_at", None)
            raw_comment_likes = getattr(comment, "like_count", None)
            if raw_comment_likes is None:
                raw_comment_likes = getattr(comment, "like_count_display", None)
            c_likes = _optional_count(raw_comment_likes)

        if isinstance(c_ts, datetime):
            comment_date = c_ts.strftime("%Y-%m-%d %H:%M:%S")
        elif c_ts:
            try:
                comment_date = datetime.fromtimestamp(int(c_ts)).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError, OverflowError):
                comment_date = str(c_ts)
        else:
            comment_date = "N/A"

        commenter_user = str(commenter_user or "unknown").strip() or "unknown"
        commenter_username_key = commenter_user.lower()
        identity_matched = (
            commenter_username_key in liker_result.usernames
            or bool(commenter_user_id and commenter_user_id in liker_result.user_ids)
        )
        commenter_has_username = bool(str(raw_commenter_username or "").strip())
        commenter_has_user_id = bool(commenter_user_id)
        identity_comparable = (
            (commenter_has_username and liker_result.usernames_complete)
            or (commenter_has_user_id and liker_result.user_ids_complete)
            or (
                liker_result.complete
                and liker_result.observed_count == 0
                and (commenter_has_username or commenter_has_user_id)
            )
        )
        if identity_matched:
            # Membership positif tetap valid walau daftar baru parsial.
            has_liked = "Ya"
        elif liker_result.complete and identity_comparable:
            has_liked = "Tidak"
        else:
            has_liked = "Belum dapat diverifikasi"

        like_lookup_reason = liker_result.reason
        if liker_result.complete and not identity_matched and not identity_comparable:
            like_lookup_reason = (
                f"{like_lookup_reason} Identity komentar tidak tersedia dalam namespace "
                "username/ID yang terbukti lengkap."
            ).strip()

        comment_data = {
            "commenter_username": commenter_user,
            "comment_text": comment_text,
            "has_liked_post": has_liked,
            "comment_likes": c_likes,
            "comment_date": comment_date,
            "post_shortcode": post_code,
            "post_url": post_url,
            "post_likes": post_likes,
            "post_date": taken_at_str,
            "post_caption": (caption[:100] + "...") if caption and len(caption) > 100 else caption,
            "comment_lookup_status": comment_lookup_status,
            "comment_lookup_reason": comment_lookup_reason,
            "post_comment_count_expected": expected_comment_count,
            "post_comment_count_observed": observed_comment_count,
            "like_lookup_status": liker_result.status,
            "like_lookup_source": liker_result.source,
            "like_lookup_reason": like_lookup_reason,
            "liker_count_observed": liker_result.observed_count,
            "liker_lookup_complete": liker_result.complete,
            "liker_usernames_complete": liker_result.usernames_complete,
            "liker_user_ids_complete": liker_result.user_ids_complete,
        }
        comments.append(comment_data)

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
    consecutive_liker_failures = 0
    consecutive_comment_failures = 0
    liker_lookup_attempts = 0
    liker_circuit_reason = ""
    job_diagnostics = {
        "comment_errors": [],
        "comment_truncations": [],
        "comment_unknowns": [],
        "comments_per_post_limit": COMMENTS_PER_POST_LIMIT,
        "comment_circuit_open": False,
        "comment_circuit_reason": "",
        "liker_circuit_open": False,
        "liker_circuit_reason": "",
        "liker_lookup_attempts": 0,
        "liker_lookup_limit": LIKER_LOOKUP_JOB_LIMIT,
    }
    setattr(cl, "_instagram_job_diagnostics", job_diagnostics)

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

            if not liker_circuit_reason and liker_lookup_attempts >= LIKER_LOOKUP_JOB_LIMIT:
                liker_circuit_reason = (
                    f"Pemeriksaan liker dibatasi maksimal {LIKER_LOOKUP_JOB_LIMIT} post per pekerjaan "
                    "untuk mengurangi request berulang ke Instagram."
                )
                job_diagnostics["liker_circuit_open"] = True
                job_diagnostics["liker_circuit_reason"] = liker_circuit_reason
                if liker_browser is not None:
                    liker_browser.close()
                    liker_browser = None

            fetch_likers_for_post = not bool(liker_circuit_reason)
            try:
                comments = get_comments_from_post(
                    cl,
                    media,
                    fetch_likers=fetch_likers_for_post,
                    liker_browser=liker_browser,
                    liker_skip_reason=liker_circuit_reason,
                )
            except InstagramCommentFetchError as exc:
                consecutive_comment_failures += 1
                error_item = {"post": str(post_code), "error": str(exc)}
                job_diagnostics["comment_errors"].append(error_item)
                if consecutive_comment_failures >= COMMENT_FETCH_FAILURE_LIMIT:
                    circuit_reason = (
                        f"Pengambilan komentar dihentikan setelah {consecutive_comment_failures} kegagalan beruntun "
                        "untuk mencegah request berulang ke Instagram."
                    )
                    job_diagnostics["comment_circuit_open"] = True
                    job_diagnostics["comment_circuit_reason"] = circuit_reason
                    if progress_callback:
                        progress_callback(
                            i + 1,
                            total,
                            media,
                            len(all_comments),
                            circuit_reason,
                        )
                    break
                if progress_callback:
                    progress_callback(
                        i + 1,
                        total,
                        media,
                        len(all_comments),
                        f"Post {i+1}/{total} dilewati: komentar tidak dapat diambil ({str(exc)[:160]}).",
                    )
                continue
            consecutive_comment_failures = 0
            all_comments.extend(comments)

            comment_lookup = getattr(cl, "_last_comment_lookup", {}) or {}
            comment_message = ""
            if comment_lookup.get("status") == "partial":
                truncation = {
                    "post": str(post_code),
                    "expected_count": comment_lookup.get("expected_count"),
                    "observed_count": comment_lookup.get("observed_count"),
                    "reason": comment_lookup.get("reason"),
                }
                job_diagnostics["comment_truncations"].append(truncation)
                comment_message = (
                    f"; komentar parsial {comment_lookup.get('observed_count', 0)}/"
                    f"{comment_lookup.get('expected_count', '?')} terbaca"
                )
            elif comment_lookup.get("status") == "unknown":
                unknown_item = {
                    "post": str(post_code),
                    "observed_count": comment_lookup.get("observed_count"),
                    "reason": comment_lookup.get("reason"),
                }
                job_diagnostics["comment_unknowns"].append(unknown_item)
                comment_message = "; kelengkapan komentar tidak dapat diverifikasi"

            liker_lookup = getattr(cl, "_last_liker_lookup", {}) or {}
            lookup_status = str(liker_lookup.get("status") or "unavailable")
            lookup_source = str(liker_lookup.get("source") or "")
            if fetch_likers_for_post and lookup_source not in {
                "no_comments",
                "post_like_count",
                "not_checked",
                "disabled",
                "circuit_breaker",
            }:
                liker_lookup_attempts += 1
                job_diagnostics["liker_lookup_attempts"] = liker_lookup_attempts
            if lookup_status == "complete":
                consecutive_liker_failures = 0
                liker_message = (
                    f"; daftar liker lengkap via {liker_lookup.get('source')} "
                    f"({liker_lookup.get('count', 0)} liker terbaca)"
                )
            elif lookup_status == "partial":
                consecutive_liker_failures = 0
                liker_message = (
                    f"; daftar liker parsial via {liker_lookup.get('source')} "
                    f"({liker_lookup.get('count', 0)} terbaca; hasil negatif tidak disimpulkan)"
                )
            elif lookup_status == "not_checked" and liker_lookup.get("source") == "no_comments":
                liker_message = "; tidak ada komentar untuk diperiksa"
            elif lookup_status == "not_checked" and liker_lookup.get("source") == "post_like_count":
                liker_message = "; jumlah like post 0, tetapi daftar identity tidak diverifikasi"
            elif lookup_status == "not_checked":
                liker_message = "; pemeriksaan liker dilewati karena circuit breaker"
            else:
                consecutive_liker_failures += 1
                liker_message = (
                    f"; daftar liker belum dapat diverifikasi ({lookup_status}: "
                    f"{str(liker_lookup.get('reason') or 'tanpa detail')[:120]})"
                )

                if consecutive_liker_failures >= LIKER_LOOKUP_FAILURE_LIMIT and not liker_circuit_reason:
                    liker_circuit_reason = (
                        f"Pemeriksaan liker dihentikan setelah {consecutive_liker_failures} kegagalan beruntun "
                        "untuk mencegah request berulang."
                    )
                    job_diagnostics["liker_circuit_open"] = True
                    job_diagnostics["liker_circuit_reason"] = liker_circuit_reason
                    liker_message += f"; {liker_circuit_reason}"
                    if liker_browser is not None:
                        liker_browser.close()
                        liker_browser = None

            # Notifikasi setelah postingan selesai diproses
            if progress_callback:
                progress_callback(
                    i + 1,
                    total,
                    media,
                    len(all_comments),
                    f"Post {i+1}/{total} selesai: +{len(comments)} komentar "
                    f"(Total: {len(all_comments)}){comment_message}{liker_message}"
                )
    finally:
        if liker_browser is not None:
            liker_browser.close()

    return all_comments
