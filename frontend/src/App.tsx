import React, { useState, useEffect, useRef } from 'react';
import IndonesianDatePicker from './IndonesianDatePicker';
import type {
  Platform,
  TopCommenter,
  AnalysisResultPayload,
  WebSocketMessage,
  LogEntry,
} from './types';

const isServedDirectlyByFastAPI = typeof window !== 'undefined' && window.location.port === '8008';

const API_BASE = isServedDirectlyByFastAPI
  ? window.location.origin
  : 'http://127.0.0.1:8008';

const WS_URL = isServedDirectlyByFastAPI
  ? `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/logs`
  : 'ws://127.0.0.1:8008/ws/logs';


// Helper parser JSON aman
const safeJson = async (res: Response): Promise<Record<string, any>> => {
  try {
    const text = await res.text();
    if (!text || text.trim() === '') return {};
    return JSON.parse(text);
  } catch {
    return {};
  }
};

export default function App(): React.JSX.Element {
  const [platform, setPlatform] = useState<Platform>('tiktok');
  const [target, setTarget] = useState<string>('pusatlsskincare');

  // Date Helpers
  const MONTH_NAMES_ID = [
    'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
    'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember'
  ];

  const formatDateStr = (d: Date): string => {
    const day = String(d.getDate()).padStart(2, '0');
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const year = d.getFullYear();
    return `${day}-${month}-${year}`;
  };

  const getPastDate = (daysAgo: number): string => {
    const d = new Date();
    d.setDate(d.getDate() - daysAgo);
    return formatDateStr(d);
  };

  const formatIndoDate = (dmyStr: string): string => {
    if (!dmyStr) return '';
    const parts = dmyStr.split('-');
    if (parts.length !== 3) return dmyStr;
    const day = parseInt(parts[0], 10);
    const monthIdx = parseInt(parts[1], 10) - 1;
    const year = parts[2];
    const monthName = MONTH_NAMES_ID[monthIdx] || parts[1];
    return `${day} ${monthName} ${year}`;
  };

  // Helper untuk memformat tanggal ke format Indonesia lengkap: e.g. "20 Agustus 2026 09:08:49"
  const formatIndoDateTime = (dateStr?: string): string => {
    if (!dateStr || dateStr === 'N/A') return 'N/A';

    // Jika format "YYYY-MM-DD HH:MM:SS" atau "YYYY-MM-DD HH:MM"
    if (dateStr.includes(' ') && dateStr.includes('-')) {
      const [datePart, timePart] = dateStr.split(' ');
      const dParts = datePart.split('-');
      if (dParts.length === 3) {
        // YYYY-MM-DD
        if (dParts[0].length === 4) {
          const year = dParts[0];
          const monthIdx = parseInt(dParts[1], 10) - 1;
          const day = parseInt(dParts[2], 10);
          const monthName = MONTH_NAMES_ID[monthIdx] || dParts[1];
          return `${day} ${monthName} ${year} ${timePart || ''}`.trim();
        }
        // DD-MM-YYYY
        else if (dParts[2].length === 4) {
          const day = parseInt(dParts[0], 10);
          const monthIdx = parseInt(dParts[1], 10) - 1;
          const year = dParts[2];
          const monthName = MONTH_NAMES_ID[monthIdx] || dParts[1];
          return `${day} ${monthName} ${year} ${timePart || ''}`.trim();
        }
      }
    }

    // Jika hanya "YYYY-MM-DD"
    if (dateStr.includes('-') && dateStr.split('-').length === 3) {
      const dParts = dateStr.split('-');
      if (dParts[0].length === 4) {
        const year = dParts[0];
        const monthIdx = parseInt(dParts[1], 10) - 1;
        const day = parseInt(dParts[2], 10);
        const monthName = MONTH_NAMES_ID[monthIdx] || dParts[1];
        return `${day} ${monthName} ${year}`;
      } else if (dParts[2].length === 4) {
        const day = parseInt(dParts[0], 10);
        const monthIdx = parseInt(dParts[1], 10) - 1;
        const year = dParts[2];
        const monthName = MONTH_NAMES_ID[monthIdx] || dParts[1];
        return `${day} ${monthName} ${year}`;
      }
    }

    return dateStr;
  };

  const [startDate, setStartDate] = useState<string>('24-08-2026');
  const [endDate, setEndDate] = useState<string>('30-08-2026');
  const [topN, setTopN] = useState<number>(10);

  // Instagram credentials
  const [igUser, setIgUser] = useState<string>('');
  const [igPass, setIgPass] = useState<string>('');

  // Status & Live Logs
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [statusMessage, setStatusMessage] = useState<string>('Siap untuk memulai analisis');
  const [progressLog, setProgressLog] = useState<LogEntry[]>([]);
  const [progressPercent, setProgressPercent] = useState<number>(0);

  // Results Data
  const [analysisResult, setAnalysisResult] = useState<AnalysisResultPayload | null>(null);
  const [selectedUserDetail, setSelectedUserDetail] = useState<TopCommenter | null>(null);
  const [searchFilter, setSearchFilter] = useState<string>('');
  const [exportMessage, setExportMessage] = useState<string>('');
  const [isExporting, setIsExporting] = useState<boolean>(false);

  const logEndRef = useRef<HTMLDivElement>(null);

  // Connect WebSocket for live events
  useEffect(() => {
    let ws: WebSocket | null = null;
    let isMounted = true;

    const connectWS = () => {
      try {
        ws = new WebSocket(WS_URL);
        ws.onopen = () => {
          console.log('Connected to backend WebSocket');
        };
        ws.onmessage = (event: MessageEvent) => {
          if (!isMounted) return;
          try {
            const data: WebSocketMessage = JSON.parse(event.data);
            handleWebSocketMessage(data);
          } catch (e) {
            console.error('Error parsing WS message:', e);
          }
        };
        ws.onclose = () => {
          if (isMounted) {
            setTimeout(connectWS, 2000);
          }
        };
      } catch (err) {
        console.error('WebSocket connection error:', err);
      }
    };
    connectWS();

    return () => {
      isMounted = false;
      if (ws) ws.close();
    };
  }, []);

  // Polling Fallback: Selalu sinkronkan state dari backend setiap 800ms saat proses scraping berjalan
  useEffect(() => {
    if (!isLoading) return;

    const pollInterval = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/progress`);
        if (!res.ok) return;
        const state = await safeJson(res);

        if (state.is_running) {
          setIsLoading(true);
        }
        if (state.status) {
          setStatusMessage(state.status);
        }
        if (state.progress_percent !== undefined) {
          setProgressPercent(state.progress_percent);
        }
        if (Array.isArray(state.logs) && state.logs.length > 0) {
          setProgressLog(state.logs);
        }
        // Hanya update hasil jika backend sudah selesai (is_running: false)
        if (!state.is_running && state.result) {
          setAnalysisResult(state.result);
          setIsLoading(false);
          setProgressPercent(100);
        }
        if (!state.is_running && state.error) {
          setIsLoading(false);
          setStatusMessage(`Error: ${state.error}`);
        }
      } catch (e) {
        console.warn('Progress poll error:', e);
      }
    }, 800);

    return () => clearInterval(pollInterval);
  }, [isLoading]);

  const handleWebSocketMessage = (data: WebSocketMessage) => {
    const { type, message, payload, timestamp } = data;
    const timeStr = timestamp ? new Date(timestamp).toLocaleTimeString() : '';

    if (type === 'started') {
      setIsLoading(true);
      setAnalysisResult(null);
      setSelectedUserDetail(null);
      setExportMessage('');
      setProgressPercent(0);
      setStatusMessage(message);
      setProgressLog([{ time: timeStr, text: message, type: 'info' }]);
    } else if (type === 'status') {
      setStatusMessage(message);
      setProgressLog((prev) => [...prev, { time: timeStr, text: message, type: 'info' }]);
    } else if (type === 'log') {
      setStatusMessage(message);
      setProgressLog((prev) => [...prev, { time: timeStr, text: message, type: 'log' }]);
    } else if (type === 'post_found') {
      setStatusMessage(message);
      setProgressLog((prev) => [...prev, { time: timeStr, text: message, type: 'success' }]);
    } else if (type === 'comment_progress') {
      if (payload && payload.total > 0) {
        const pct = Math.round((payload.current / payload.total) * 100);
        setProgressPercent(pct);
      }
      setStatusMessage(message);
      setProgressLog((prev) => [...prev, { time: timeStr, text: message, type: 'info' }]);
    } else if (type === 'completed') {
      setIsLoading(false);
      setStatusMessage(message);
      setAnalysisResult(payload as AnalysisResultPayload);
      setProgressPercent(100);
      setProgressLog((prev) => [...prev, { time: timeStr, text: `✓ ${message}`, type: 'completed' }]);
    } else if (type === 'error') {
      setIsLoading(false);
      setStatusMessage(`Error: ${message}`);
      setProgressLog((prev) => [...prev, { time: timeStr, text: `✗ ${message}`, type: 'error' }]);
    }
  };

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [progressLog]);

  // Quick Preset Handlers
  const applyPreset = (type: '7days' | '14days' | '30days' | 'thismonth') => {
    const now = new Date();
    if (type === '7days') {
      setStartDate(getPastDate(7));
      setEndDate(formatDateStr(now));
    } else if (type === '14days') {
      setStartDate(getPastDate(14));
      setEndDate(formatDateStr(now));
    } else if (type === '30days') {
      setStartDate(getPastDate(30));
      setEndDate(formatDateStr(now));
    } else if (type === 'thismonth') {
      const firstDay = new Date(now.getFullYear(), now.getMonth(), 1);
      setStartDate(formatDateStr(firstDay));
      setEndDate(formatDateStr(now));
    }
  };

  // Start Analysis
  const handleStartAnalysis = async () => {
    if (!target.trim()) {
      alert('Target akun tidak boleh kosong!');
      return;
    }
    if (platform === 'instagram' && (!igUser || !igPass)) {
      alert('Instagram mewajibkan username dan password login!');
      return;
    }

    setIsLoading(true);
    setProgressLog([]);
    setProgressPercent(0);
    setAnalysisResult(null);
    setExportMessage('');
    setStatusMessage('Menghubungkan ke scraping engine...');

    try {
      const res = await fetch(`${API_BASE}/api/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          platform,
          target: target.replace('@', '').trim(),
          start_date: startDate,
          end_date: endDate,
          top_n: Number(topN) || 10,
          ig_username: igUser || null,
          ig_password: igPass || null,
        }),
      });

      if (!res.ok) {
        const err = await safeJson(res);
        throw new Error(err.detail || err.message || `Server error (${res.status})`);
      }
    } catch (e: any) {
      setIsLoading(false);
      setStatusMessage(`Gagal: ${e.message}`);
      alert(e.message);
    }
  };

  // Export to Excel
  const handleExportExcel = async () => {
    if (!analysisResult) return;
    try {
      setIsExporting(true);
      setExportMessage('⏳ Mengekspor ke Excel...');
      const res = await fetch(`${API_BASE}/api/export`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          top_commenters: analysisResult.top_commenters || [],
          detail_comments: analysisResult.detail_comments || [],
          summary_stats: analysisResult.summary || {},
          target_username: target.replace('@', '').trim(),
          start_date: startDate,
          end_date: endDate,
          platform: platform.toUpperCase(),
        }),
      });

      const data = await safeJson(res);
      if (res.ok) {
        setExportMessage(`✓ Berhasil disimpan: ${data.filename || 'export.xlsx'}`);
      } else {
        const errorDetail = typeof data.detail === 'string'
          ? data.detail
          : Array.isArray(data.detail)
            ? data.detail.map((d: any) => d.msg || JSON.stringify(d)).join(', ')
            : data.message || 'Gagal export file';
        setExportMessage(`✗ Gagal: ${errorDetail}`);
        alert(`Gagal export file: ${errorDetail}`);
      }
    } catch (e: any) {
      setExportMessage(`✗ Error: ${e.message}`);
      alert(`Error export: ${e.message}`);
    } finally {
      setIsExporting(false);
    }
  };

  // Open External URL in default system browser
  const handleOpenExternalUrl = async (url?: string) => {
    if (!url) return;
    try {
      await fetch(`${API_BASE}/api/open-url`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
    } catch {
      window.open(url, '_blank', 'noopener,noreferrer');
    }
  };

  // Open Export Folder
  const handleOpenFolder = async () => {
    try {
      await fetch(`${API_BASE}/api/open-folder`, { method: 'POST' });
    } catch (e) {
      console.error(e);
    }
  };

  // Filtered commenters
  const filteredCommenters = (analysisResult?.top_commenters || []).filter((c) =>
    c.username.toLowerCase().includes(searchFilter.toLowerCase())
  );

  return (
    <div style={{ padding: '24px 32px', maxWidth: '1400px', margin: '0 auto' }}>
      {/* Top Navbar */}
      <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '28px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
          <div style={{
            width: '44px',
            height: '44px',
            borderRadius: '12px',
            background: 'var(--gradient-primary)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '22px',
            boxShadow: '0 4px 16px rgba(6, 182, 212, 0.4)'
          }}>
            ⚡
          </div>
          <div>
            <h1 style={{ fontSize: '20px', fontWeight: '800', background: 'var(--gradient-primary)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
              Social Media Top Commenter Analyzer
            </h1>
            <p style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
              Desktop Engine — Scrape & Analisis Engagement TikTok & Instagram (TypeScript + Bun)
            </p>
          </div>
        </div>
      </header>

      {/* Main Grid Layout */}
      <div style={{ display: 'grid', gridTemplateColumns: '380px 1fr', gap: '24px', alignItems: 'start' }}>
        
        {/* Left Column: Form Controls */}
        <div className="glass-panel" style={{ padding: '24px', position: 'relative', zIndex: 30 }}>
          <h2 style={{ fontSize: '16px', fontWeight: '700', marginBottom: '18px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            ⚙️ Parameter Scraping
          </h2>

          {/* Platform Selector */}
          <div style={{ marginBottom: '20px' }}>
            <label style={{ display: 'block', fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '8px', fontWeight: '500' }}>
              Platform Media Sosial
            </label>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
              <button
                type="button"
                onClick={() => setPlatform('tiktok')}
                style={{
                  padding: '12px',
                  borderRadius: '12px',
                  border: platform === 'tiktok' ? '2px solid var(--accent-cyan)' : '1px solid var(--border-color)',
                  background: platform === 'tiktok' ? 'rgba(6, 182, 212, 0.15)' : 'rgba(0,0,0,0.2)',
                  color: 'var(--text-primary)',
                  fontWeight: '600',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: '8px'
                }}
              >
                🎵 TikTok
              </button>
              <button
                type="button"
                onClick={() => setPlatform('instagram')}
                style={{
                  padding: '12px',
                  borderRadius: '12px',
                  border: platform === 'instagram' ? '2px solid var(--accent-pink)' : '1px solid var(--border-color)',
                  background: platform === 'instagram' ? 'rgba(236, 72, 153, 0.15)' : 'rgba(0,0,0,0.2)',
                  color: 'var(--text-primary)',
                  fontWeight: '600',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: '8px'
                }}
              >
                📸 Instagram
              </button>
            </div>
          </div>

          {/* Target Account Input */}
          <div style={{ marginBottom: '20px' }}>
            <label style={{ display: 'block', fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '8px', fontWeight: '500' }}>
              Target {platform === 'tiktok' ? 'Username / Link Video TikTok' : 'Username Instagram'}
            </label>
            <input
              type="text"
              className="custom-input"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              placeholder={platform === 'tiktok' ? 'misal: pusatlsskincare' : 'misal: akun_ig'}
            />
            <span style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px', display: 'block' }}>
              {platform === 'tiktok' ? 'Bisa berupa username, atau link URL video/foto langsung.' : 'Username akun Instagram yang akan di-scan.'}
            </span>
          </div>

          {/* Date Range Inputs */}
          <div style={{ marginBottom: '20px', position: 'relative', zIndex: 40 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
              <label style={{ fontSize: '13px', color: 'var(--text-secondary)', fontWeight: '500' }}>
                📅 Rentang Tanggal
              </label>
              <span style={{ fontSize: '11px', color: 'var(--accent-cyan)', fontWeight: '600' }}>
                {formatIndoDate(startDate)} s/d {formatIndoDate(endDate)}
              </span>
            </div>

            {/* Quick Presets */}
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginBottom: '12px' }}>
              <button type="button" onClick={() => applyPreset('7days')} className="btn-secondary" style={{ padding: '4px 10px', fontSize: '11px' }}>7 Hari Terakhir</button>
              <button type="button" onClick={() => applyPreset('14days')} className="btn-secondary" style={{ padding: '4px 10px', fontSize: '11px' }}>14 Hari Terakhir</button>
              <button type="button" onClick={() => applyPreset('30days')} className="btn-secondary" style={{ padding: '4px 10px', fontSize: '11px' }}>30 Hari Terakhir</button>
              <button type="button" onClick={() => applyPreset('thismonth')} className="btn-secondary" style={{ padding: '4px 10px', fontSize: '11px' }}>Bulan Ini</button>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', position: 'relative', zIndex: 40 }}>
              <IndonesianDatePicker
                label="Mulai Dari Tanggal:"
                value={startDate}
                onChange={(val: string) => setStartDate(val)}
                align="left"
              />
              <IndonesianDatePicker
                label="Sampai Tanggal:"
                value={endDate}
                onChange={(val: string) => setEndDate(val)}
                align="right"
              />
            </div>
          </div>

          {/* Top N Slider */}
          <div style={{ marginBottom: '24px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '6px' }}>
              <label style={{ fontSize: '13px', color: 'var(--text-secondary)', fontWeight: '500' }}>
                Jumlah Top Commenters
              </label>
              <span style={{ fontSize: '13px', color: 'var(--accent-cyan)', fontWeight: '700' }}>
                Top {topN}
              </span>
            </div>
            <input
              type="range"
              min="5"
              max="50"
              step="5"
              value={topN}
              onChange={(e) => setTopN(Number(e.target.value))}
              style={{ width: '100%', accentColor: 'var(--accent-cyan)', cursor: 'pointer' }}
            />
          </div>

          {/* Instagram Login Credentials (if IG selected) */}
          {platform === 'instagram' && (
            <div style={{ padding: '14px', borderRadius: '12px', background: 'rgba(236, 72, 153, 0.08)', border: '1px solid rgba(236, 72, 153, 0.2)', marginBottom: '20px' }}>
              <span style={{ fontSize: '12px', color: 'var(--accent-pink)', fontWeight: '600', display: 'block', marginBottom: '8px' }}>
                🔒 Login Instagram (Wajib Akun Sekunder)
              </span>
              <div style={{ display: 'grid', gap: '8px' }}>
                <input
                  type="text"
                  className="custom-input"
                  placeholder="Username Instagram Anda"
                  value={igUser}
                  onChange={(e) => setIgUser(e.target.value)}
                />
                <input
                  type="password"
                  className="custom-input"
                  placeholder="Password Instagram Anda"
                  value={igPass}
                  onChange={(e) => setIgPass(e.target.value)}
                />
              </div>
            </div>
          )}

          {/* Action Button */}
          <button
            className="btn-gradient"
            onClick={handleStartAnalysis}
            disabled={isLoading}
            style={{ width: '100%', padding: '14px', fontSize: '15px' }}
          >
            {isLoading ? (
              <>
                <span className="spinner">⏳</span> Sedang Menganalisis...
              </>
            ) : (
              '🚀 Mulai Scraping & Analisis'
            )}
          </button>
        </div>

        {/* Right Column: Live Monitor, Results, and Tables */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '24px', position: 'relative', zIndex: 1 }}>
          
          {/* Live Progress Card */}
          <div className="glass-panel" style={{ padding: '20px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <div style={{
                  width: '10px',
                  height: '10px',
                  borderRadius: '50%',
                  background: isLoading ? 'var(--accent-cyan)' : 'var(--accent-green)',
                  boxShadow: isLoading ? '0 0 10px var(--accent-cyan)' : '0 0 10px var(--accent-green)'
                }} className={isLoading ? 'glow-animation' : ''} />
                <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
                  {statusMessage}
                </span>
              </div>
              {isLoading && (
                <span style={{ fontSize: '12px', color: 'var(--accent-cyan)', fontWeight: '700' }}>
                  {progressPercent}%
                </span>
              )}
            </div>

            {/* Progress Bar */}
            {isLoading && (
              <div style={{ width: '100%', height: '6px', background: 'rgba(255,255,255,0.08)', borderRadius: '6px', overflow: 'hidden', marginBottom: '14px' }}>
                <div style={{
                  width: `${progressPercent}%`,
                  height: '100%',
                  background: 'var(--gradient-primary)',
                  transition: 'width 0.3s ease'
                }} />
              </div>
            )}

            {/* Log Terminal Window */}
            <div style={{
              background: '#06090e',
              border: '1px solid rgba(255,255,255,0.05)',
              borderRadius: '10px',
              padding: '12px 16px',
              maxHeight: '140px',
              overflowY: 'auto',
              fontSize: '12px',
              fontFamily: 'monospace',
              display: 'flex',
              flexDirection: 'column',
              gap: '4px'
            }}>
              {progressLog.length === 0 ? (
                <span style={{ color: 'var(--text-muted)' }}>Menunggu perintah scraping... Log aktivitas akan muncul di sini secara langsung.</span>
              ) : (
                progressLog.map((l, idx) => (
                  <div key={idx} style={{
                    color: l.type === 'error' ? 'var(--accent-red)' : l.type === 'success' ? 'var(--accent-green)' : l.type === 'completed' ? 'var(--accent-cyan)' : 'var(--text-secondary)'
                  }}>
                    <span style={{ color: 'var(--text-muted)', marginRight: '8px' }}>[{l.time}]</span>
                    {l.text}
                  </div>
                ))
              )}
              <div ref={logEndRef} />
            </div>
          </div>

          {/* Results View: Top Commenters Analysis */}
          {analysisResult && (
            <div className="glass-panel" style={{ padding: '24px' }}>
              
              {/* Summary Stats Cards */}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '14px', marginBottom: '24px' }}>
                <div className="glass-panel" style={{ padding: '14px 18px', background: 'rgba(6, 182, 212, 0.08)', border: '1px solid rgba(6, 182, 212, 0.2)' }}>
                  <span style={{ fontSize: '11px', color: 'var(--accent-cyan)', fontWeight: '600' }}>TOTAL POSTINGAN</span>
                  <p style={{ fontSize: '24px', fontWeight: '800', marginTop: '4px' }}>{analysisResult.total_posts}</p>
                </div>
                <div className="glass-panel" style={{ padding: '14px 18px', background: 'rgba(59, 130, 246, 0.08)', border: '1px solid rgba(59, 130, 246, 0.2)' }}>
                  <span style={{ fontSize: '11px', color: 'var(--accent-blue)', fontWeight: '600' }}>TOTAL KOMENTAR</span>
                  <p style={{ fontSize: '24px', fontWeight: '800', marginTop: '4px' }}>{analysisResult.total_comments}</p>
                </div>
                <div className="glass-panel" style={{ padding: '14px 18px', background: 'rgba(139, 92, 246, 0.08)', border: '1px solid rgba(139, 92, 246, 0.2)' }}>
                  <span style={{ fontSize: '11px', color: 'var(--accent-purple)', fontWeight: '600' }}>TOTAL LIKES POST</span>
                  <p style={{ fontSize: '24px', fontWeight: '800', marginTop: '4px' }}>{(analysisResult.summary.total_post_likes || 0).toLocaleString()}</p>
                </div>
                <div className="glass-panel" style={{ padding: '14px 18px', background: 'rgba(16, 185, 129, 0.08)', border: '1px solid rgba(16, 185, 129, 0.2)' }}>
                  <span style={{ fontSize: '11px', color: 'var(--accent-green)', fontWeight: '600' }}>UNIQUE COMMENTERS</span>
                  <p style={{ fontSize: '24px', fontWeight: '800', marginTop: '4px' }}>{analysisResult.summary.unique_commenters}</p>
                </div>
              </div>

              {/* Table Action Bar */}
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '18px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <h3 style={{ fontSize: '16px', fontWeight: '700' }}>🏆 Top {topN} Commenters</h3>
                  <input
                    type="text"
                    placeholder="Cari username..."
                    value={searchFilter}
                    onChange={(e) => setSearchFilter(e.target.value)}
                    className="custom-input"
                    style={{ width: '200px', padding: '6px 12px', fontSize: '12px' }}
                  />
                </div>
                
                {/* Export Buttons */}
                <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
                  {exportMessage && (
                    <span style={{ fontSize: '12px', color: exportMessage.startsWith('✓') ? 'var(--accent-green)' : exportMessage.startsWith('⏳') ? 'var(--accent-cyan)' : '#f87171', fontWeight: '600' }}>
                      {exportMessage}
                    </span>
                  )}
                  <button
                    disabled={isExporting}
                    onClick={handleExportExcel}
                    className="btn-gradient"
                    style={{ padding: '8px 16px', fontSize: '13px' }}
                  >
                    {isExporting ? '⏳ Mengekspor...' : '📥 Export ke Excel (.xlsx)'}
                  </button>
                  <button onClick={handleOpenFolder} className="btn-secondary" style={{ padding: '8px 14px', fontSize: '13px' }}>
                    📁 Buka Folder
                  </button>
                </div>
              </div>

              {/* Data Table */}
              <div style={{ overflowX: 'auto', borderRadius: '12px', border: '1px solid var(--border-color)' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '13px' }}>
                  <thead>
                    <tr style={{ background: 'rgba(255,255,255,0.03)', borderBottom: '1px solid var(--border-color)', color: 'var(--text-secondary)' }}>
                      <th style={{ padding: '12px 16px', width: '70px', textAlign: 'center' }}>Rank</th>
                      <th style={{ padding: '12px 16px' }}>Username</th>
                      <th style={{ padding: '12px 16px', textAlign: 'center' }}>Jumlah Komen</th>
                      <th style={{ padding: '12px 16px' }}>Komentar Pertama</th>
                      <th style={{ padding: '12px 16px', textAlign: 'center' }}>Sudah Like?</th>
                      <th style={{ padding: '12px 16px', textAlign: 'center' }}>Like Komen</th>
                      <th style={{ padding: '12px 16px', textAlign: 'center' }}>Aksi</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredCommenters.map((c, index) => (
                      <tr key={index} style={{ borderBottom: '1px solid rgba(255,255,255,0.04)', transition: 'background 0.2s' }} className="glass-panel-hover">
                        <td style={{ padding: '12px 16px', textAlign: 'center', fontWeight: '800' }}>
                          {c.rank === 1 ? '🥇 #1' : c.rank === 2 ? '🥈 #2' : c.rank === 3 ? '🥉 #3' : `#${c.rank}`}
                        </td>
                        <td style={{ padding: '12px 16px', fontWeight: '600', color: 'var(--accent-cyan)' }}>
                          @{c.username}
                        </td>
                        <td style={{ padding: '12px 16px', textAlign: 'center' }}>
                          <span style={{ background: 'rgba(6, 182, 212, 0.15)', color: 'var(--accent-cyan)', padding: '4px 10px', borderRadius: '8px', fontWeight: '700' }}>
                            {c.comment_count}
                          </span>
                        </td>
                        <td style={{ padding: '12px 16px', color: 'var(--text-secondary)', fontSize: '12px' }}>
                          {formatIndoDateTime(c.earliest_comment_date)}
                        </td>
                        <td style={{ padding: '12px 16px', textAlign: 'center' }}>
                          <span style={{
                            padding: '3px 8px',
                            borderRadius: '6px',
                            fontSize: '11px',
                            fontWeight: '600',
                            background: c.has_liked_post === 'Ya' ? 'rgba(16, 185, 129, 0.15)' : 'rgba(255,255,255,0.05)',
                            color: c.has_liked_post === 'Ya' ? 'var(--accent-green)' : 'var(--text-muted)'
                          }}>
                            {c.has_liked_post || 'N/A'}
                          </span>
                        </td>
                        <td style={{ padding: '12px 16px', textAlign: 'center', color: 'var(--accent-blue)', fontWeight: '600' }}>
                          {(c.total_comment_likes || 0).toLocaleString()}
                        </td>
                        <td style={{ padding: '12px 16px', textAlign: 'center' }}>
                          <button
                            onClick={() => setSelectedUserDetail(c)}
                            className="btn-secondary"
                            style={{ padding: '4px 10px', fontSize: '11px' }}
                          >
                            Detail ({c.unique_posts_count} post)
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Modal User Detail Comments */}
      {selectedUserDetail && (
        <div style={{
          position: 'fixed',
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          background: 'rgba(0, 0, 0, 0.75)',
          backdropFilter: 'blur(8px)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          zIndex: 100,
          padding: '20px'
        }}>
          <div className="glass-panel" style={{ width: '100%', maxWidth: '750px', maxHeight: '85vh', display: 'flex', flexDirection: 'column', padding: '24px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
              <div>
                <h3 style={{ fontSize: '18px', fontWeight: '800', color: 'var(--accent-cyan)' }}>
                  Detail Komentar @{selectedUserDetail.username}
                </h3>
                <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                  Peringkat #{selectedUserDetail.rank} • Total {selectedUserDetail.comment_count} Komentar
                </span>
              </div>
              <button
                onClick={() => setSelectedUserDetail(null)}
                className="btn-secondary"
                style={{ padding: '6px 14px', borderRadius: '8px' }}
              >
                ✕ Tutup
              </button>
            </div>

            <div style={{ overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '12px', paddingRight: '6px' }}>
              {(() => {
                const userComments = Array.isArray(analysisResult?.detail_comments)
                  ? analysisResult.detail_comments.filter((c: any) =>
                      (c.commenter_username || c.username) === selectedUserDetail.username
                    )
                  : ((analysisResult?.detail_comments as Record<string, any[]>)?.[selectedUserDetail.username] || []);

                if (userComments.length === 0) {
                  return (
                    <div style={{ textAlign: 'center', padding: '30px', color: 'var(--text-muted)', fontSize: '13px' }}>
                      Tidak ada detail riwayat komentar tersimpan untuk @{selectedUserDetail.username}.
                    </div>
                  );
                }

                return userComments.map((comm: any, idx: number) => (
                  <div
                    key={idx}
                    style={{
                      padding: '14px',
                      borderRadius: '10px',
                      background: 'rgba(0, 0, 0, 0.3)',
                      border: '1px solid var(--border-color)'
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '6px', fontSize: '11px', color: 'var(--text-muted)' }}>
                      <span>Tanggal: {formatIndoDateTime(comm.comment_date)}</span>
                      <span>👍 {comm.comment_likes || 0} Like Komentar</span>
                    </div>
                    <p style={{ fontSize: '13px', color: 'var(--text-primary)', marginBottom: '8px', lineHeight: '1.4' }}>
                      "{comm.comment_text || ''}"
                    </p>
                    <div style={{ fontSize: '11px', color: 'var(--accent-blue)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      {comm.post_url ? (
                        <button
                          type="button"
                          onClick={() => handleOpenExternalUrl(comm.post_url)}
                          style={{
                            background: 'none',
                            border: 'none',
                            color: 'var(--accent-cyan)',
                            cursor: 'pointer',
                            padding: 0,
                            fontSize: '12px',
                            fontWeight: '600',
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '4px',
                            textDecoration: 'underline'
                          }}
                        >
                          🔗 Buka Postingan Target ↗
                        </button>
                      ) : (
                        <span style={{ color: 'var(--text-muted)' }}>-</span>
                      )}
                      <span style={{ color: 'var(--text-muted)' }}>Likes Post: {comm.post_likes || 0}</span>
                    </div>
                  </div>
                ));
              })()}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
