import AVFoundation
import Foundation
import LiveKit
import Observation

struct ActionUpdate: Decodable, Equatable, Identifiable {
    enum Action: String, Decodable {
        case spotify, vscode
        case openApp = "open_app"
    }

    enum State: String, Decodable {
        case running, succeeded, failed
    }

    let id: String
    let action: Action
    let state: State
    let message: String
    let historyWarning: String?
}

struct ActionRecord: Decodable, Identifiable {
    let id: String
    let action: ActionUpdate.Action
    let state: ActionUpdate.State
    let startedAt: Date
    let message: String?

    private enum CodingKeys: String, CodingKey {
        case id, action, state, message
        case startedAt = "started_at"
    }
}

struct GmailMessage: Decodable, Identifiable {
    let id: String
    let sender: String
    let subject: String
    let snippet: String
}

private enum Backend {
    enum Method: String {
        case get = "GET"
        case post = "POST"
    }

    struct Health: Decodable {
        let spotifyConfigured: Bool
        let spotifyConnected: Bool
        let spotifyAuthorizationError: String?
        let gmailConfigured: Bool?
        let gmailConnected: Bool?
        let gmailAuthorizationError: String?
    }

    struct Credentials: Decodable {
        let url: URL
        let token: String
    }

    struct Message: Decodable {
        let message: String
    }

    struct Failure: Decodable {
        let detail: String
    }

    struct History: Decodable {
        let actions: [ActionRecord]
    }

    struct Inbox: Decodable {
        let messages: [GmailMessage]
    }

    static let supportDirectory = URL.applicationSupportDirectory.appending(path: "Local Voice Agent")

    static func request<T: Decodable>(
        _ path: String, method: Method, authenticated: Bool = false, queryItems: [URLQueryItem] = [], timeout: TimeInterval = 3
    ) async throws -> T {
        var endpoint = URLComponents(string: "http://127.0.0.1:8765/\(path)")!
        if !queryItems.isEmpty {
            endpoint.queryItems = queryItems
            endpoint.percentEncodedQuery = endpoint.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
        }
        var request = URLRequest(
            url: endpoint.url!,
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: timeout
        )
        request.httpMethod = method.rawValue
        if authenticated {
            let token = (try? String(contentsOf: supportDirectory.appending(path: "desktop-token"), encoding: .utf8))?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard let token, !token.isEmpty else {
                throw SessionFailure("The local agent access token is missing. Restart the app or run ./scripts/start.")
            }
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else {
            let detail = try? JSONDecoder().decode(Failure.self, from: data).detail
            throw SessionFailure(detail ?? "The local agent rejected the request.")
        }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let timestamp = try decoder.singleValueContainer().decode(String.self)
            if let date = try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(timestamp) {
                return date
            }
            return try Date.ISO8601FormatStyle().parse(timestamp)
        }
        return try decoder.decode(T.self, from: data)
    }
}

private struct SessionFailure: LocalizedError {
    let message: String

    init(_ message: String) { self.message = message }

    var errorDescription: String? { message }
}

@MainActor
@Observable
final class VoiceSession: NSObject, RoomDelegate {
    enum Microphone {
        case off, starting, listening, stopping
    }

    enum TalkControl {
        case shortcut, button
    }

    @ObservationIgnored var microphoneDidChange: (() -> Void)?
    private(set) var microphone = Microphone.off {
        didSet {
            if microphone != oldValue { microphoneDidChange?() }
        }
    }
    private(set) var microphoneLevel = 0.0
    private(set) var voiceReady = false
    private(set) var liveSession: Session?
    private(set) var actions: [ActionUpdate] = []
    private(set) var previousMessages: [ReceivedMessage] = []
    private(set) var voiceError: String?
    private(set) var backendError: String?
    private(set) var spotifyMessage: String?
    private(set) var isAuthorizingSpotify = false
    private(set) var gmailMessage: String?
    private(set) var isAuthorizingGmail = false
    var shortcutError: String?

    var serviceOnline: Bool { health != nil }
    var isTalkHeld: Bool { heldControl != nil }
    var spotifyConfigured: Bool { health?.spotifyConfigured == true }
    var spotifyConnected: Bool { health?.spotifyConnected == true }
    var gmailConfigured: Bool { health?.gmailConfigured == true }
    var gmailConnected: Bool { health?.gmailConnected == true }
    private var health: Backend.Health?

