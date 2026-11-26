#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use tauri::menu::{AboutMetadata, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent, Wry};

// the app's own start page, where the chat's address is entered
#[cfg(not(windows))]
const START: &str = "tauri://localhost/index.html";
#[cfg(windows)]
const START: &str = "http://tauri.localhost/index.html";

// the manager of this machine's chats (the owner, #2011): the working copy the app was built from runs it with
// `./lagom --app` - started in the background if it is not up, it prints "<address> <chats running>"; none running: the
// app shows its wizard. An app copied to a machine without that working copy has no manager and is as it was
static MANAGER: Mutex<Option<String>> = Mutex::new(None);

fn repo() -> Option<PathBuf> {
    let p = env!("LAGOM_REPO");                            // set by build.rs; empty: an app for other machines
    (!p.is_empty() && Path::new(p).join("lagom").is_file()).then(|| PathBuf::from(p))
}

fn manager() -> Option<(String, u32, Option<String>)> {
    let repo = repo()?;
    let out = Command::new(repo.join("lagom"))
        .arg("--app")
        .current_dir(&repo)
        // an app started from the Dock has a bare PATH: python3 of Homebrew / the system
        .env("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let mut w = text.lines().last()?.split_whitespace();
    let url = w.next()?.to_string();
    let running = w.next().and_then(|n| n.parse().ok()).unwrap_or(1);
    let first = w.next().map(str::to_string);
    *MANAGER.lock().unwrap() = Some(url.clone());
    Some((url, running, first))
}

fn open_manager(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        // asked again each time: a manager started anew has a new key (cheap when it is up)
        let Some(url) = manager().map(|m| m.0).or_else(|| MANAGER.lock().unwrap().clone()) else { return };
        if let (Some(main), Ok(u)) = (app.get_webview_window("main"), url.parse::<Url>()) {
            let _ = main.navigate(u);
            let _ = main.set_focus();
        }
    });
}

// the page counts what is unread in its title, "(3) Name" (the owner, #812): the number goes to the Dock's badge, the window's title is the name
// alone. A title with no "(N) " in front takes the badge off. Nothing but the title comes from the page: no commands, no permissions for it
fn split_badge(title: &str) -> (Option<i64>, &str) {
    if let Some((n, rest)) = title.strip_prefix('(').and_then(|t| t.split_once(") ")) {
        if (1..=4).contains(&n.len()) && n.bytes().all(|b| b.is_ascii_digit()) {
            return (n.parse().ok().filter(|&n| n > 0), rest);
        }
    }
    (None, title)
}

// a notice the page asks for (the owner, #2193): it puts " [notify]{"t":"<title>","b":"<text>"}" after its title for a moment,
// the app shows it as a notification of macOS. The page decides what is worth one (its Notifications setting); here only the
// text is taken, a few a second at most - still nothing but the title comes from the page
static LAST_NOTICE: Mutex<Option<(std::time::Instant, String)>> = Mutex::new(None);

fn split_notify(title: &str) -> (&str, Option<(String, String)>) {
    let Some((base, json)) = title.split_once(" [notify]") else { return (title, None) };
    let v: serde_json::Value = serde_json::from_str(json).unwrap_or_default();
    let get = |k: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or("").chars().take(240).collect::<String>();
    let (t, b) = (get("t"), get("b"));
    (base, (!t.is_empty() || !b.is_empty()).then_some((t, b)))
}

fn notify(app: &AppHandle, title: String, body: String) {
    use tauri_plugin_notification::NotificationExt;
    // the title is set anew while the notice is in it (the badge repaints): the same notice within 2 s is that one, not another
    let key = format!("{title}\n{body}");
    let mut last = LAST_NOTICE.lock().unwrap();
    if let Some((t, k)) = last.as_ref() {
        if t.elapsed() < std::time::Duration::from_millis(250) || (*k == key && t.elapsed() < std::time::Duration::from_secs(2)) {
            return;
        }
    }
    *last = Some((std::time::Instant::now(), key));
    let _ = app.notification().builder().title(title).body(body).show();
}

