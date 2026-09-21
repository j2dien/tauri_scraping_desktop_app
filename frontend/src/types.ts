/**
 * types.ts — Definisi tipe data TypeScript untuk Scraping Desktop App.
 */

export type Platform = 'tiktok' | 'instagram';

export type LikeVerificationStatus =
  | 'Ya'
  | `Ya (${number}/${number})`
  | `Ya (${number}/${number} yang dapat dicek; ${number} belum dapat diverifikasi)`
  | 'Tidak'
  | 'Belum dapat diverifikasi'
  | `Belum dapat diverifikasi (0/${number} yang dapat dicek; ${number} belum dapat diverifikasi)`
  | 'Tidak dapat dicek'
  | 'Disembunyikan Instagram'
  | 'N/A';

export interface TopCommenter {
  rank: number;
  username: string;
  comment_count: number;
  earliest_comment_date?: string;
  has_liked_post?: LikeVerificationStatus | null;
  liked_posts_count?: number;
  not_liked_posts_count?: number;
  checkable_posts_count?: number;
  unknown_posts_count?: number;
  unverified_posts_count?: number;
  not_applicable_posts_count?: number;
  total_comment_likes: number | null;
  comment_likes_known_count?: number;
  comment_likes_unknown_count?: number;
  comment_likes_complete?: boolean;
  total_post_likes?: number | null;
  post_likes_known_count?: number;
  post_likes_unknown_count?: number;
  post_likes_complete?: boolean;
  unique_posts_count: number;
  post_urls: string[];
}

export interface UserCommentDetail {
  comment_id?: string;
  commenter_username?: string;
  username?: string;
  comment_text: string;
  comment_date: string;
  comment_likes: number | null;
  post_url: string;
  post_likes: number | null;
  post_caption?: string;
  has_liked_post?: LikeVerificationStatus | null;
  like_lookup_status?: string;
  like_lookup_source?: string;
  like_lookup_reason?: string;
  liker_count_observed?: number;
  liker_lookup_complete?: boolean;
  liker_usernames_complete?: boolean;
  liker_user_ids_complete?: boolean;
  comment_lookup_status?: string;
  comment_lookup_reason?: string;
  post_comment_count_expected?: number | null;
  post_comment_count_observed?: number;
}

export interface SummaryStats {
  total_posts_scanned: number;
  total_comments: number;
  unique_commenters: number;
  avg_comments_per_post: number;
  total_post_likes?: number | null;
  avg_likes_per_post?: number | null;
  post_likes_known_count?: number;
  post_likes_unknown_count?: number;
  post_likes_complete?: boolean;
}

export interface ScrapedPost {
  post_id?: string;
  post_url: string;
  post_date: string;
  post_type?: string;
  post_likes: number | null;
  post_caption?: string;
  comment_count?: number;
}

export interface AnalysisResultPayload {
  top_commenters: TopCommenter[];
  summary: SummaryStats;
  detail_comments: UserCommentDetail[] | Record<string, UserCommentDetail[]>;
  all_comments: Record<string, any>[];
  scraped_posts: Record<string, any>[];
  total_posts: number;
  total_comments: number;
  diagnostics?: Record<string, any>;
}

export interface WebSocketMessage {
  type: 'started' | 'status' | 'log' | 'post_found' | 'comment_progress' | 'completed' | 'error' | 'cancelled';
  message: string;
  payload?: any;
  timestamp?: string;
}

export interface LogEntry {
  time: string;
  text: string;
  type: 'info' | 'log' | 'success' | 'completed' | 'error' | 'cancelled';
}

export interface IndonesianDatePickerProps {
  label?: string;
  value: string; // "DD-MM-YYYY"
  onChange: (dateStr: string) => void;
  align?: 'left' | 'right';
}
