// the working copy the app is built from: its menu's Chats and Server runs that copy's ./lagom. LAGOM_REPO= (empty) builds an
// app for other machines, with no working copy and no path of the builder's in it (as linux/stamp.js does)
fn main() {
    println!("cargo:rerun-if-env-changed=LAGOM_REPO");
    let repo = std::env::var("LAGOM_REPO").unwrap_or_else(|_| {
        std::path::Path::new(&std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("../..").to_string_lossy().into_owned()
    });
    println!("cargo:rustc-env=LAGOM_REPO={repo}");
    tauri_build::build()
}