// a window on the chat's page: links that open a new window go to the default browser, a webview has no tabs
#[cfg(target_os = "macos")]
const TITLE_TAG: isize = 2322;

// our centred title label in the window's title bar: made the first time, its text set every time after
#[cfg(target_os = "macos")]
fn center_title<R: tauri::Runtime>(window: &tauri::WebviewWindow<R>, text: &str) {
    let (w, text) = (window.clone(), text.to_string());
    let _ = window.run_on_main_thread(move || {
        use objc2_app_kit::{NSColor, NSFont, NSTextField, NSView, NSWindow, NSWindowButton, NSWindowTitleVisibility};
        use objc2_foundation::NSString;
        let Some(mtm) = objc2::MainThreadMarker::new() else { return };
        let Ok(ptr) = w.ns_window() else { return };
        let ns: &NSWindow = unsafe { &*(ptr as *const NSWindow) };
        ns.setTitleVisibility(NSWindowTitleVisibility::Hidden);
        let Some(close) = ns.standardWindowButton(NSWindowButton::CloseButton) else { return };
        let Some(bar) = (unsafe { close.superview() }) else { return };
        let s = NSString::from_str(&text);
        for v in bar.subviews().iter() {
            if let Ok(f) = v.downcast::<NSTextField>() {
                if f.tag() == TITLE_TAG {
                    f.setStringValue(&s);
                    return;
                }
            }
        }
        let f = NSTextField::labelWithString(&s, mtm);
        f.setTag(TITLE_TAG);
        f.setFont(Some(&NSFont::titleBarFontOfSize(0.0)));
        f.setTextColor(Some(&NSColor::labelColor()));
        f.setTranslatesAutoresizingMaskIntoConstraints(false);
        bar.addSubview(&f);
        let v: &NSView = &f;
        v.centerXAnchor().constraintEqualToAnchor(&bar.centerXAnchor()).setActive(true);
        v.centerYAnchor().constraintEqualToAnchor(&close.centerYAnchor()).setActive(true);
    });
}

