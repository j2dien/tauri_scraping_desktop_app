"""
analyzer.py — Modul untuk menganalisis dan mengagregasi data komentar.

Menghitung top commenters berdasarkan frekuensi komentar
dan menyiapkan data ringkasan.
"""

from datetime import datetime


LIKE_STATUS_YES = "yes"
LIKE_STATUS_NO = "no"
LIKE_STATUS_UNKNOWN = "unknown"
LIKE_STATUS_NOT_APPLICABLE = "not_applicable"


def _post_key(comment: dict):
    """Return the best available stable key for a comment's post."""
    return comment.get("post_url") or comment.get("post_shortcode")


def _classify_like_status(comment: dict) -> str:
    """Classify a per-comment like result without turning uncertainty into "no".

    A positive match is useful even when the liker list is partial. A negative
    match, on the other hand, is only conclusive when the scraper explicitly
    says that it inspected the complete liker list. Older records do not have
    that completeness evidence, so their ``Tidak`` value remains unknown.
    """
    raw_status = str(comment.get("has_liked_post") or "").strip().casefold()
    lookup_status = str(
        comment.get("like_lookup_status")
        or comment.get("liker_lookup_status")  # compatibility with early records
        or ""
    ).strip().casefold()
    lookup_complete = (
        comment.get("liker_lookup_complete") is True or lookup_status == "complete"
    )

    if raw_status in {"ya", "yes", "true"}:
        return LIKE_STATUS_YES

    if raw_status in {"tidak", "no", "false"}:
        return LIKE_STATUS_NO if lookup_complete else LIKE_STATUS_UNKNOWN

    if raw_status in {"n/a", "na", "not applicable", "tidak berlaku"}:
        return LIKE_STATUS_NOT_APPLICABLE

    # This includes the current safe label ("Belum dapat diverifikasi"), old
    # labels such as "Disembunyikan Instagram"/"Tidak dapat dicek", and any
    # partial or unavailable lookup result.
    return LIKE_STATUS_UNKNOWN


def _merge_post_like_status(current: str | None, incoming: str) -> str:
    """Merge duplicate observations for one post, preferring hard evidence."""
    if current is None:
        return incoming

    # Finding the username is direct evidence and wins over every other state.
    if LIKE_STATUS_YES in {current, incoming}:
        return LIKE_STATUS_YES

    # A confirmed complete-list negative is stronger than an unavailable
    # duplicate observation for the same post.
    if LIKE_STATUS_NO in {current, incoming}:
        return LIKE_STATUS_NO

    if LIKE_STATUS_UNKNOWN in {current, incoming}:
        return LIKE_STATUS_UNKNOWN

    return LIKE_STATUS_NOT_APPLICABLE


