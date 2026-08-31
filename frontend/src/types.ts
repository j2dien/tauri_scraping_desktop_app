/**
 * types.ts — Definisi tipe data TypeScript untuk Scraping Desktop App.
 */

export type Platform = 'tiktok' | 'instagram';

export interface TopCommenter {
  rank: number;
  username: string;
  comment_count: number;
  earliest_comment_date?: string;
  has_liked_post?: 'Ya' | 'Tidak' | 'N/A' | string;
  total_comment_likes: number;
  total_post_likes?: number;
  unique_posts_count: number;
  post_urls: string[];
}

export interface UserCommentDetail {
  comment_id?: string;
  commenter_username?: string;
  username?: string;
  comment_text: string;
  comment_date: string;
  comment_likes: number;
  post_url: string;
  post_likes: number;
  post_caption?: string;
  has_liked_post?: string;
}

export interface SummaryStats {
  total_posts_scanned: number;
  total_comments: number;
  unique_commenters: number;
  avg_comments_per_post: number;
  total_post_likes?: number;
  avg_likes_per_post?: number;
}

export interface ScrapedPost {
  post_id: string;
  post_url: string;
  post_date: string;
  post_type: string;
  post_likes: number;
  post_caption?: string;
  comment_count?: number;
}

export interface AnalysisResultPayload {
  top_commenters: TopCommenter[];
  summary: SummaryStats;
  detail_comments: UserCommentDetail[] | Record<string, UserCommentDetail[]>;
  total_posts: number;
  total_comments: number;
}

export interface WebSocketMessage {
  type: 'started' | 'status' | 'log' | 'post_found' | 'comment_progress' | 'completed' | 'error';
  message: string;
  payload?: any;
  timestamp?: string;
}

export interface LogEntry {
  time: string;
  text: string;
  type: 'info' | 'log' | 'success' | 'completed' | 'error';
}

export interface IndonesianDatePickerProps {
  label?: string;
  value: string; // "DD-MM-YYYY"
  onChange: (dateStr: string) => void;
  align?: 'left' | 'right';
}
