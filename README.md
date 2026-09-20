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
| **JS Package Runner** | [Bun](https://bun.sh/) |
| **Styling** | Vanilla CSS (Dark Glassmorphism Design System) |
| **Backend API Engine**| Python 3.10+, [FastAPI](https://fastapi.tiangolo.com/), Uvicorn, WebSockets |
| **Scraping Core** | [Playwright](https://playwright.dev/python/) (TikTok), [Instagrapi](https://github.com/subzeroid/instagrapi) (Instagram) |
| **Data Processing & Export** | OpenPyXL, Regex, Pydantic v2 |

---

## 📋 Prasyarat Sistem

Project desktop saat ini ditujukan untuk Windows. Sebelum menjalankan aplikasi,
pastikan sistem telah memiliki:

1. **Python 3.10+**: [python.org](https://www.python.org/) (Python 3.12 direkomendasikan).
2. **Bun**: [bun.sh](https://bun.sh/). Script Tauri saat ini menjalankan Bun secara langsung.
3. **Rust stable, Cargo, dan rustup**: [rustup.rs](https://rustup.rs/).
4. **Microsoft C++ Build Tools** dengan workload **Desktop development with C++**.
5. **Microsoft Edge WebView2 Runtime**. Komponen ini biasanya sudah tersedia pada Windows 10/11.

Pastikan tool utama dapat ditemukan dari PowerShell:

```powershell
python --version
bun --version
rustc --version
cargo --version
```

Jika `cargo` baru saja diinstal tetapi belum dikenali, tutup seluruh VS Code lalu
buka kembali. Rustup biasanya menambahkan `%USERPROFILE%\.cargo\bin` ke `PATH`.

---

## 🚀 Panduan Instalasi

### 1. Clone Repository

```powershell
git clone https://github.com/j2dien/tauri_scraping_desktop_app.git
cd tauri_scraping_desktop_app
```

### 2. Setup Backend Python

Buat virtual environment agar dependensi project tidak tercampur dengan instalasi
Python global:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Gunakan `python -m pip` dan `python -m playwright` agar perintah selalu memakai
interpreter dari virtual environment aktif. Warning bahwa folder `Scripts` global
tidak ada di `PATH` dapat diabaikan jika perintah dijalankan dengan pola ini.

### 3. Setup Frontend

```powershell
cd frontend
bun install
cd ..
```

---

## 🖥️ Cara Menjalankan Aplikasi

### Opsi 1: Menjalankan Versi Desktop (Tauri)

Aktifkan virtual environment Python terlebih dahulu, kemudian jalankan script dari
root repository:

```powershell
.\.venv\Scripts\Activate.ps1
bun run desktop
```

Alternatifnya, setelah virtual environment aktif, jalankan:

```powershell
.\start_desktop.bat
```

Tauri akan menjalankan Vite dan `server.py` secara otomatis. Konfigurasi
`tauri.dev.conf.json` menonaktifkan resource binary production selama mode
development, sehingga binary `server-backend.exe` tidak diperlukan untuk
`tauri dev`.

### Opsi 2: Menjalankan Mode Web / Dev Terpisah
Jika ingin menjalankan backend FastAPI dan Web UI secara terpisah:

**Terminal 1 — Backend FastAPI:**

```powershell
.\.venv\Scripts\Activate.ps1
python server.py
# Server berjalan di http://127.0.0.1:8008
```

**Terminal 2 — Frontend Vite:**

```powershell
cd frontend
bun run dev
# Buka http://localhost:5174 di browser
```

### Build Frontend dan Desktop

Build frontend saja dapat dijalankan dari root repository:

```powershell
bun run build
```

Script `bun run build:desktop` ditujukan untuk build installer production. Sebelum
menjalankannya, backend Python harus sudah dipaketkan sebagai:

```text
frontend/src-tauri/resources/server-backend/server-backend.exe
```

Binary tersebut belum disediakan atau dibuat otomatis oleh repository saat ini.
Mode development tidak memiliki kebutuhan ini.

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
│   │   ├── tauri.conf.json     # Konfigurasi utama dan bundle production
│   │   └── tauri.dev.conf.json # Override resource untuk mode development
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

### Troubleshooting singkat

- **`pip` tidak dikenali**: jalankan `python -m pip ...`, bukan `pip ...`.
- **`cargo metadata ... program not found`**: instal Rust melalui rustup, lalu buka ulang VS Code.
- **`resources/server-backend/**/* path not found` pada mode dev**: gunakan `bun run desktop`, karena script ini memuat `tauri.dev.conf.json`. Jangan menjalankan `bun run tauri dev` secara langsung.
- **Backend tidak tersambung**: pastikan virtual environment aktif sebelum menjalankan Tauri dan cek `http://127.0.0.1:8008/api/health`.

---

## 📄 Lisensi
Proyek ini dibuat untuk keperluan internal dan riset data engagement media sosial.
