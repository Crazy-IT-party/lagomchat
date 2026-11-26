// LagomChat for iPhone and iPad: the chat's page in the system's web view (the owner, #3684), the way the Mac app shows it.
// The app's own part is small: the start page where the chat's address is entered (the Mac app's, desktop/dist/index.html),
// the address remembered, the count of unread messages on the icon, the page's notices as notifications, the camera and the
// microphone given to the chat's page, links to other windows opened in the browser, downloads handed to the share sheet.
// Nothing but the title comes from the page to the app: "(3) Name" is the badge, " [notify]{...}" after it is a notice.
import SwiftUI
import UserNotifications
import WebKit

@main
struct LagomChatApp: App {
    @StateObject private var chat = Chat()

    var body: some Scene {
        WindowGroup {
            WebBox(web: chat.web)
                .background(Color(chat.under).ignoresSafeArea())      // around the page: the page's own colour, not a white strip
                .onReceive(NotificationCenter.default.publisher(for: UIApplication.didBecomeActiveNotification)) { _ in chat.back() }
        }
        .commands {
            // an iPad with a keyboard or a menu bar; an iPhone changes the address in Settings - LagomChat
            CommandGroup(after: .appSettings) {
                Button("Change Chat Address…") { chat.start() }.keyboardShortcut("l", modifiers: .command)
            }
        }
    }
}

/// what the app does, on the console of a debug build only
func trace(_ s: @autoclosure () -> String) {
    #if DEBUG
    print("[app] " + s()); fflush(stdout)
    #endif
}

struct WebBox: UIViewRepresentable {
    let web: WKWebView
    func makeUIView(context: Context) -> WKWebView { web }
    func updateUIView(_ view: WKWebView, context: Context) {}
}

final class Chat: NSObject, ObservableObject {
    static let key = "chat_url"                    // the name the start page keeps the address under, and Settings.bundle
    let web: WKWebView
    @Published var under = UIColor.systemBackground
    private var watch: [NSKeyValueObservation] = []
    private var shown: String?                     // the address the web view was last sent to
    private var onStart: String?                   // what the start page is told once it is loaded
    private var lastNotice: (at: Date, key: String)?
    private var asked = false
    private var lastBadge = 0
    private var typed = false
    private var saved: [ObjectIdentifier: URL] = [:]   // downloads on their way to the share sheet, each by its own

    private var address: URL? {
        guard let s = UserDefaults.standard.string(forKey: Chat.key)?.trimmingCharacters(in: .whitespaces), !s.isEmpty,
              let u = URL(string: s.contains("://") ? s : "https://" + s), Chat.isWeb(u), u.host != nil else { return nil }
        return u
    }

    static func isWeb(_ u: URL?) -> Bool { ["http", "https"].contains(u?.scheme?.lowercased() ?? "") }

    /// a host of the remembered chat's site: its own, or another chat under the same domain (lab.lagom.game: any *.lagom.game)
    func ofChat(_ host: String?) -> Bool {
        guard let h = host?.lowercased(), let a = address?.host?.lowercased() else { return false }
        let n = a.split(separator: ".")
        let site = n.count > 2 && Int(n.last ?? "") == nil ? n.dropFirst().joined(separator: ".") : a
        return h == a || h == site || h.hasSuffix("." + site)
    }

