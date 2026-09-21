import unittest

from core.analyzer import count_top_commenters, get_summary_stats


def make_comment(post, status, **overrides):
    comment = {
        "commenter_username": "alice",
        "post_shortcode": post,
        "post_likes": 10,
        "comment_likes": 0,
        "comment_date": "2026-09-20 10:00:00",
        "has_liked_post": status,
    }
    comment.update(overrides)
    return comment


class CountTopCommentersTests(unittest.TestCase):
    def test_confirmed_no_mixed_with_unknown_does_not_become_no(self):
        comments = [
            make_comment(
                "complete",
                "Tidak",
                liker_lookup_complete=True,
                like_lookup_status="complete",
            ),
            make_comment(
                "hidden",
                "Disembunyikan Instagram",
                liker_lookup_complete=False,
                like_lookup_status="unavailable",
            ),
        ]

        result = count_top_commenters(comments)[0]

        self.assertTrue(result["has_liked_post"].startswith("Belum dapat diverifikasi"))
        self.assertEqual(result["liked_posts_count"], 0)
        self.assertEqual(result["not_liked_posts_count"], 1)
        self.assertEqual(result["checkable_posts_count"], 1)
        self.assertEqual(result["unknown_posts_count"], 1)
        self.assertEqual(result["unverified_posts_count"], 1)

    def test_partial_lookup_cannot_confirm_negative(self):
        result = count_top_commenters([
            make_comment(
                "partial",
                "Tidak",
                liker_lookup_complete=False,
                like_lookup_status="partial",
            )
        ])[0]

        self.assertEqual(result["has_liked_post"], "Belum dapat diverifikasi")
        self.assertEqual(result["checkable_posts_count"], 0)
        self.assertEqual(result["unknown_posts_count"], 1)
        self.assertEqual(result["unverified_posts_count"], 1)

    def test_partial_positive_is_valid_and_uses_checkable_denominator(self):
        comments = [
            make_comment(
                "matched",
                "Ya",
                liker_lookup_complete=False,
                like_lookup_status="partial",
            ),
            make_comment(
                "unknown",
                "Belum dapat diverifikasi",
                liker_lookup_complete=False,
                like_lookup_status="partial",
            ),
        ]

        result = count_top_commenters(comments)[0]

        self.assertEqual(result["liked_posts_count"], 1)
        self.assertEqual(result["checkable_posts_count"], 1)
        self.assertEqual(result["unverified_posts_count"], 1)
        self.assertIn("Ya (1/1 yang dapat dicek", result["has_liked_post"])
        self.assertNotIn("1/2", result["has_liked_post"])

    def test_legacy_negative_without_completeness_is_unknown(self):
        result = count_top_commenters([
            make_comment("legacy", "Tidak")
        ])[0]

        self.assertEqual(result["has_liked_post"], "Belum dapat diverifikasi")
        self.assertEqual(result["not_liked_posts_count"], 0)
        self.assertEqual(result["unverified_posts_count"], 1)

    def test_all_explicit_comment_like_counts_are_aggregated_including_zero(self):
        comments = [
            make_comment("one", "N/A", comment_likes=3),
            make_comment("two", "N/A", comment_likes=0),
        ]

        result = count_top_commenters(comments)[0]

        self.assertEqual(result["total_comment_likes"], 3)
        self.assertEqual(result["comment_likes_known_count"], 2)
        self.assertEqual(result["comment_likes_unknown_count"], 0)
        self.assertTrue(result["comment_likes_complete"])
        self.assertNotIn("earliest_comment_ts", result)

    def test_mixed_none_comment_likes_exposes_partial_subtotal(self):
        comments = [
            make_comment("one", "N/A", comment_likes=None),
            make_comment("two", "N/A", comment_likes=4),
        ]

        result = count_top_commenters(comments)[0]

        self.assertEqual(result["total_comment_likes"], 4)
        self.assertEqual(result["comment_likes_known_count"], 1)
        self.assertEqual(result["comment_likes_unknown_count"], 1)
        self.assertFalse(result["comment_likes_complete"])

    def test_all_unknown_comment_likes_remain_none(self):
        comments = [
            make_comment("one", "N/A", comment_likes=None),
            make_comment("two", "N/A", comment_likes=None),
        ]

        result = count_top_commenters(comments)[0]

        self.assertIsNone(result["total_comment_likes"])
        self.assertEqual(result["comment_likes_known_count"], 0)
        self.assertEqual(result["comment_likes_unknown_count"], 2)
        self.assertFalse(result["comment_likes_complete"])

    def test_missing_post_like_count_is_not_coerced_to_zero(self):
        comments = [
            make_comment("one", "N/A", post_likes=None),
            make_comment("two", "N/A", post_likes=4),
        ]

        result = count_top_commenters(comments)[0]

        self.assertEqual(result["total_post_likes"], 4)
        self.assertEqual(result["post_likes_known_count"], 1)
        self.assertEqual(result["post_likes_unknown_count"], 1)
        self.assertFalse(result["post_likes_complete"])

    def test_summary_uses_all_posts_and_marks_partial_like_totals(self):
        posts = [
            {"post_url": "one", "post_likes": 5},
            {"post_url": "two", "post_likes": None},
        ]

        result = get_summary_stats([], len(posts), posts=posts)

        self.assertEqual(result["total_post_likes"], 5)
        self.assertEqual(result["avg_likes_per_post"], 2.5)
        self.assertEqual(result["post_likes_known_count"], 1)
        self.assertEqual(result["post_likes_unknown_count"], 1)
        self.assertFalse(result["post_likes_complete"])


if __name__ == "__main__":
    unittest.main()
