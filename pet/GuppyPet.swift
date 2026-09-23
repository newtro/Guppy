// GuppyPet: Guppy's head, floating on the desktop.
//
// A borderless, transparent, always-on-top panel (every Space, over full-screen apps) hosting the kernel's UI in
// pet mode (http://127.0.0.1:8765/?pet). Drag the head to move it; click to mute/unmute; right-click for the menu.
// No Dock icon. The panel remembers where you left it. If the kernel isn't up yet, it keeps retrying.
import AppKit
import WebKit

let kernelURL = URL(string: "http://127.0.0.1:8765/?pet")!
let sizes: [(String, CGFloat)] = [("Small", 200), ("Medium", 280), ("Large", 380)]

final class Panel: NSPanel {
    override var canBecomeKey: Bool { true }   // needed for the web view to receive audio focus / menus
}

/// Sits on top of the web view and owns the mouse: drag moves the panel, click toggles mute, right-click opens the menu.
final class HandleView: NSView {
    weak var app: AppDelegate?
    private var downAt: NSPoint = .zero
    private var dragged = false

    override func mouseDown(with e: NSEvent) { downAt = NSEvent.mouseLocation; dragged = false }
    override func mouseDragged(with e: NSEvent) {
        guard let w = window else { return }
        let p = NSEvent.mouseLocation
        if !dragged && hypot(p.x - downAt.x, p.y - downAt.y) < 3 { return }
        dragged = true
        w.setFrameOrigin(NSPoint(x: w.frame.origin.x + e.deltaX, y: w.frame.origin.y - e.deltaY))
    }
    override func mouseUp(with e: NSEvent) {
        if dragged { app?.savePosition() } else { app?.toggleMute() }
    }
    override func rightMouseDown(with e: NSEvent) {
        guard let menu = app?.menu() else { return }
        NSMenu.popUpContextMenu(menu, with: e, for: self)
    }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var panel: Panel!
    var web: WKWebView!
    var state = "offline"
    var retry: Timer?
    let defaults = UserDefaults.standard

    func applicationDidFinishLaunching(_ n: Notification) {
        let side = CGFloat(defaults.double(forKey: "size").nonZero ?? 280)
        let origin = defaults.string(forKey: "origin").map(NSPointFromString)
            ?? NSPoint(x: (NSScreen.main?.visibleFrame.maxX ?? 1200) - side - 40, y: 80)
        panel = Panel(contentRect: NSRect(origin: origin, size: NSSize(width: side, height: side * 1.15)),
                      styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.hidesOnDeactivate = false
        panel.isMovable = false  // HandleView moves it

        let cfg = WKWebViewConfiguration()
        cfg.mediaTypesRequiringUserActionForPlayback = []
        cfg.userContentController.add(self, name: "guppy")
        web = WKWebView(frame: panel.contentView!.bounds, configuration: cfg)
        web.autoresizingMask = [.width, .height]
        web.setValue(false, forKey: "drawsBackground")
        web.underPageBackgroundColor = .clear
        web.navigationDelegate = self
        web.uiDelegate = self

        let handle = HandleView(frame: panel.contentView!.bounds)
        handle.autoresizingMask = [.width, .height]
        handle.app = self
        panel.contentView!.addSubview(web)
        panel.contentView!.addSubview(handle)
        panel.orderFrontRegardless()
        load()
    }

    // ---- kernel connection ----
    func load() { web.load(URLRequest(url: kernelURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 5)) }
    func scheduleRetry() {
        retry?.invalidate()
        retry = Timer.scheduledTimer(withTimeInterval: 5, repeats: false) { [weak self] _ in self?.load() }
    }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) { scheduleRetry() }
    func webView(_ w: WKWebView, didFail n: WKNavigation!, withError e: Error) { scheduleRetry() }
    func webViewWebContentProcessDidTerminate(_ w: WKWebView) { scheduleRetry() }

    // Grant the mic to our own kernel page only.
    func webView(_ w: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin, initiatedByFrame f: WKFrameInfo,
                 type: WKMediaCaptureType, decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(origin.host == "127.0.0.1" && origin.port == 8765 ? .grant : .deny)
    }

    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        if let d = m.body as? [String: Any], let s = d["state"] as? String { state = s }
    }

    // ---- actions ----
    func toggleMute() { web.evaluateJavaScript("window.guppyPet && window.guppyPet.toggleMute()") }
    func savePosition() { defaults.set(NSStringFromPoint(panel.frame.origin), forKey: "origin") }
    func setSize(_ side: CGFloat) {
        var f = panel.frame
        f.origin.y += f.height - side * 1.15
        f.size = NSSize(width: side, height: side * 1.15)
        panel.setFrame(f, display: true)
        defaults.set(Double(side), forKey: "size")
        savePosition()
    }

    func menu() -> NSMenu {
        let m = NSMenu()
        m.addItem(withTitle: "Guppy: \(state)", action: nil, keyEquivalent: "")
        m.addItem(.separator())
        m.addItem(item(state == "muted" ? "Unmute" : "Mute", #selector(menuMute)))
        m.addItem(item("Open full view", #selector(menuOpen)))
        let sizeMenu = NSMenu()
        for (i, (name, side)) in sizes.enumerated() {
            let it = item(name, #selector(menuSize(_:))); it.tag = i
            it.state = abs(panel.frame.width - side) < 1 ? .on : .off
            sizeMenu.addItem(it)
        }
        let sizeItem = NSMenuItem(title: "Size", action: nil, keyEquivalent: ""); sizeItem.submenu = sizeMenu
        m.addItem(sizeItem)
        m.addItem(item("Reconnect", #selector(menuReload)))
        m.addItem(.separator())
        m.addItem(item("Quit GuppyPet", #selector(menuQuit)))
        return m
    }
    private func item(_ t: String, _ a: Selector) -> NSMenuItem {
        let i = NSMenuItem(title: t, action: a, keyEquivalent: ""); i.target = self; return i
    }
    @objc func menuMute() { toggleMute() }
    @objc func menuOpen() { NSWorkspace.shared.open(URL(string: "http://127.0.0.1:8765/")!) }
    @objc func menuSize(_ s: NSMenuItem) { setSize(sizes[s.tag].1) }
    @objc func menuReload() { load() }
    @objc func menuQuit() { NSApp.terminate(nil) }
}

extension Double { var nonZero: Double? { self == 0 ? nil : self } }

@main
struct GuppyPetMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)  // no Dock icon
        app.run()
    }
}