    @ObservationIgnored private var backendProcess: Process?
    @ObservationIgnored private var monitor: Task<Void, Never>?
    @ObservationIgnored private var warmup: Task<Void, Never>?
    @ObservationIgnored private var transition: Task<Void, Never>?
    @ObservationIgnored private var disconnecting: Task<Void, Never>?
    @ObservationIgnored private var microphoneMeter: MicrophoneMeter?
    @ObservationIgnored private var meterUpdates: Task<Void, Never>?
    private var heldControl: TalkControl? {
        didSet {
            if isTalkHeld != (oldValue != nil) { microphoneDidChange?() }
        }
    }

    private static let outputOnly = AudioEngineAvailability(isInputAvailable: false, isOutputAvailable: true)

    func monitorService() {
        prepareVoice()
        monitor = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard !Task.isCancelled, let self else { return }
                let wasOnline = serviceOnline
                if await refreshHealth(), !wasOnline, liveSession == nil { prepareVoice() }
                updateVoiceReadiness()
            }
        }
    }

    func setTalkHeld(_ held: Bool, by control: TalkControl) {
        if held {
            guard heldControl == nil else { return }
            heldControl = control
            beginHolding()
        } else {
            guard heldControl == control else { return }
            heldControl = nil
            switch microphone {
            case .starting:
                microphone = transition == nil ? .off : .stopping
            case .listening:
                microphone = .stopping
                do {
                    try AudioManager.shared.setEngineAvailability(Self.outputOnly)
                } catch {
                    disconnect(message: "The voice session was closed to turn off the microphone.")
                    return
                }
                stopMetering()
                transition = Task { await stopMicrophone() }
            case .off, .stopping:
                break
            }
        }
    }

    private func beginHolding() {
        guard heldControl != nil, microphone == .off else { return }
        microphone = .starting
        voiceError = nil
        updateVoiceReadiness()
        if !voiceReady { prepareVoice() }
        startMicrophoneIfReady()
    }

    private func finishStopping() {
        transition = nil
        microphone = .off
        beginHolding()
    }

    func connectSpotify() async throws {
        guard !isAuthorizingSpotify, !spotifyConnected else { return }
        isAuthorizingSpotify = true
        defer { isAuthorizingSpotify = false }
        spotifyMessage = nil
        let result: Backend.Message = try await Backend.request("spotify/authorize", method: .post, authenticated: true)
        spotifyMessage = result.message
    }

    func connectGmail() async {
        guard !isAuthorizingGmail, !gmailConnected else { return }
        isAuthorizingGmail = true
        defer { isAuthorizingGmail = false }
        gmailMessage = nil
        do {
            let result: Backend.Message = try await Backend.request("gmail/authorize", method: .post, authenticated: true)
            gmailMessage = result.message
        } catch {
            gmailMessage = error.localizedDescription
        }
    }

    func loadGmailInbox(query: String) async throws -> [GmailMessage] {
        let items = query.isEmpty ? [] : [URLQueryItem(name: "q", value: query)]
        let result: Backend.Inbox = try await Backend.request(
            "gmail/inbox", method: .get, authenticated: true, queryItems: items, timeout: 20
        )
        return result.messages
    }

    func loadHistory() async throws -> [ActionRecord] {
        let result: Backend.History = try await Backend.request("history", method: .get, authenticated: true)
        return result.actions
    }

    func shutDown() {
        monitor?.cancel()
        disconnect()
        if let backendProcess, backendProcess.isRunning { backendProcess.terminate() }
    }

    private func refreshHealth() async -> Bool {
        let current: Backend.Health? = try? await Backend.request("health", method: .get)
        guard !Task.isCancelled else { return false }
        health = current
        if current != nil { backendError = nil }
        if spotifyConnected {
            spotifyMessage = nil
        } else if let failure = current?.spotifyAuthorizationError {
            spotifyMessage = failure
        }
        if gmailConnected {
            gmailMessage = nil
        } else if let failure = current?.gmailAuthorizationError {
            gmailMessage = failure
        }
        return current != nil
    }

    private func launchBackend() {
        guard backendProcess == nil else { return }
        guard let path = Bundle.main.object(forInfoDictionaryKey: "KeniProjectRoot") as? String else {
            backendError = "Run ./scripts/start from the project folder to set up the local agent."
            return
        }
        let root = URL(fileURLWithPath: path, isDirectory: true)
        let python = root.appending(path: ".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path()) else {
            backendError = "Run ./scripts/start from the project folder to set up the local agent."
            return
        }

        let process = Process()
        process.currentDirectoryURL = root
        process.executableURL = python
        process.arguments = ["-m", "agent.launch"]
        process.terminationHandler = { [weak self] process in
            Task { @MainActor in
                guard let self, self.backendProcess === process else { return }
                self.backendProcess = nil
                self.backendError = "The local agent stopped. See service.log in Library/Application Support/Local Voice Agent."
            }
        }
        do {
            try process.run()
            backendProcess = process
            backendError = nil
        } catch {
            backendError = "Could not start the local agent. Run ./scripts/start from the project folder."
        }
    }

    private func waitForBackend() async throws {
        if await refreshHealth() { return }
        try Task.checkCancellation()
        launchBackend()
        let deadline = ContinuousClock.now + .seconds(30)
        while backendProcess != nil, ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(350))
            if await refreshHealth() { return }
        }
        throw SessionFailure(backendError ?? "The local agent is still starting. Try again in a moment.")
    }

    private func prepareVoice() {
        guard !voiceReady, warmup == nil else { return }
        warmup = Task { await warmVoice() }
    }

    private func updateVoiceReadiness() {
        guard warmup == nil, let liveSession else { return }
        let ready = liveSession.room.connectionState == .connected && liveSession.agent.isConnected
        if ready != voiceReady { voiceReady = ready }
    }

    private func warmVoice() async {
        do {
            await disconnecting?.value
            try Task.checkCancellation()
            disconnecting = nil
            if let liveSession, liveSession.room.connectionState != .reconnecting {
                previousMessages = liveSession.messages
                self.liveSession = nil
                await liveSession.end()
                try Task.checkCancellation()
            }
            if liveSession == nil {
                try await waitForBackend()
                let credentials: Backend.Credentials = try await Backend.request("session", method: .post, authenticated: true)
                try Task.checkCancellation()
                guard credentials.url.scheme == "ws",
                      let host = credentials.url.host(), ["localhost", "127.0.0.1"].contains(host),
                      credentials.url.user() == nil, credentials.url.password() == nil else {
                    throw SessionFailure("The local agent supplied an invalid voice server address.")
                }
                try AudioManager.shared.setEngineAvailability(Self.outputOnly)
                let source = LiteralTokenSource(serverURL: credentials.url, participantToken: credentials.token)
                let room = Room(delegate: self)
                let session = Session(tokenSource: source, options: SessionOptions(room: room, preConnectAudio: false))
                actions = []
                previousMessages = []
                liveSession = session
                // Session.start() automatically publishes the mic after joining.
                try await room.connect(url: credentials.url.absoluteString, token: credentials.token,
                                       connectOptions: ConnectOptions(enableMicrophone: false))
            }

            try Task.checkCancellation()
            guard let session = liveSession else { throw SessionFailure("The voice connection ended. Hold to reconnect.") }
            let deadline = ContinuousClock.now + .seconds(15)
            while session.room.connectionState != .connected || !session.agent.isConnected {
                guard ContinuousClock.now < deadline else {
                    throw SessionFailure("The voice agent did not become ready. Try again.")
                }
                try await Task.sleep(for: .milliseconds(60))
            }
            warmup = nil
            voiceReady = true
            voiceError = nil
            startMicrophoneIfReady()
        } catch {
            guard !Task.isCancelled else { return }
            disconnect(message: error.localizedDescription)
        }
    }

    private func startMicrophoneIfReady() {
        guard voiceReady, microphone == .starting, transition == nil, let room = liveSession?.room else { return }
        transition = Task { await startMicrophone(in: room) }
    }

    private func startMicrophone(in room: Room) async {
        guard microphone == .starting else { finishStopping(); return }
        do {
            let granted = await AVCaptureDevice.requestAccess(for: .audio)
            try Task.checkCancellation()
            guard microphone == .starting else {
                finishStopping()
                return
            }
            guard granted else {
                heldControl = nil
                voiceError = "Allow microphone access in System Settings → Privacy & Security → Microphone."
                finishStopping()
                return
            }

            // Publishing with input disabled avoids waiting for audio frames after an early release.
            guard let track = try await room.localParticipant.setMicrophone(enabled: true) else {
                throw SessionFailure("The microphone could not be published. Try again.")
            }
            try Task.checkCancellation()
            guard microphone == .starting else {
                try await room.localParticipant.unpublish(publication: track)
                try Task.checkCancellation()
                finishStopping()
                return
            }
            try AudioManager.shared.setEngineAvailability(.default)
            transition = nil
            microphone = .listening
            startMetering()
        } catch {
            guard !Task.isCancelled else { return }
            disconnect(message: error.localizedDescription)
        }
    }

    private func startMetering() {
        let meter = MicrophoneMeter()
        microphoneMeter = meter
        AudioManager.shared.add(localAudioRenderer: meter)
        meterUpdates = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(33))
                guard let self, !Task.isCancelled else { return }
                let level = MicrophoneMeter.smooth(microphoneLevel, toward: meter.recentLevel())
                if level != microphoneLevel { microphoneLevel = level }
            }
        }
    }

    private func stopMetering() {
        meterUpdates?.cancel()
        meterUpdates = nil
        if let microphoneMeter {
            AudioManager.shared.remove(localAudioRenderer: microphoneMeter)
        }
        microphoneMeter = nil
        microphoneLevel = 0
    }

    private func stopMicrophone() async {
        guard let room = liveSession?.room else { finishStopping(); return }
        do {
            // Unpublish to stop hardware capture while the agent finishes sending results.
            if let track = room.localParticipant.audioTracks.first(where: { $0.source == .microphone }) as? LocalTrackPublication {
                try await room.localParticipant.unpublish(publication: track)
            }
            try Task.checkCancellation()
            try await room.localParticipant.publish(
                data: Data("release".utf8), options: DataPublishOptions(topic: "keni.release", reliable: true)
            )
            try Task.checkCancellation()
            finishStopping()
        } catch {
            guard !Task.isCancelled else { return }
            disconnect(message: "The voice session was closed to turn off the microphone.")
        }
    }

    private func disconnect(message: String? = nil) {
        heldControl = nil
        voiceReady = false
        let warming = warmup
        warming?.cancel()
        warmup = nil
        let finishing = transition
        finishing?.cancel()
        transition = nil
        try? AudioManager.shared.setEngineAvailability(Self.outputOnly)
        stopMetering()
        if let liveSession {
            previousMessages = liveSession.messages
            disconnecting = Task {
                await liveSession.end()
                await warming?.value
                await finishing?.value
            }
        }
        liveSession = nil
        microphone = .off
        if let message { voiceError = message }
    }

    nonisolated func room(_ room: Room, didDisconnectWithError error: LiveKitError?) {
        Task { @MainActor in
            guard self.liveSession?.room === room else { return }
            self.disconnect(message: self.voiceError ?? error?.localizedDescription ?? "The voice connection ended. Hold to reconnect.")
        }
    }

    nonisolated func room(_ room: Room, participant: RemoteParticipant?, didReceiveData data: Data,
                          forTopic topic: String, encryptionType: EncryptionType) {
        switch topic {
        case "keni.action":
            guard let update = try? JSONDecoder().decode(ActionUpdate.self, from: data) else { return }
            Task { @MainActor in
                guard self.liveSession?.room === room else { return }
                if let index = self.actions.firstIndex(where: { $0.id == update.id }) {
                    if self.actions[index].state == .running { self.actions[index] = update }
                } else {
                    self.actions.append(update)
                }
            }
        case "keni.session":
            guard let result = try? JSONDecoder().decode(Backend.Message.self, from: data) else { return }
            Task { @MainActor in
                guard self.liveSession?.room === room else { return }
                self.disconnect(message: result.message)
            }
        default:
            break
        }
    }
}