fn page_window(
    app: &AppHandle,
    label: &str,
    url: WebviewUrl,
    title: &str,
    size: (f64, f64),
    min: (f64, f64),
) -> tauri::Result<()> {
    let mut builder = WebviewWindowBuilder::new(app, label, url);
    // the app's version for the page's About (the owner, #2484, #2494)
    let ver = format!("window.__acAppVer = {:?};", app.package_info().version.to_string());
    // the main window's title follows the page's: the chat's own name; the settings window stays "Settings"
    if label == "main" {
        builder = builder
            // the page knows it is in the app by this mark (the badge is drawn only here); the start page and the settings window have no use for it
            .initialization_script(format!("window.__acApp = 1; {ver}"))
            .on_navigation(|u| {
                note_nav(u);
                true
            })
            .on_document_title_changed(|window, title| {
                // Cmd+W on a page that has nothing of its own to close (one chat, not split): the window (the owner, #3799)
                if title.ends_with(" [close]") {
                    let _ = window.close();
                    return;
                }
                keep_page(window.app_handle());
                let (title, notice) = split_notify(&title);
                if let Some((t, b)) = notice {
                    notify(window.app_handle(), t, b);
                }
                let (count, name) = split_badge(title);
                let _ = window.set_badge_count(count);
                if !name.is_empty() {
                    // "LagomChat · <the chat's name>" (the owner, #2322)
                    let t = if name == "LagomChat" { name.to_string() } else { format!("LagomChat · {name}") };
                    let _ = window.set_title(&t);
                    #[cfg(target_os = "macos")]
                    center_title(&window, &t);
                }
            });
    }
    // the settings window: the page says through its title that something is unsaved, and that it may close (the owner, #1079)
    let unsaved = Arc::new(AtomicBool::new(false));
    if label == "settings" {
        let flag = unsaved.clone();
        builder = builder.initialization_script(format!("window.__acAppSet = 1; {ver}")).on_document_title_changed(move |window, title| {
            flag.store(title.ends_with(" [dirty]"), Ordering::SeqCst);
            if title.ends_with(" [close]") {
                let _ = window.close();
            }
        });
    }
    let window = builder
        .title(title)
        .inner_size(size.0, size.1)
        .min_inner_size(min.0, min.1)
        // the page takes dropped files itself
        .disable_drag_drop_handler()
        // a download (the picture viewer's Save, a file's Download; the owner, #3726: saving did nothing, the webview cancels
        // a download that has no handler): into Downloads under a name not taken yet, then shown in the Finder. macOS does not
        // say where a finished one went, so the place is kept from its start, by its address
        .on_download({
            let going: Arc<Mutex<Vec<(String, PathBuf)>>> = Arc::new(Mutex::new(Vec::new()));
            move |_webview, event| {
                match event {
                    DownloadEvent::Requested { url, destination } => {
                        going.lock().unwrap().push((url.to_string(), destination.clone()));
                    }
                    DownloadEvent::Finished { url, path, success } => {
                        let mut g = going.lock().unwrap();
                        let kept = g.iter().position(|(u, _)| *u == url.to_string()).map(|i| g.remove(i).1);
                        if success {
                            if let Some(p) = path.or(kept) {
                                let _ = tauri_plugin_opener::reveal_item_in_dir(p);
                            }
                        }
                    }
                    _ => {}
                }
                true
            }
        })
        .on_new_window(|url, _features| {
            if matches!(url.scheme(), "http" | "https") {
                let _ = tauri_plugin_opener::open_url(url.as_str(), None::<&str>);
            }
            NewWindowResponse::Deny
        })
        .build()?;
    // the title in the middle of the title bar (the owner, #2322): this macOS draws a window's title on the left, so the
    // window keeps its title (the Window menu, Mission Control) hidden and a label of ours shows it centred
    #[cfg(target_os = "macos")]
    if label == "main" {
        center_title(&window, title);
    }
    if label == "settings" {
        let w = window.clone();
        window.on_window_event(move |event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if unsaved.load(Ordering::SeqCst) {
                    api.prevent_close();
                    let _ = w.eval("window.__acAskClose && window.__acAskClose()");
                }
            }
        });
    }
    Ok(())
}

// the main window's page at the app's quit, opened again at its start (the owner, #3802): a split window comes back with its
// chats and their sizes (they are in the address), not the one chat the start page remembers
fn last_file(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_config_dir().ok().map(|d| d.join("last-page.txt"))
}

// the chat's site: where the main window went from the start page (or the page it was opened on). Only its pages are kept
// for the next start - a site a link took the window to is not reopened as the app's own, nor the manager's wizard with its key
static CHAT_SITE: Mutex<Option<String>> = Mutex::new(None);
static AT_START: AtomicBool = AtomicBool::new(true);

// a chat's host without its first name ("lab.lagom.game" -> "lagom.game"): the chats of one domain are one site (the rail
// goes between them); an address of two names or a number keeps all of it
fn site_of(u: &Url) -> Option<String> {
    let h = u.host_str()?.to_lowercase();
    let n: Vec<&str> = h.split('.').collect();
    Some(if n.len() > 2 && h.parse::<std::net::IpAddr>().is_err() { n[1..].join(".") } else { h })
}

fn of_site(u: &Url, site: &str) -> bool {
    u.host_str().map(str::to_lowercase).is_some_and(|h| h == site || h.ends_with(&format!(".{site}")))
}

fn is_manager(u: &Url) -> bool {
    u.query_pairs().any(|(k, _)| k == "k")
}

// every navigation of the main window: the first one off the start page names the chat's site
fn note_nav(u: &Url) {
    if u.as_str().starts_with(START) || !matches!(u.scheme(), "http" | "https") {
        AT_START.store(true, Ordering::SeqCst);
    } else if !is_manager(u) && AT_START.swap(false, Ordering::SeqCst) {
        *CHAT_SITE.lock().unwrap() = site_of(u);
    }
}

