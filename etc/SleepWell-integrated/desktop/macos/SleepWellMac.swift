// SleepWell researcher launcher for macOS.
// Mirrors SleepWell.cs: control window + menu bar item, readiness verification,
// browser window, and graceful shutdown of the server process this launcher started.
import AppKit
import Foundation

let appVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"

struct ReadyMetadata {
    let pid: Int32
    let instanceId: String
    let url: URL
}

enum LauncherContract {
    static func defaultDataDirectory() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("SleepWell", isDirectory: true)
    }

    static func parseReady(_ data: Data, expectedPid: Int32) -> ReadyMetadata? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["app"] as? String == "SleepWell",
              let pid = (object["pid"] as? NSNumber)?.int32Value, pid == expectedPid,
              let instance = object["instance_id"] as? String, instance.count >= 16,
              let text = object["url"] as? String, let url = URL(string: text),
              isLoopbackRoot(url) else { return nil }
        return ReadyMetadata(pid: pid, instanceId: instance, url: url)
    }

    static func isLoopbackRoot(_ url: URL) -> Bool {
        guard url.scheme == "http", url.host == "127.0.0.1", let port = url.port,
              port > 0, port < 65536, url.path == "/", url.query == nil else { return false }
        return true
    }

    static func applicationURL(_ root: URL, researchLab: Bool) -> URL {
        researchLab ? root.appendingPathComponent("research-lab") : root
    }

    static func health(_ ready: ReadyMetadata, timeout: TimeInterval = 3) -> Bool {
        var request = URLRequest(url: ready.url.appendingPathComponent("api/desktop-health"))
        request.timeoutInterval = timeout
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let done = DispatchSemaphore(value: 0)
        var matched = false
        let task = URLSession.shared.dataTask(with: request) { data, response, _ in
            defer { done.signal() }
            guard (response as? HTTPURLResponse)?.statusCode == 200, let data = data,
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            matched = object["instance_id"] as? String == ready.instanceId
                && (object["pid"] as? NSNumber)?.int32Value == ready.pid
        }
        task.resume()
        if done.wait(timeout: .now() + timeout + 1) == .timedOut { task.cancel() }
        return matched
    }

    static func selfTest() -> Int32 {
        let good = "{\"app\":\"SleepWell\",\"pid\":42,\"instance_id\":\"0123456789abcdef0123\",\"url\":\"http://127.0.0.1:54321/\"}"
        guard let ready = parseReady(Data(good.utf8), expectedPid: 42) else { return 10 }
        if parseReady(Data(good.utf8), expectedPid: 43) != nil { return 11 }
        let remote = good.replacingOccurrences(of: "127.0.0.1", with: "192.168.0.2")
        if parseReady(Data(remote.utf8), expectedPid: 42) != nil { return 12 }
        let path = good.replacingOccurrences(of: "54321/", with: "54321/x")
        if parseReady(Data(path.utf8), expectedPid: 42) != nil { return 13 }
        if applicationURL(ready.url, researchLab: true).absoluteString != "http://127.0.0.1:54321/research-lab" { return 14 }
        let options = Options.parse(["SleepWell", "--research-lab", "--data-dir", "/tmp/Sleep Well"])
        if !options.researchLab || options.dataDirectory?.path != "/tmp/Sleep Well" { return 15 }
        guard let resources = Bundle.main.resourceURL else { return 20 }
        for item in ["runtime/bin/python3", "app/mac_host.py", "app/desktop_host.py", "app/SyncCertificate", "app/web-app/server.py"] {
            if !FileManager.default.fileExists(atPath: resources.appendingPathComponent(item).path) { return 21 }
        }
        return 0
    }
}

struct Options {
    var researchLab = false
    var dataDirectory: URL?
    var selfTest = false
    var smokeTest = false

    static func parse(_ arguments: [String]) -> Options {
        var options = Options()
        var index = 1
        while index < arguments.count {
            switch arguments[index] {
            case "--research-lab": options.researchLab = true
            case "--self-test": options.selfTest = true
            case "--smoke-test": options.smokeTest = true
            case "--data-dir" where index + 1 < arguments.count:
                index += 1
                options.dataDirectory = URL(fileURLWithPath: arguments[index], isDirectory: true)
            default: break  // Finder may pass -psn_* and similar arguments.
            }
            index += 1
        }
        return options
    }
}

final class HostProcess {
    let dataDirectory: URL
    let readyFile: URL
    private(set) var child: Process?
    private var control: Pipe?
    private(set) var ready: ReadyMetadata?
    var onUnexpectedExit: ((Int32) -> Void)?
    private var stopping = false

