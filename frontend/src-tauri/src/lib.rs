use std::process::{Command, Child};
use std::sync::Mutex;
use tauri::Manager;

pub struct BackendProcess(pub Mutex<Option<Child>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .manage(BackendProcess(Mutex::new(None)))
    .setup(|app| {
      let mut server_dir = std::env::current_dir().unwrap_or_default();
      if server_dir.ends_with("src-tauri") {
        if let Some(p) = server_dir.parent().and_then(|p| p.parent()) {
          server_dir = p.to_path_buf();
        }
      } else if server_dir.ends_with("frontend") {
        if let Some(p) = server_dir.parent() {
          server_dir = p.to_path_buf();
        }
      }

      let server_script = server_dir.join("server.py");

      #[cfg(target_os = "windows")]
      {
        // Kill any existing backend process on port 8008
        let _ = Command::new("powershell")
          .args(["-Command", "Get-NetTCPConnection -LocalPort 8008 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"])
          .output();
      }

      let child = Command::new("python")
        .arg(&server_script)
        .current_dir(&server_dir)
        .spawn();

      if let Ok(c) = child {
        let state = app.state::<BackendProcess>();
        let mut guard = state.0.lock().unwrap();
        *guard = Some(c);
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
      }
    })
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}