fn keep_page(app: &AppHandle) {
    let (Some(main), Some(f)) = (app.get_webview_window("main"), last_file(app)) else { return };
    let Ok(u) = main.url() else { return };
    let site = CHAT_SITE.lock().unwrap().clone();
    if matches!(u.scheme(), "http" | "https") && !is_manager(&u) && site.is_some_and(|s| of_site(&u, &s)) {
        let _ = std::fs::create_dir_all(f.parent().unwrap_or(Path::new(".")));
        let _ = std::fs::write(f, u.as_str());
    }
}

fn last_page(app: &AppHandle) -> Option<Url> {
    let u: Url = std::fs::read_to_string(last_file(app)?).ok()?.trim().parse().ok()?;
    if !matches!(u.scheme(), "http" | "https") || is_manager(&u) {
        return None;
    }
    *CHAT_SITE.lock().unwrap() = site_of(&u);
    AT_START.store(false, Ordering::SeqCst);
    Some(u)
}

// the main window's page closes its split pane in use if it has one; it asks for the window through its title otherwise
fn close_focused(app: &AppHandle) -> tauri::Result<()> {
    let Some(w) = app.webview_windows().into_values().find(|w| w.is_focused().unwrap_or(false)) else { return Ok(()) };
    if w.label() == "main" {
        return w.eval("if (!(window.__acClose && window.__acClose())) document.title += ' [close]';");
    }
    w.close()
}

fn open_settings(app: &AppHandle) -> tauri::Result<()> {
    if let Some(w) = app.get_webview_window("settings") {
        return w.set_focus();
    }
    // the settings of the chat the main window is on; on the start page there is no chat yet
    let Some(main) = app.get_webview_window("main") else { return Ok(()) };
    let mut url = main.url()?;
    if !matches!(url.scheme(), "http" | "https") || url.as_str().starts_with(START) {
        return main.set_focus();
    }
    url.set_path("/");
    url.set_query(None);
    url.set_fragment(Some("settings"));
    page_window(app, "settings", WebviewUrl::External(url), "Settings", (900.0, 680.0), (840.0, 400.0))
}

// back to the start page; the settings window belongs to the chat that is being left
fn change_address(app: &AppHandle) -> tauri::Result<()> {
    if let Some(f) = last_file(app) {
        let _ = std::fs::remove_file(f);              // the start page at the next start too
    }
    if let Some(w) = app.get_webview_window("settings") {
        w.close()?;
    }
    if let Some(main) = app.get_webview_window("main") {
        main.navigate(START.parse::<Url>().expect("the start page's address"))?;
        let _ = main.set_badge_count(None);   // the badge is of the chat that is being left
        main.set_focus()?;
    }
    Ok(())
}

// the people thanked in the About window (the owner, #2365, #2369): the Mac shows them as its credits, Linux as the authors
const CREDITS: &[&str] = &[
    "martyshka", "Luca", "Dominic", "Patrick", "ovtza", "penis lena", "moon", "ienrf", "HACTEHA", "pio", "Fedor", "mama", "Masha",
    "Natasha", "Tsoi", "Gorshok", "Eugene", "Jim", "Alexander", "Adam", "pupsique", "Zello", "Thomas", "Sina", "Scooby", "elkraino", "LazyKarlson",
    "Lysenko Viktor",
];

fn about() -> AboutMetadata<'static> {
    AboutMetadata {
        credits: Some(format!("Credits\n\n{}", CREDITS.join(", "))),
        authors: Some(CREDITS.iter().map(|s| s.to_string()).collect()),
        ..Default::default()
    }
}

