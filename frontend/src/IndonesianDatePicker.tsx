import React, { useState, useRef, useEffect } from 'react';
import type { IndonesianDatePickerProps } from './types';

const MONTH_NAMES_ID = [
  'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
  'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember'
];

const DAY_NAMES_ID = ['Min', 'Sen', 'Sel', 'Rab', 'Kam', 'Jum', 'Sab'];

export const IndonesianDatePicker: React.FC<IndonesianDatePickerProps> = ({ label, value, onChange, align = 'left' }) => {
  const [isOpen, setIsOpen] = useState<boolean>(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Parse initial value from "DD-MM-YYYY"
  const parseDMY = (str: string): Date => {
    if (!str) return new Date();
    const parts = str.split('-');
    if (parts.length !== 3) return new Date();
    return new Date(parseInt(parts[2], 10), parseInt(parts[1], 10) - 1, parseInt(parts[0], 10));
  };

  const selectedDate = parseDMY(value);
  const [viewYear, setViewYear] = useState<number>(selectedDate.getFullYear());
  const [viewMonth, setViewMonth] = useState<number>(selectedDate.getMonth());

  useEffect(() => {
    const d = parseDMY(value);
    setViewYear(d.getFullYear());
    setViewMonth(d.getMonth());
  }, [value]);

  // Click outside to close
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Format display in Indonesian: e.g. "24 Agustus 2026"
  const displayFormatted = (): string => {
    if (!value) return 'Pilih Tanggal';
    const parts = value.split('-');
    if (parts.length !== 3) return value;
    const day = parseInt(parts[0], 10);
    const month = MONTH_NAMES_ID[parseInt(parts[1], 10) - 1] || parts[1];
    const year = parts[2];
    return `${day} ${month} ${year}`;
  };

  // Calendar calculations
  const firstDayOfMonth = new Date(viewYear, viewMonth, 1).getDay();
  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
  const daysInPrevMonth = new Date(viewYear, viewMonth, 0).getDate();

  const handlePrevMonth = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (viewMonth === 0) {
      setViewMonth(11);
      setViewYear(viewYear - 1);
    } else {
      setViewMonth(viewMonth - 1);
    }
  };

  const handleNextMonth = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (viewMonth === 11) {
      setViewMonth(0);
      setViewYear(viewYear + 1);
    } else {
      setViewMonth(viewMonth + 1);
    }
  };

  const handleSelectDay = (day: number) => {
    const dStr = String(day).padStart(2, '0');
    const mStr = String(viewMonth + 1).padStart(2, '0');
    const yStr = String(viewYear);
    onChange(`${dStr}-${mStr}-${yStr}`);
    setIsOpen(false);
  };

  return (
    <div ref={containerRef} style={{ position: 'relative', width: '100%', zIndex: isOpen ? 1000 : 1 }}>
      {label && (
        <span style={{ fontSize: '11px', color: 'var(--text-muted)', display: 'block', marginBottom: '4px' }}>
          {label}
        </span>
      )}
      
      {/* Clickable Indonesian Input Trigger */}
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className="custom-input"
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          textAlign: 'left',
          cursor: 'pointer',
          padding: '10px 14px',
          background: isOpen ? 'rgba(6, 182, 212, 0.12)' : 'rgba(0, 0, 0, 0.3)',
          borderColor: isOpen ? 'var(--accent-cyan)' : 'var(--border-color)',
        }}
      >
        <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>
          {displayFormatted()}
        </span>
        <span style={{ fontSize: '15px', color: 'var(--accent-cyan)' }}>📅</span>
      </button>

      {/* Pop-up Indonesian Calendar Card */}
      {isOpen && (
        <div
          className="glass-panel"
          style={{
            position: 'absolute',
            top: 'calc(100% + 6px)',
            ...(align === 'right' ? { right: 0 } : { left: 0 }),
            zIndex: 9999,
            width: '280px',
            padding: '16px',
            background: '#0d131f',
            boxShadow: '0 20px 50px rgba(0, 0, 0, 0.9), 0 0 0 1px rgba(6, 182, 212, 0.4)',
            border: '1px solid rgba(6, 182, 212, 0.35)',
            borderRadius: '14px',
          }}
        >
          {/* Header: Month & Year Navigator */}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
            <button
              type="button"
              onClick={handlePrevMonth}
              className="btn-secondary"
              style={{ padding: '4px 8px', fontSize: '12px', borderRadius: '8px' }}
            >
              ◀
            </button>

            <span style={{ fontWeight: '700', fontSize: '13px', color: 'var(--accent-cyan)' }}>
              {MONTH_NAMES_ID[viewMonth]} {viewYear}
            </span>

            <button
              type="button"
              onClick={handleNextMonth}
              className="btn-secondary"
              style={{ padding: '4px 8px', fontSize: '12px', borderRadius: '8px' }}
            >
              ▶
            </button>
          </div>

          {/* Day of Week Headers (Indonesia: Min, Sen, Sel, Rab, Kam, Jum, Sab) */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', textAlign: 'center', marginBottom: '6px' }}>
            {DAY_NAMES_ID.map((dayName, idx) => (
              <span
                key={dayName}
                style={{
                  fontSize: '11px',
                  fontWeight: '700',
                  color: idx === 0 ? 'var(--accent-pink)' : 'var(--text-muted)',
                  padding: '4px 0',
                }}
              >
                {dayName}
              </span>
            ))}
          </div>

          {/* Days Grid */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: '4px' }}>
            {/* Empty slots before day 1 */}
            {Array.from({ length: firstDayOfMonth }).map((_, idx) => {
              const prevDateNum = daysInPrevMonth - firstDayOfMonth + idx + 1;
              return (
                <div
                  key={`prev-${idx}`}
                  style={{
                    fontSize: '11px',
                    color: 'rgba(255, 255, 255, 0.15)',
                    padding: '8px 0',
                    textAlign: 'center',
                  }}
                >
                  {prevDateNum}
                </div>
              );
            })}

            {/* Days of current month */}
            {Array.from({ length: daysInMonth }).map((_, idx) => {
              const dayNum = idx + 1;
              const isSelected =
                selectedDate.getDate() === dayNum &&
                selectedDate.getMonth() === viewMonth &&
                selectedDate.getFullYear() === viewYear;

              return (
                <button
                  key={dayNum}
                  type="button"
                  onClick={() => handleSelectDay(dayNum)}
                  style={{
                    padding: '8px 0',
                    fontSize: '12px',
                    fontWeight: isSelected ? '800' : '500',
                    borderRadius: '8px',
                    border: 'none',
                    cursor: 'pointer',
                    background: isSelected ? 'var(--gradient-primary)' : 'transparent',
                    color: isSelected ? '#fff' : 'var(--text-primary)',
                    boxShadow: isSelected ? '0 2px 10px rgba(6, 182, 212, 0.5)' : 'none',
                    transition: 'all 0.15s ease',
                  }}
                  onMouseEnter={(e) => {
                    if (!isSelected) (e.currentTarget as HTMLButtonElement).style.background = 'rgba(255, 255, 255, 0.08)';
                  }}
                  onMouseLeave={(e) => {
                    if (!isSelected) (e.currentTarget as HTMLButtonElement).style.background = 'transparent';
                  }}
                >
                  {dayNum}
                </button>
              );
            })}
          </div>

          {/* Quick Select Today Button */}
          <div style={{ marginTop: '12px', paddingTop: '10px', borderTop: '1px solid var(--border-color)', textAlign: 'center' }}>
            <button
              type="button"
              onClick={() => {
                const now = new Date();
                const dStr = String(now.getDate()).padStart(2, '0');
                const mStr = String(now.getMonth() + 1).padStart(2, '0');
                const yStr = String(now.getFullYear());
                onChange(`${dStr}-${mStr}-${yStr}`);
                setIsOpen(false);
              }}
              style={{
                fontSize: '11px',
                color: 'var(--accent-cyan)',
                background: 'none',
                border: 'none',
                cursor: 'pointer',
                fontWeight: '600'
              }}
            >
              📅 Pilih Hari Ini ({new Date().getDate()} {MONTH_NAMES_ID[new Date().getMonth()]})
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default IndonesianDatePicker;
