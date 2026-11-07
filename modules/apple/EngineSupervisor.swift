#if os(macOS)
import Foundation

// one supervisor per app process, shared by windows and refreshed sessions.
final class EngineSupervisor {
    static let shared = EngineSupervisor(
        runtime: Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/engine"),
        log: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Flowback/engine.log")
    )

    private let runtime: URL
    private let log: URL
    private let port: Int
    private var timer: Timer?
    private var process: Process?
    private var lifetime: FileHandle?
    private var checking = false
    private var stopped = false
    private var failures = 0
    private var nextStart = Date.distantPast
    private(set) var ready = false
    private(set) var error = ""

    init(runtime: URL, log: URL, port: Int = 8891) {
        self.runtime = runtime
        self.log = log
        self.port = port
    }

    func start() {
        guard timer == nil, !stopped else { return }
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.check() }
        check()
    }

    func stop() {
        stopped = true
        timer?.invalidate()
        timer = nil
        // the engine drains its queue after eof, even if the app is force-quit.
        try? lifetime?.close()
        lifetime = nil
    }

    func whenReady(_ completion: @escaping (String) -> Void) {
        start()
        wait(until: Date().addingTimeInterval(20), completion: completion)
    }

    private func wait(until deadline: Date, completion: @escaping (String) -> Void) {
        if ready { completion(""); return }
        if stopped || !error.isEmpty || Date() >= deadline {
            completion(error.isEmpty ? "The audio engine is still starting. Try again in a moment." : error)
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
            guard let self else { completion("The app is closing."); return }
            self.wait(until: deadline, completion: completion)
        }
    }

    private func check() {
        guard !checking, !stopped else { return }
        checking = true
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/health")!)
        request.timeoutInterval = 1
        request.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            let body = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let healthy = (response as? HTTPURLResponse)?.statusCode == 200
                && body?["ok"] as? Bool == true && body?["models_available"] is Bool
            DispatchQueue.main.async {
                guard let self else { return }
                self.checking = false
                guard !self.stopped else { return }
                self.ready = healthy
                if healthy {
                    self.error = ""
                    self.failures = 0
                } else if response != nil {
                    self.error = "Another service is using Flowback’s audio-engine port (\(self.port))."
                } else if self.process == nil && Date() >= self.nextStart {
                    self.launch()
                }
            }
        }.resume()
    }

    private func launch() {
        let python = runtime.appendingPathComponent("python/bin/python3")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            error = "Flowback’s audio engine is missing. Reinstall the complete Mac app."
            return
        }
        do {
            try FileManager.default.createDirectory(at: log.deletingLastPathComponent(), withIntermediateDirectories: true)
            if let size = (try? FileManager.default.attributesOfItem(atPath: log.path)[.size]) as? NSNumber,
               size.intValue > 2 * 1024 * 1024 {
                let previous = log.appendingPathExtension("previous")
                if FileManager.default.fileExists(atPath: previous.path) { try FileManager.default.removeItem(at: previous) }
                try FileManager.default.moveItem(at: log, to: previous)
            }
            if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil) }
            let output = try FileHandle(forWritingTo: log)
            try output.seekToEnd()
            let child = Process()
            let pipe = Pipe()
            child.executableURL = python
            child.arguments = ["-I", "-B", "-m", "cover_engine.managed"]
            child.currentDirectoryURL = runtime
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = runtime.appendingPathComponent("bin").path + ":/usr/bin:/bin:/usr/sbin:/sbin"
            environment["NUMBA_CACHE_DIR"] = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/exact-cover/numba").path
            environment.removeValue(forKey: "PYTHONHOME")
            environment.removeValue(forKey: "PYTHONPATH")
            child.environment = environment
            child.standardInput = pipe
            child.standardOutput = output
            child.standardError = output
            child.terminationHandler = { [weak self] child in
                DispatchQueue.main.async {
                    guard let self, self.process === child else { return }
                    self.process = nil
                    try? self.lifetime?.close()
                    self.lifetime = nil
                    self.ready = false
                    self.failures += 1
                    self.nextStart = Date().addingTimeInterval(min(30, pow(2, Double(min(self.failures, 5)))))
                    self.error = "The audio engine stopped (\(child.terminationStatus)). Retrying automatically. Details: ~/Library/Logs/Flowback/engine.log"
                }
            }
            try child.run()
            try? output.close()
            try? pipe.fileHandleForReading.close()
            process = child
            lifetime = pipe.fileHandleForWriting
            error = ""
        } catch {
            self.error = "Could not start the audio engine: \(error.localizedDescription)"
            nextStart = Date().addingTimeInterval(10)
        }
    }
}
#endif
