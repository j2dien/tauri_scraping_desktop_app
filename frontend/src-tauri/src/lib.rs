use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::Manager;

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

pub struct BackendProcess(pub Mutex<Option<Child>>);

fn find_backend_executable(app: &tauri::App) -> Option<(PathBuf, PathBuf, bool)> {
  // Returns: (executable_path, working_dir, is_python_script)

  let mut candidate_paths: Vec<PathBuf> = Vec::new();

  // 1. Cek di Resource Directory Tauri (Production bundle / Installer)
  if let Ok(res_dir) = app.path().resource_dir() {
    candidate_paths.push(res_dir.join("resources").join("server-backend").join("server-backend.exe"));
    candidate_paths.push(res_dir.join("server-backend").join("server-backend.exe"));
    candidate_paths.push(res_dir.join("server-backend.exe"));
    candidate_paths.push(res_dir.join("_up_").join("_up_").join("dist").join("server-backend").join("server-backend.exe"));
  }

  // 2. Cek di samping file .exe aplikasi (Portable / Installed root)
  if let Ok(mut exe_dir) = std::env::current_exe() {
    exe_dir.pop();
    candidate_paths.push(exe_dir.join("resources").join("server-backend").join("server-backend.exe"));
    candidate_paths.push(exe_dir.join("server-backend").join("server-backend.exe"));
    candidate_paths.push(exe_dir.join("server-backend.exe"));
    candidate_paths.push(exe_dir.join("_up_").join("_up_").join("dist").join("server-backend").join("server-backend.exe"));
    candidate_paths.push(exe_dir.join("_up_").join("dist").join("server-backend").join("server-backend.exe"));
  }

  // 3. Cek di folder proyek kerja (Development mode)
  let mut current = std::env::current_dir().unwrap_or_default();
  for _ in 0..5 {
    candidate_paths.push(current.join("frontend").join("src-tauri").join("resources").join("server-backend").join("server-backend.exe"));
    candidate_paths.push(current.join("src-tauri").join("resources").join("server-backend").join("server-backend.exe"));
    candidate_paths.push(current.join("resources").join("server-backend").join("server-backend.exe"));
    candidate_paths.push(current.join("dist").join("server-backend").join("server-backend.exe"));
    if !current.pop() {
      break;
    }
  }

  // Periksa kandidat binary server-backend.exe
  for candidate in candidate_paths {
    if candidate.exists() && candidate.is_file() {
      let work_dir = candidate.parent().unwrap_or(&candidate).to_path_buf();
      return Some((candidate, work_dir, false));
    }
  }

  // 4. Fallback ke script python server.py jika binary belum di-build
  let mut current_py = std::env::current_dir().unwrap_or_default();
  for _ in 0..5 {
    let candidate_script = current_py.join("server.py");
    if candidate_script.exists() {
      return Some((PathBuf::from("python"), current_py.clone(), true));
    }
    if !current_py.pop() {
      break;
    }
  }

  None
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .manage(BackendProcess(Mutex::new(None)))
    .setup(|app| {
      #[cfg(target_os = "windows")]
      {
        // Kill any existing backend process on port 8008
        let _ = Command::new("powershell")
          .creation_flags(0x08000000)
          .args(["-Command", "Get-NetTCPConnection -LocalPort 8008 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"])
          .output();
      }

      if let Some((exe_path, work_dir, is_script)) = find_backend_executable(app) {
        let mut cmd = Command::new(&exe_path);
        cmd.current_dir(&work_dir);

        #[cfg(target_os = "windows")]
        {
          cmd.creation_flags(0x08000000);
        }

        if is_script {
          cmd.arg("server.py");
        }

        if let Ok(c) = cmd.spawn() {
          let state = app.state::<BackendProcess>();
          let mut guard = state.0.lock().unwrap();
          *guard = Some(c);
        }
      }

      Ok(())
    })
    .on_window_event(|window, event| {
      if let tauri::WindowEvent::Destroyed = event {
        let state = window.state::<BackendProcess>();
        let mut guard = state.0.lock().unwrap();
        if let Some(mut child) = guard.take() {
          let _ = child.kill();
        }

        #[cfg(target_os = "windows")]
        {
          let _ = Command::new("powershell")
            .creation_flags(0x08000000)
            .args(["-Command", "Get-NetTCPConnection -LocalPort 8008 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"])
            .output();
        }
      }
    })
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}