fn app_menu(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
    let settings = MenuItem::with_id(app, "settings", "Settings…", true, Some("CmdOrCtrl+,"))?;
    let address = MenuItem::with_id(app, "address", "Change Chat Address…", true, None::<&str>)?;
    let chats = MenuItem::with_id(app, "chats", "Chats and Server…", repo().is_some(), Some("CmdOrCtrl+Shift+M"))?;
    let main = Submenu::with_items(
        app,
        "LagomChat",
        true,
        &[
            &PredefinedMenuItem::about(app, None, Some(about()))?,
            &PredefinedMenuItem::separator(app)?,
            &settings,
            &address,
            &chats,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::services(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::hide(app, None)?,
            &PredefinedMenuItem::hide_others(app, None)?,
            &PredefinedMenuItem::show_all(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::quit(app, None)?,
        ],
    )?;
    let edit = Submenu::with_items(
        app,
        "Edit",
        true,
        &[
            &PredefinedMenuItem::undo(app, None)?,
            &PredefinedMenuItem::redo(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::cut(app, None)?,
            &PredefinedMenuItem::copy(app, None)?,
            &PredefinedMenuItem::paste(app, None)?,
            &PredefinedMenuItem::select_all(app, None)?,
        ],
    )?;
    // Cmd+W: the chat in use when the window is split into several, else the window (the owner, #3799)
    let close = MenuItem::with_id(app, "close", "Close", true, Some("CmdOrCtrl+W"))?;
    let window = Submenu::with_items(
        app,
        "Window",
        true,
        &[
            &PredefinedMenuItem::minimize(app, None)?,
            &PredefinedMenuItem::maximize(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &close,
        ],
    )?;
    Menu::with_items(app, &[&main, &edit, &window])
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .menu(|app| app_menu(app))
        .on_menu_event(|app, event| {
            let _ = match event.id().as_ref() {
                "settings" => open_settings(app),
                "close" => close_focused(app),
                "address" => change_address(app),
                "chats" => {
                    open_manager(app);
                    Ok(())
                }
                _ => Ok(()),
            };
        })
        .setup(|app| {
            let last = last_page(app.handle());
            let start = last.clone().map(WebviewUrl::External).unwrap_or_else(|| WebviewUrl::App("index.html".into()));
            page_window(app.handle(), "main", start, "LagomChat", (1200.0, 820.0), (420.0, 500.0))?;
            // the manager up in the background; no chat running yet: its wizard instead of the address page
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                match manager() {
                    Some((url, 0, _)) => {
                        if let (Some(main), Ok(u)) = (handle.get_webview_window("main"), url.parse::<Url>()) {
                            let _ = main.navigate(u);
                        }
                    }
                    // a chat of this working copy runs: the address page opens it when it remembers no other address
                    Some((_, _, Some(first))) if last.is_none() => {
                        if let (Some(main), Ok(mut u)) = (handle.get_webview_window("main"), START.parse::<Url>()) {
                            u.query_pairs_mut().append_pair("chat", &first);
                            let _ = main.navigate(u);
                        }
                    }
                    _ => {}
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("the app failed to start")
        .run(|app, event| {
            if let RunEvent::ExitRequested { .. } = event {
                keep_page(app);
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chat_site() {
        let u = |s: &str| s.parse::<Url>().unwrap();
        let s = site_of(&u("https://lab.lagom.game/")).unwrap();
        assert_eq!(s, "lagom.game");
        assert!(of_site(&u("https://orbitar.lagom.game/split#x"), &s));
        assert!(!of_site(&u("https://evil-lagom.game/"), &s));
        assert!(!of_site(&u("https://lagom.game.evil.com/"), &s));
        assert_eq!(site_of(&u("http://127.0.0.1:8160/")).unwrap(), "127.0.0.1");
        assert_eq!(site_of(&u("https://example.com/")).unwrap(), "example.com");
        assert!(is_manager(&u("http://127.0.0.1:8159/?k=abc")));
        assert!(!is_manager(&u("https://lab.lagom.game/?m=1")));
        AT_START.store(true, Ordering::SeqCst);
        note_nav(&u("http://127.0.0.1:8159/?k=abc"));
        assert!(AT_START.load(Ordering::SeqCst));
        note_nav(&u("https://lab.lagom.game/"));
        assert_eq!(CHAT_SITE.lock().unwrap().as_deref(), Some("lagom.game"));
        note_nav(&u("https://evil.com/"));
        assert_eq!(CHAT_SITE.lock().unwrap().as_deref(), Some("lagom.game"));
    }
}
