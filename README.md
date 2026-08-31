# ⚡ Social Media Top Commenter Analyzer — Desktop App

Aplikasi desktop modern dan bertenaga untuk melakukan **scraping, analisis interaksi, dan penentuan Top Commenters / Pemenang Giveaway** pada platform **TikTok** dan **Instagram** berdasarkan rentang tanggal tertentu, dilengkapi dengan fitur ekspor laporan ke format Microsoft Excel (`.xlsx`).

Dibangun menggunakan kombinasi teknologi berkinerja tinggi: **Tauri v2 (Rust)**, **React 19 + TypeScript (Bun/Vite)**, dan backend engine **Python (FastAPI + Playwright + Instagrapi)**.

---

## ✨ Fitur Utama

- 🎵 **Multi-Platform Scraping**:
  - **TikTok**: Mendukung scraping postingan dan seluruh komentar menggunakan engine Playwright (tanpa API berbayar).
  - **Instagram**: Mendukung scraping akun publik/target dengan integrasi autentikasi `instagrapi`.
- 📅 **Filter Rentang Tanggal Fleksibel**:
  - Date picker format Indonesia (`DD-MM-YYYY`).
  - *Quick Presets*: 7 Hari Terakhir, 14 Hari Terakhir, 30 Hari Terakhir, dan Bulan Ini.
- 🏆 **Analisis Peringkat Top Commenters**:
  - Penentuan peringkat (Rank #1 s/d Top N) berdasarkan total kuantitas komentar yang ditinggalkan.
  - Menampilkan waktu komentar pertama (*earliest comment*), status like, total like komentar, dan jumlah post unik yang dikomentari.
- 💬 **Detail Riwayat Komentar per User**:
  - Modal interaktif untuk melihat seluruh kutipan komentar yang dibuat oleh user tertentu.
  - Link langsung untuk membuka postingan target di browser bawaan sistem.
- 📊 **Statistik & Ringkasan Engagement**:
  - Total postingan yang dipindai, total komentar, rata-rata komentar per post, total post likes, dan *unique commenters*.
- 📥 **Export ke Excel Otomatis**:
  - Menghasilkan file `.xlsx` dengan format rapi, styling tabel profesional, dan multiple sheet (Ringkasan, Peringkat Top Commenters, serta Semua Riwayat Komentar).
- ⚡ **Real-Time Live Monitor**:
  - Live progress bar dan terminal logs bertenaga **WebSocket** dengan mekanisme *Polling Fallback* agar UI tetap responsif.

---

## 🛠️ Tech Stack

| Komponen | Teknologi |
| --- | --- |
| **Desktop Shell** | [Tauri v2](https://v2.tauri.app/) (Rust) |
| **Frontend Framework** | React 19, TypeScript, Vite |
| **JS Package Runner** | [Bun](https://bun.sh/) (atau Node.js / NPM) |
| **Styling** | Vanilla CSS (Dark Glassmorphism Design System) |
| **Backend API Engine**| Python 3.10+, [FastAPI](https://fastapi.tiangolo.com/), Uvicorn, WebSockets |
| **Scraping Core** | [Playwright](https://playwright.dev/python/) (TikTok), [Instagrapi](https://github.com/subzeroid/instagrapi) (Instagram) |
| **Data Processing & Export** | OpenPyXL, Regex, Pydantic v2 |

---

## 📋 Prasyarat Sistem

Sebelum menjalankan aplikasi, pastikan sistem Anda telah terpasang:

1. **Python 3.10+**: [python.org](https://www.python.org/)
2. **Bun** (disarankan) atau **Node.js 18+**: [bun.sh](https://bun.sh/)
3. **Rust & Cargo** (diperlukan untuk Tauri v2): [rustup.rs](https://rustup.rs/)

---

## 🚀 Panduan Instalasi

### 1. Clone Repository
```bash
git clone https://github.com/j2dien/tauri_scraping_desktop_app.git
cd tauri_scraping_desktop_app
```

### 2. Setup Backend Python
Buat virtual environment (opsional namun disarankan) dan install dependensi:
```bash
# Buat dan aktifkan virtual environment (opsional)
python -m venv venv
# Windows:
.\venv\Scripts\activate

# Install dependensi Python
pip install -r requirements.txt

# Install browser Playwright untuk engine TikTok
playwright install chromium
```

### 3. Setup Frontend
Masuk ke folder `frontend` dan install dependensi package:
```bash
cd frontend
bun install
# atau jika menggunakan npm: npm install
cd ..
```

---

## 🖥️ Cara Menjalankan Aplikasi

### Opsi 1: Menjalankan Versi Desktop (Tauri)
Cukup klik ganda file **`start_desktop.bat`** atau jalankan perintah:
```bash
cd frontend
bun run desktop
# atau: npm run desktop
```

### Opsi 2: Menjalankan Mode Web / Dev Terpisah
Jika ingin menjalankan backend FastAPI dan Web UI secara terpisah:

**Jalankan Backend Server:**
```bash
python server.py
# Server berjalan di http://127.0.0.1:8008
```

**Jalankan Frontend Dev Server:**
```bash
cd frontend
bun run dev
# Buka http://localhost:5173 di browser
```

---

## 📁 Struktur Folder

```text
tauri_scraping_desktop_app/
├── core/                       # Python Core Scraping & Processing
│   ├── analyzer.py             # Agregasi & ranking top commenter
│   ├── exporter.py             # Generator laporan Excel (.xlsx)
│   ├── scraper_instagram.py    # Engine scraping Instagram via Instagrapi
│   └── scraper_tiktok.py       # Engine scraping TikTok via Playwright
├── exports/                    # Folder output hasil export file Excel
├── frontend/                   # React + TypeScript Frontend
│   ├── src/                    # Komponen React, CSS, types, & logic
│   │   ├── App.tsx             # Antarmuka utama aplikasi
│   │   ├── IndonesianDatePicker.tsx # Komponen pemilih tanggal kustom
│   │   ├── types.ts            # Definisi tipe data TypeScript
│   │   └── index.css           # Styling tema dark glassmorphism
│   ├── src-tauri/              # Konfigurasi & Source Code Rust Tauri v2
│   └── package.json            # Script frontend & dependensi JS
├── desktop_launcher.py         # Alternatif Desktop Launcher (PyWebView)
├── requirements.txt            # Dependensi Python
├── server.py                   # FastAPI REST & WebSocket Backend
├── start_desktop.bat           # Batch script launcher sekali klik
└── README.md                   # Dokumentasi proyek
```

---

## ⚠️ Catatan & Best Practices

- **Instagram Scraping**: Instagram memiliki proteksi *rate limit* yang ketat. Disarankan menggunakan **akun Instagram sekunder / dummy** khusus untuk keperluan scraping, dan hindari melakukan scanning ribuan post dalam waktu berdekatan.
- **TikTok Scraping**: Menggunakan profil browser Playwright lokal yang tersimpan di folder `.tiktok_browser_profile/` untuk menjaga stabilitas sesi penjelajahan.
- **Penyimpanan Build Rust (`target/`)**: Folder kompilasi Rust (`frontend/src-tauri/target/`) dapat dibersihkan kapan saja dengan perintah `cargo clean` di folder `src-tauri` jika ingin menghemat ruang disk.

---

## 📄 Lisensi
Proyek ini dibuat untuk keperluan internal dan riset data engagement media sosial.