    override init() {
        let conf = WKWebViewConfiguration()
        conf.allowsInlineMediaPlayback = true
        conf.mediaTypesRequiringUserActionForPlayback = []
        // the page knows by this mark that it is in the app: only then it puts the unread count and its notices into the title
        let ver = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
        conf.userContentController.addUserScript(WKUserScript(source: "window.__acApp = 1; window.__acAppVer = \(Chat.js(ver));",
                                                              injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: .zero, configuration: conf)
        super.init()
        web.navigationDelegate = self
        web.uiDelegate = self
        web.isOpaque = false
        web.backgroundColor = .clear
        watch = [
            web.observe(\.title) { [weak self] w, _ in self?.titled(w.title ?? "") },
            web.observe(\.underPageBackgroundColor, options: [.initial]) { [weak self] w, _ in self?.under = w.underPageBackgroundColor ?? .systemBackground },
        ]
        UNUserNotificationCenter.current().delegate = self
        open()
    }

    static func js(_ s: String) -> String {       // a string as a JavaScript literal
        let d = (try? JSONSerialization.data(withJSONObject: [s])) ?? Data("[\"\"]".utf8)
        return String(String(decoding: d, as: UTF8.self).dropFirst().dropLast())
    }

    /// the remembered chat, or the start page when there is none
    func open() {
        guard let u = address else { return start() }
        shown = u.absoluteString
        trace("open \(u.absoluteString)")
        web.load(URLRequest(url: u))
    }

    /// the start page, with the address that is remembered and, after a failure, what went wrong
    func start(error: String = "") {
        guard let page = Bundle.main.url(forResource: "index", withExtension: "html") else { return }
        shown = nil
        trace("start page\(error.isEmpty ? "" : ": " + error)")
        badge(0)
        let was = UserDefaults.standard.string(forKey: Chat.key) ?? ""
        onStart = "(u => { if (u && \(Chat.js(was))) u.value = \(Chat.js(was)); const e = document.getElementById('err'); if (e) e.textContent = \(Chat.js(error)); })(document.getElementById('u'))"
        web.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent())
    }

    /// the app is in front again: an address changed in Settings meanwhile is opened
    func back() {
        trace("in front; the address in Settings: \(address?.absoluteString ?? "-")")
        if let u = address, shown != nil, u.absoluteString != shown { open() }
    }

    // MARK: the title: the badge and the notices

    private func titled(_ full: String) {
        guard Chat.isWeb(web.url) else { return }
        var title = full
        if let r = title.range(of: " [notify]") {
            let json = Data(title[r.upperBound...].utf8)
            title = String(title[..<r.lowerBound])
            if let v = try? JSONSerialization.jsonObject(with: json) as? [String: Any] {
                notice(String((v["t"] as? String ?? "").prefix(240)), String((v["b"] as? String ?? "").prefix(240)))
            }
        }
        var n = 0
        if title.hasPrefix("("), let end = title.range(of: ") ") {
            let digits = title[title.index(after: title.startIndex)..<end.lowerBound]
            if (1...4).contains(digits.count), digits.allSatisfy({ $0.isASCII && $0.isNumber }) { n = Int(digits) ?? 0 }
        }
        badge(n)
    }

    private func badge(_ n: Int) {
        if n != lastBadge { lastBadge = n; trace("badge \(n)") }
        UNUserNotificationCenter.current().setBadgeCount(n) { _ in }
    }

    private func notice(_ title: String, _ body: String) {
        guard !title.isEmpty || !body.isEmpty else { return }
        // the title is set anew while the notice is in it (the badge repaints): the same notice within 2 s is that one, not another
        let key = title + "\n" + body, now = Date()
        if let l = lastNotice, now.timeIntervalSince(l.at) < 0.25 || (l.key == key && now.timeIntervalSince(l.at) < 2) { return }
        lastNotice = (now, key)
        trace("notice \(title) | \(body)")
        let c = UNMutableNotificationContent()
        c.title = title
        c.body = body
        c.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }

    /// asked once a chat's page is shown, not at the first start: then it is clear what the notifications are of
    private func askNotifications() {
        guard !asked else { return }
        asked = true
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound]) { ok, _ in trace("notifications allowed: \(ok)") }
    }

    private var top: UIViewController? {
        var c = web.window?.rootViewController
        while let p = c?.presentedViewController { c = p }
        return c
    }
}

