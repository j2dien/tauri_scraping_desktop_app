"""
analyzer.py — Modul untuk menganalisis dan mengagregasi data komentar.

Menghitung top commenters berdasarkan frekuensi komentar
dan menyiapkan data ringkasan.
"""

from collections import Counter


from datetime import datetime


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
        username = c.get("commenter_username", "unknown")
        if username not in user_groups:
            user_groups[username] = []
        user_groups[username].append(c)

    users_stats = []
    for username, user_comments in user_groups.items():
        comment_count = len(user_comments)

        # Hitung unique posts dan total likes dari post-post yang dikomentari
        unique_post_likes = {}
        for c in user_comments:
            p_key = c.get("post_url") or c.get("post_shortcode")
            if p_key and p_key not in unique_post_likes:
                unique_post_likes[p_key] = c.get("post_likes", 0)

        total_post_likes = sum(unique_post_likes.values())
        total_comment_likes = sum(c.get("comment_likes", 0) for c in user_comments)
        unique_urls = list({c["post_url"] for c in user_comments if c.get("post_url")})
        unique_posts = list({c["post_shortcode"] for c in user_comments if c.get("post_shortcode")})

        # Hitung status apakah user melakukan like pada post yang dikomentari
        liked_posts_count = sum(1 for c in user_comments if c.get("has_liked_post") == "Ya")
        is_tiktok_na = all("N/A" in str(c.get("has_liked_post", "")) for c in user_comments)

        if is_tiktok_na:
            like_status_display = "N/A"
        elif liked_posts_count > 0:
            like_status_display = f"Ya ({liked_posts_count}/{len(unique_post_likes)})"
        else:
            like_status_display = "Tidak"

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
            "has_liked_post": like_status_display,
            "total_post_likes": total_post_likes,
            "total_comment_likes": total_comment_likes,
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
            -x["total_post_likes"],
            -x["total_comment_likes"],
            x["earliest_comment_ts"],
            x["username"].lower(),
        )
    )

    # Beri nomor rank
    result = []
    for rank, u in enumerate(users_stats[:top_n], 1):
        u["rank"] = rank
        result.append(u)

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
                "post_likes": c.get("post_likes", 0),
                "comments_count": 1,
                "post_caption": c.get("post_caption", ""),
            }
        else:
            posts_map[url]["comments_count"] += 1
            if posts_map[url]["post_likes"] == 0 and c.get("post_likes", 0) > 0:
                posts_map[url]["post_likes"] = c.get("post_likes", 0)

    # Sort descending berdasarkan jumlah likes, lalu komentar
    result = list(posts_map.values())
    result.sort(key=lambda x: (x["post_likes"], x["comments_count"]), reverse=True)
    return result


def get_summary_stats(
    comments: list[dict],
    posts_count: int,
) -> dict:
    """
    Hitung statistik ringkasan termasuk like dan komentar.

    Args:
        comments: List of dict komentar.
        posts_count: Jumlah post yang di-scan.

    Returns:
        Dict berisi statistik ringkasan.
    """
    if not comments:
        return {
            "total_posts_scanned": posts_count,
            "total_post_likes": 0,
            "avg_likes_per_post": 0,
            "total_comments": 0,
            "unique_commenters": 0,
            "avg_comments_per_post": 0,
        }

    unique_commenters = len({c["commenter_username"] for c in comments})
    avg_comments = len(comments) / posts_count if posts_count > 0 else 0

    # Hitung total likes dari post unik
    unique_post_likes = {}
    for c in comments:
        sc = c.get("post_shortcode") or c.get("post_url")
        if sc and sc not in unique_post_likes:
            unique_post_likes[sc] = c.get("post_likes", 0)

    total_likes = sum(unique_post_likes.values())
    avg_likes = total_likes / posts_count if posts_count > 0 else 0

    return {
        "total_posts_scanned": posts_count,
        "total_post_likes": total_likes,
        "avg_likes_per_post": round(avg_likes, 1),
        "total_comments": len(comments),
        "unique_commenters": unique_commenters,
        "avg_comments_per_post": round(avg_comments, 1),
    }