def _known_number(value):
    """Return a numeric value, or None when a metric was not supplied."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _like_status_display(
    liked_count: int,
    not_liked_count: int,
    unverified_count: int,
    not_applicable_count: int,
) -> str:
    """Build a compact UI label while keeping its denominator truthful."""
    checkable_count = liked_count + not_liked_count
    unavailable_count = unverified_count + not_applicable_count

    if checkable_count == 0:
        if unverified_count == 0 and not_applicable_count > 0:
            return "N/A"
        return "Belum dapat diverifikasi"

    if unavailable_count > 0:
        if liked_count > 0:
            return (
                f"Ya ({liked_count}/{checkable_count} yang dapat dicek; "
                f"{unavailable_count} belum dapat diverifikasi)"
            )
        return (
            f"Belum dapat diverifikasi (0/{checkable_count} yang dapat dicek; "
            f"{unavailable_count} belum dapat diverifikasi)"
        )

    if liked_count > 0:
        return f"Ya ({liked_count}/{checkable_count})"
    return "Tidak"


def count_top_commenters(comments: list[dict], top_n: int = 10) -> list[dict]:
    """
    Hitung top commenters berdasarkan banyak komentar, like, dan waktu komentar pertama (tercepat).

    Urutan perangkingan:
    1. Jumlah Komentar (banyak komentar yang ditulis) - Terbanyak
    2. Status Like Postingan (komentator yang sudah me-like post) - Terbanyak
    3. Total Like Postingan (akumulasi like dari post yang dikomentari) - Terbanyak
    4. Total Like Komentar (jumlah like yang didapat komentar user) - Terbanyak
    5. Waktu Komentar Pertama (earliest comment timestamp) - Tercepat/Paling Awal

    Args:
        comments: List of dict komentar dari scraper.
        top_n: Jumlah top commenters yang ingin ditampilkan.

    Returns:
        List of dict berisi ranking top commenters.
    """
    if not comments:
        return []

    # Kelompokkan komentar per user
    user_groups = {}
    for c in comments:
        username = str(c.get("commenter_username") or "unknown")
        if username not in user_groups:
            user_groups[username] = []
        user_groups[username].append(c)

    users_stats = []
    for username, user_comments in user_groups.items():
        comment_count = len(user_comments)

        # Hitung unique posts dan total likes dari post-post yang dikomentari
        unique_post_likes = {}
        for c in user_comments:
            p_key = _post_key(c)
            if not p_key:
                continue
            like_count = _known_number(c.get("post_likes"))
            if p_key not in unique_post_likes or (
                unique_post_likes[p_key] is None and like_count is not None
            ):
                unique_post_likes[p_key] = like_count

        known_post_likes = [value for value in unique_post_likes.values() if value is not None]
        unknown_post_likes_count = len(unique_post_likes) - len(known_post_likes)
        total_post_likes = sum(known_post_likes) if known_post_likes else None
        known_comment_likes = [
            value
            for c in user_comments
            if (value := _known_number(c.get("comment_likes"))) is not None
        ]
        unknown_comment_likes_count = len(user_comments) - len(known_comment_likes)
        # None means the metric was unavailable for every comment. For a mixed
        # result, expose the known subtotal together with completeness metadata.
        total_comment_likes = (
            sum(known_comment_likes) if known_comment_likes else None
        )
        unique_urls = list({c["post_url"] for c in user_comments if c.get("post_url")})
        unique_posts = list({c["post_shortcode"] for c in user_comments if c.get("post_shortcode")})

        # Hitung per post unik agar beberapa komentar pada post yang sama tidak
        # menggandakan jumlah like. Status yang disembunyikan Instagram tidak
        # boleh dianggap sebagai "Tidak".
        like_status_by_post = {}
        for c in user_comments:
            p_key = _post_key(c)
            if not p_key:
                continue
            like_status_by_post[p_key] = _merge_post_like_status(
                like_status_by_post.get(p_key),
                _classify_like_status(c),
            )

        liked_posts_count = sum(
            1 for status in like_status_by_post.values() if status == LIKE_STATUS_YES
        )
        not_liked_posts_count = sum(
            1 for status in like_status_by_post.values() if status == LIKE_STATUS_NO
        )
        unverified_posts_count = sum(
            1 for status in like_status_by_post.values() if status == LIKE_STATUS_UNKNOWN
        )
        not_applicable_posts_count = sum(
            1
            for status in like_status_by_post.values()
            if status == LIKE_STATUS_NOT_APPLICABLE
        )
        checkable_posts_count = liked_posts_count + not_liked_posts_count
        like_status_display = _like_status_display(
            liked_posts_count,
            not_liked_posts_count,
            unverified_posts_count,
            not_applicable_posts_count,
        )

        # Cari waktu komentar pertama (tercepat) dari user ini
        comment_datetimes = []
        for c in user_comments:
            c_d = c.get("comment_date")
            if c_d and c_d != "N/A":
                try:
                    dt = datetime.strptime(c_d, "%Y-%m-%d %H:%M:%S")
                    comment_datetimes.append(dt)
                except ValueError:
                    pass

        earliest_comment_ts = min(comment_datetimes) if comment_datetimes else datetime.max
        earliest_comment_str = earliest_comment_ts.strftime("%Y-%m-%d %H:%M:%S") if comment_datetimes else "N/A"

        users_stats.append({
            "username": username,
            "comment_count": comment_count,
            "liked_posts_count": liked_posts_count,
            "not_liked_posts_count": not_liked_posts_count,
            "checkable_posts_count": checkable_posts_count,
            "unknown_posts_count": unverified_posts_count,
            "unverified_posts_count": unverified_posts_count,
            "not_applicable_posts_count": not_applicable_posts_count,
            "has_liked_post": like_status_display,
            "total_post_likes": total_post_likes,
            "post_likes_known_count": len(known_post_likes),
            "post_likes_unknown_count": unknown_post_likes_count,
            "post_likes_complete": unknown_post_likes_count == 0,
            "total_comment_likes": total_comment_likes,
            "comment_likes_known_count": len(known_comment_likes),
            "comment_likes_unknown_count": unknown_comment_likes_count,
            "comment_likes_complete": unknown_comment_likes_count == 0,
            "earliest_comment_date": earliest_comment_str,
            "earliest_comment_ts": earliest_comment_ts,
            "unique_posts_count": len(unique_post_likes),
            "posts_commented": unique_posts,
            "post_urls": unique_urls if unique_urls else [f"https://www.instagram.com/p/{sc}/" for sc in unique_posts],
        })

    # Urutkan berdasarkan:
    # 1. Banyak komentar (comment_count DESC)
    # 2. Sudah like post (liked_posts_count DESC)
    # 3. Total like pada postingan yang dikomen (total_post_likes DESC)
    # 4. Total like komentar (total_comment_likes DESC)
    # 5. Waktu komentar pertama tercepat (earliest_comment_ts ASC)
    # 6. Abjad username (username.lower() ASC)
    users_stats.sort(
        key=lambda x: (
            -x["comment_count"],
            -x["liked_posts_count"],
            -(x["total_post_likes"] if x["total_post_likes"] is not None else -1),
            -(x["total_comment_likes"] if x["total_comment_likes"] is not None else -1),
            x["earliest_comment_ts"],
            x["username"].lower(),
        )
    )

    # Beri nomor rank
    result = []
    for rank, u in enumerate(users_stats[:top_n], 1):
        output = dict(u)
        output.pop("earliest_comment_ts", None)
        output["rank"] = rank
        result.append(output)

    return result


def get_detailed_comments_by_user(
    comments: list[dict],
    usernames: list[str],
) -> list[dict]:
    """
    Ambil detail komentar dari user-user tertentu.

    Args:
        comments: List of dict komentar.
        usernames: List of username yang ingin diambil detailnya.

    Returns:
        List of dict komentar yang sudah difilter.
    """
    return [c for c in comments if c["commenter_username"] in usernames]


def get_unique_posts_summary(comments: list[dict]) -> list[dict]:
    """
    Ekstrak daftar unik postingan beserta statistik likes dan jumlah komentar.

    Args:
        comments: List of dict komentar.

    Returns:
        List of dict data postingan unik.
    """
    posts_map = {}
    for c in comments:
        url = c.get("post_url") or c.get("post_shortcode")
        if not url:
            continue

        if url not in posts_map:
            posts_map[url] = {
                "post_url": url,
                "post_date": c.get("post_date", "N/A"),
                "post_likes": _known_number(c.get("post_likes")),
                "comments_count": 1,
                "post_caption": c.get("post_caption", ""),
            }
        else:
            posts_map[url]["comments_count"] += 1
            candidate_likes = _known_number(c.get("post_likes"))
            if posts_map[url]["post_likes"] is None and candidate_likes is not None:
                posts_map[url]["post_likes"] = candidate_likes

    # Sort descending berdasarkan jumlah likes, lalu komentar
    result = list(posts_map.values())
    result.sort(
        key=lambda x: (
            x["post_likes"] if x["post_likes"] is not None else -1,
            x["comments_count"],
        ),
        reverse=True,
    )
    return result


def get_summary_stats(
    comments: list[dict],
    posts_count: int,
    posts: list | None = None,
) -> dict:
    """
    Hitung statistik ringkasan termasuk like dan komentar.

    Args:
        comments: List of dict komentar.
        posts_count: Jumlah post yang di-scan.

    Returns:
        Dict berisi statistik ringkasan.
    """
    unique_commenters = len({str(c.get("commenter_username") or "unknown") for c in comments})
    avg_comments = len(comments) / posts_count if posts_count > 0 else 0

    # Gunakan daftar post asli bila tersedia agar post tanpa komentar tetap ikut
    # dihitung. Nilai yang hilang dipertahankan sebagai unknown, bukan nol.
    unique_post_likes = {}
    if posts is not None:
        for index, post in enumerate(posts):
            if isinstance(post, dict):
                key = post.get("post_url") or post.get("post_id") or post.get("id") or index
                raw_likes = post.get("post_likes")
            else:
                key = getattr(post, "code", None) or getattr(post, "pk", None) or index
                raw_likes = getattr(post, "like_count", None)
            unique_post_likes[str(key)] = _known_number(raw_likes)
    else:
        for c in comments:
            key = _post_key(c)
            if not key:
                continue
            like_count = _known_number(c.get("post_likes"))
            if key not in unique_post_likes or (
                unique_post_likes[key] is None and like_count is not None
            ):
                unique_post_likes[key] = like_count

    known_post_likes = [value for value in unique_post_likes.values() if value is not None]
    unknown_post_likes_count = len(unique_post_likes) - len(known_post_likes)
    if posts is None:
        unknown_post_likes_count += max(posts_count - len(unique_post_likes), 0)

    total_likes = sum(known_post_likes) if known_post_likes else None
    avg_likes = (
        total_likes / posts_count
        if total_likes is not None and posts_count > 0
        else None if posts_count > 0 else 0
    )

    return {
        "total_posts_scanned": posts_count,
        "total_post_likes": total_likes,
        "avg_likes_per_post": round(avg_likes, 1) if avg_likes is not None else None,
        "post_likes_known_count": len(known_post_likes),
        "post_likes_unknown_count": unknown_post_likes_count,
        "post_likes_complete": unknown_post_likes_count == 0,
        "total_comments": len(comments),
        "unique_commenters": unique_commenters,
        "avg_comments_per_post": round(avg_comments, 1),
    }