    init(dataDirectory: URL) {
        self.dataDirectory = dataDirectory
        readyFile = dataDirectory.appendingPathComponent("desktop-ready.json")
    }

    var isRunning: Bool { child?.isRunning == true && ready != nil }

    func start() throws {
        guard let resources = Bundle.main.resourceURL else { throw LaunchError.missingResources }
        let python = resources.appendingPathComponent("runtime/bin/python3")
        let script = resources.appendingPathComponent("app/mac_host.py")
        guard FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: script.path) else { throw LaunchError.missingResources }
        try FileManager.default.createDirectory(at: dataDirectory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let process = Process()
        process.executableURL = python
        process.arguments = ["-I", "-B", "-X", "utf8", script.path,
                             "--data-dir", dataDirectory.path, "--ready-file", readyFile.path]
        process.currentDirectoryURL = dataDirectory
        var environment = ProcessInfo.processInfo.environment.filter { !$0.key.hasPrefix("PYTHON") }
        environment["LANG"] = "ko_KR.UTF-8"
        process.environment = environment
        let pipe = Pipe()
        process.standardInput = pipe
        process.standardOutput = FileHandle.nullDevice
        let log = dataDirectory.appendingPathComponent("desktop-host.log")
        if !FileManager.default.fileExists(atPath: log.path) {
            FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        if let handle = try? FileHandle(forWritingTo: log) {
            handle.seekToEndOfFile()
            process.standardError = handle
        } else {
            process.standardError = FileHandle.nullDevice
        }
        process.terminationHandler = { [weak self] finished in
            guard let self = self else { return }
            let status = finished.terminationStatus
            DispatchQueue.main.async {
                self.ready = nil
                if !self.stopping { self.onUnexpectedExit?(status) }
            }
        }
        try process.run()
        child = process
        control = pipe
    }

    /// Blocks the calling (background) thread until the host is verified or fails.
    func waitUntilReady(timeout: TimeInterval) -> ReadyMetadata? {
        guard let process = child else { return nil }
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline && process.isRunning {
            if let data = try? Data(contentsOf: readyFile),
               let candidate = LauncherContract.parseReady(data, expectedPid: process.processIdentifier),
               LauncherContract.health(candidate) {
                ready = candidate
                return candidate
            }
            Thread.sleep(forTimeInterval: 0.2)
        }
        return nil
    }

    /// Graceful shutdown of the process this launcher created; never looks up other processes.
    func stop(timeout: TimeInterval = 10) {
        stopping = true
        guard let process = child else { return }
        if process.isRunning, let pipe = control {
            try? pipe.fileHandleForWriting.write(contentsOf: Data("shutdown\n".utf8))
            try? pipe.fileHandleForWriting.close()
        }
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
        if process.isRunning {
            process.terminate()
            let hard = Date().addingTimeInterval(3)
            while process.isRunning && Date() < hard { Thread.sleep(forTimeInterval: 0.1) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        }
        process.waitUntilExit()
        ready = nil
        control = nil
    }
}

enum LaunchError: Error { case missingResources }

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let options: Options
    let host: HostProcess
    var window: NSWindow!
    var statusLabel: NSTextField!
    var addressLabel: NSTextField!
    var buttons: [NSButton] = []
    var statusItem: NSStatusItem?
    var activity: NSObjectProtocol?
    var terminating = false

    init(options: Options) {
        self.options = options
        host = HostProcess(dataDirectory: options.dataDirectory ?? LauncherContract.defaultDataDirectory())
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
            .filter { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }
        if let existing = others.first {
            existing.activate(options: [])
            NSApp.terminate(nil)
            return
        }
        buildMenu()
        buildWindow()
        buildStatusItem()
        // Keep the 20:00 forecast capture timer from being throttled while hidden.
        activity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiatedAllowingIdleSystemSleep], reason: "SleepWell 20시 사전 예측 저장")
        host.onUnexpectedExit = { [weak self] status in self?.hostExited(status) }
        startHost()
    }

    func startHost() {
        setStatus("연구 엔진을 시작하는 중입니다… (처음 실행은 1분 정도 걸릴 수 있습니다)", address: "")
        setButtons(enabled: false)
        do { try host.start() } catch {
            fail("실행 파일 구성이 손상되었습니다. SleepWell을 다시 설치해주세요.")
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let ready = self.host.waitUntilReady(timeout: 120)
            DispatchQueue.main.async {
                guard !self.terminating else { return }
                guard let ready = ready else {
                    self.host.stop(timeout: 3)
                    self.fail("연구 엔진을 시작하지 못했습니다. 다른 SleepWell이 같은 기록 폴더를 사용 중인지 확인하세요.")
                    return
                }
                self.setStatus("실행 중입니다. 창을 닫아도 메뉴 막대에서 계속 실행됩니다.",
                               address: ready.url.absoluteString)
                self.setButtons(enabled: true)
                self.openScreen(researchLab: self.options.researchLab)
            }
        }
    }

    func hostExited(_ status: Int32) {
        guard !terminating else { return }
        setButtons(enabled: false)
        fail("연구 엔진이 종료되었습니다 (코드 \(status)). SleepWell을 다시 실행해주세요.")
    }

    func fail(_ message: String) {
        setStatus(message, address: "")
        showWindow()
        let alert = NSAlert()
        alert.messageText = "SleepWell"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "기록 폴더 열기")
        alert.addButton(withTitle: "종료")
        if alert.runModal() == .alertFirstButtonReturn { openDataFolder() }
        NSApp.terminate(nil)
    }

    // MARK: actions

    @objc func openScreenAction() { openScreen(researchLab: false) }
    @objc func openResearchLabAction() { openScreen(researchLab: true) }

    func openScreen(researchLab: Bool) {
        guard let ready = host.ready else { return }
        NSWorkspace.shared.open(LauncherContract.applicationURL(ready.url, researchLab: researchLab))
    }

    @objc func openDataFolder() {
        try? FileManager.default.createDirectory(at: host.dataDirectory, withIntermediateDirectories: true)
        NSWorkspace.shared.open(host.dataDirectory)
    }

    @objc func openGuide() {
        if let guide = Bundle.main.url(forResource: "사용 안내", withExtension: "md", subdirectory: "docs") {
            NSWorkspace.shared.open(guide)
        } else if let docs = Bundle.main.resourceURL?.appendingPathComponent("docs") {
            NSWorkspace.shared.open(docs)
        }
    }

    @objc func showWindow() {
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    @objc func quit() { NSApp.terminate(nil) }

    // MARK: lifecycle

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminating { return .terminateLater }
        terminating = true
        guard host.child != nil else { return .terminateNow }
        setStatus("연구 엔진을 안전하게 종료하는 중입니다…", address: "")
        setButtons(enabled: false)
        DispatchQueue.global(qos: .userInitiated).async {
            self.host.stop()
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)  // Hide like the Windows control window; 종료 stops the server.
        return false
    }

    // MARK: UI

    func setStatus(_ text: String, address: String) {
        statusLabel?.stringValue = text
        addressLabel?.stringValue = address
    }

    func setButtons(enabled: Bool) {
        for button in buttons where button.tag == 1 { button.isEnabled = enabled }
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "제어창 보기", action: #selector(showWindow), keyEquivalent: "0")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "SleepWell 숨기기", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "SleepWell 종료", action: #selector(quit), keyEquivalent: "q")
        appItem.submenu = appMenu

        let fileItem = NSMenuItem()
        main.addItem(fileItem)
        let fileMenu = NSMenu(title: "열기")
        fileMenu.addItem(withTitle: "연구 화면 열기", action: #selector(openScreenAction), keyEquivalent: "o")
        fileMenu.addItem(withTitle: "연구 분석실 열기", action: #selector(openResearchLabAction), keyEquivalent: "l")
        fileMenu.addItem(withTitle: "기록 폴더 열기", action: #selector(openDataFolder), keyEquivalent: "")
        fileMenu.addItem(withTitle: "사용 안내", action: #selector(openGuide), keyEquivalent: "")
        fileItem.submenu = fileMenu

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let editMenu = NSMenu(title: "편집")
        editMenu.addItem(withTitle: "복사", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "전체 선택", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu

        let windowItem = NSMenuItem()
        main.addItem(windowItem)
        let windowMenu = NSMenu(title: "윈도우")
        windowMenu.addItem(withTitle: "닫기", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowMenu.addItem(withTitle: "최소화", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowItem.submenu = windowMenu
        NSApp.windowsMenu = windowMenu
        NSApp.mainMenu = main
    }

    func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 460, height: 250),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "SleepWell 연구자용 \(appVersion)"
        window.isReleasedWhenClosed = false
        window.delegate = self

        let title = NSTextField(labelWithString: "SleepWell 연구자용")
        title.font = .boldSystemFont(ofSize: 18)
        statusLabel = NSTextField(wrappingLabelWithString: "")
        statusLabel.preferredMaxLayoutWidth = 420
        addressLabel = NSTextField(labelWithString: "")
        addressLabel.textColor = .secondaryLabelColor
        addressLabel.isSelectable = true
        let note = NSTextField(wrappingLabelWithString:
            "20시 사전 예측은 Mac이 깨어 있고 SleepWell이 실행 중일 때만 저장됩니다. 완전히 끄려면 ‘종료’를 누르세요.")
        note.font = .systemFont(ofSize: 11)
        note.textColor = .secondaryLabelColor
        note.preferredMaxLayoutWidth = 420

        func button(_ label: String, _ action: Selector, needsHost: Bool) -> NSButton {
            let item = NSButton(title: label, target: self, action: action)
            item.bezelStyle = .rounded
            item.tag = needsHost ? 1 : 0
            buttons.append(item)
            return item
        }
        let row = NSStackView(views: [
            button("화면 열기", #selector(openScreenAction), needsHost: true),
            button("연구 분석실", #selector(openResearchLabAction), needsHost: true),
            button("기록 폴더 열기", #selector(openDataFolder), needsHost: false),
            button("종료", #selector(quit), needsHost: false),
        ])
        row.orientation = .horizontal
        row.spacing = 8

        let stack = NSStackView(views: [title, statusLabel, addressLabel, row, note])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        stack.translatesAutoresizingMaskIntoConstraints = false
        let content = NSView()
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor),
        ])
        window.contentView = content
        window.center()
        showWindow()
    }

    func buildStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let image = NSImage(systemSymbolName: "moon.zzz", accessibilityDescription: "SleepWell") {
            image.isTemplate = true
            item.button?.image = image
        } else {
            item.button?.title = "SleepWell"
        }
        let menu = NSMenu()
        menu.addItem(withTitle: "연구 화면 열기", action: #selector(openScreenAction), keyEquivalent: "")
        menu.addItem(withTitle: "연구 분석실 열기", action: #selector(openResearchLabAction), keyEquivalent: "")
        menu.addItem(withTitle: "기록 폴더 열기", action: #selector(openDataFolder), keyEquivalent: "")
        menu.addItem(withTitle: "제어창 보기", action: #selector(showWindow), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "종료", action: #selector(quit), keyEquivalent: "")
        for entry in menu.items { entry.target = self }
        item.menu = menu
        statusItem = item
    }
}

/// Headless check: start against an isolated data folder, verify endpoints, stop cleanly.
func smokeTest(_ options: Options) -> Int32 {
    guard let directory = options.dataDirectory, directory.path.hasPrefix("/"),
          directory.standardizedFileURL != LauncherContract.defaultDataDirectory().standardizedFileURL else {
        FileHandle.standardError.write(Data("--smoke-test requires an absolute, non-default --data-dir\n".utf8))
        return 2
    }
    let host = HostProcess(dataDirectory: directory)
    do { try host.start() } catch { return 3 }
    guard let ready = host.waitUntilReady(timeout: 120) else { host.stop(timeout: 3); return 4 }
    func status(_ path: String) -> Int {
        var request = URLRequest(url: URL(string: path, relativeTo: ready.url)!)
        request.timeoutInterval = 30
        let done = DispatchSemaphore(value: 0)
        var code = -1
        let session = URLSession(configuration: .ephemeral, delegate: NoRedirect(), delegateQueue: nil)
        session.dataTask(with: request) { _, response, _ in
            code = (response as? HTTPURLResponse)?.statusCode ?? -1
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 31)
        return code
    }
    let results = ["/": status("/"), "/research-lab": status("/research-lab"),
                   "/api/desktop-health": status("/api/desktop-health")]
    let started = Date()
    host.stop()
    let stopSeconds = Date().timeIntervalSince(started)
    let readyRemoved = !FileManager.default.fileExists(atPath: host.readyFile.path)
    print("{\"root\":\(results["/"]!),\"research_lab\":\(results["/research-lab"]!),\"health\":\(results["/api/desktop-health"]!),"
        + "\"stop_seconds\":\(String(format: "%.2f", stopSeconds)),\"exit\":\(host.child?.terminationStatus ?? -1),"
        + "\"ready_removed\":\(readyRemoved)}")
    let ok = results["/"] == 200 && results["/research-lab"] == 302 && results["/api/desktop-health"] == 200
        && host.child?.terminationStatus == 0 && stopSeconds < 10 && readyRemoved
    return ok ? 0 : 5
}

final class NoRedirect: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

let options = Options.parse(CommandLine.arguments)
if options.selfTest { exit(LauncherContract.selfTest()) }
if options.smokeTest { exit(smokeTest(options)) }

let application = NSApplication.shared
let delegate = AppDelegate(options: options)
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