extension Chat: WKNavigationDelegate {
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, preferences: WKWebpagePreferences,
                 decisionHandler: @escaping (WKNavigationActionPolicy, WKWebpagePreferences) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel, preferences) }
        if action.shouldPerformDownload { return decisionHandler(.download, preferences) }
        if action.targetFrame == nil {              // a link to a new window: the browser, the web view has no tabs
            trace("to the browser: \(url.absoluteString)")
            if Chat.isWeb(url) { UIApplication.shared.open(url) }
            return decisionHandler(.cancel, preferences)
        }
        if action.targetFrame?.isMainFrame == true {
            if !Chat.isWeb(url) && !url.isFileURL && !["about", "blob", "data"].contains(url.scheme ?? "") {
                UIApplication.shared.open(url)      // mailto:, tel: and the like
                return decisionHandler(.cancel, preferences)
            }
            // the start page sends the web view to the address entered: that one is remembered
            if Chat.isWeb(url), webView.url?.isFileURL == true {
                UserDefaults.standard.set(url.absoluteString, forKey: Chat.key)
                shown = url.absoluteString
                trace("remembered \(url.absoluteString)")
            }
        }
        decisionHandler(.allow, preferences)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let attached = ((response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? "").lowercased().hasPrefix("attachment")
        decisionHandler(response.isForMainFrame && (attached || !response.canShowMIMEType) ? .download : .allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        trace("loaded \(webView.url?.isFileURL == true ? "the start page" : webView.url?.absoluteString ?? "-")")
        if webView.url?.isFileURL == true {
            if let js = onStart { onStart = nil; webView.evaluateJavaScript(js) }
            #if DEBUG
            // a test without hands: the address is typed into the start page and its form is sent
            if let t = ProcessInfo.processInfo.environment["LAGOM_TEST_ADDRESS"], typed == false {
                typed = true
                webView.evaluateJavaScript("document.getElementById('u').value = \(Chat.js(t)); document.getElementById('f').requestSubmit()")
            }
            #endif
        } else {
            askNotifications()
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let e = error as NSError
        // a navigation that became a download, or one cut short by the next one, is not a failure
        if e.code == NSURLErrorCancelled || (e.domain == "WebKitErrorDomain" && e.code == 102) { return }
        if webView.url == nil || Chat.isWeb(webView.url) || shown != nil { start(error: e.localizedDescription) }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { open() }   // the system took the page's memory: shown again

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }
}

extension Chat: WKUIDelegate {
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for action: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, Chat.isWeb(url) { UIApplication.shared.open(url) }
        return nil
    }

    // the camera and the microphone: the chat's own page has them once the system's question was answered; a frame of
    // another site inside it, or a site a link took the web view to, is asked about every time (audit: not whatever is shown)
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin, initiatedByFrame frame: WKFrameInfo,
                 type: WKMediaCaptureType, decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        trace("camera / microphone asked by \(origin.host), main frame: \(frame.isMainFrame)")
        decisionHandler(frame.isMainFrame && origin.host == webView.url?.host && ofChat(origin.host) ? .grant : .prompt)
    }
}

extension Chat: WKDownloadDelegate {
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let name = (suggestedFilename as NSString).lastPathComponent
        let file = dir.appendingPathComponent(name.isEmpty || name == "/" ? "download" : name)
        saved[ObjectIdentifier(download)] = file
        trace("download \(file.lastPathComponent)")
        completionHandler(file)
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let file = saved.removeValue(forKey: ObjectIdentifier(download)), let from = top else { return }
        let sheet = UIActivityViewController(activityItems: [file], applicationActivities: nil)
        sheet.popoverPresentationController?.sourceView = web                                  // an iPad shows it as a popover
        sheet.popoverPresentationController?.sourceRect = CGRect(x: web.bounds.midX, y: web.bounds.midY, width: 1, height: 1)
        sheet.completionWithItemsHandler = { _, _, _, _ in try? FileManager.default.removeItem(at: file.deletingLastPathComponent()) }
        from.present(sheet, animated: true)
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let file = saved.removeValue(forKey: ObjectIdentifier(download)) { try? FileManager.default.removeItem(at: file.deletingLastPathComponent()) }
    }
}

extension Chat: UNUserNotificationCenterDelegate {
    // the page asks for a notice only while it is not being read; shown also when the app is in front on another screen
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}
