import AppKit
import LiveKit
import SwiftUI

@main
struct LocalVoiceAgent: App {
    @NSApplicationDelegateAdaptor(DesktopDelegate.self) private var delegate

    var body: some Scene {
        Window("Local Voice Agent", id: "voice-agent") {
            VoiceWindow(voice: delegate.voice)
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 660, height: 518)
        .windowResizability(.contentMinSize)
    }
}

@MainActor
final class DesktopDelegate: NSObject, NSApplicationDelegate {
    let voice = VoiceSession()
    private var hotKey: MicHotKey?
    private lazy var listeningOverlay = ListeningOverlay(voice: voice)

    func applicationDidFinishLaunching(_ notification: Notification) {
        voice.microphoneDidChange = { [weak self] in self?.listeningOverlay.update() }
        let hotKey = MicHotKey { [weak voice] held in voice?.setTalkHeld(held, by: .shortcut) }
        let status = hotKey.register()
        if status == noErr {
            self.hotKey = hotKey
        } else {
            voice.shortcutError = "⌘⇧Space is unavailable. Hold the talk button or free the shortcut in System Settings."
        }
        voice.monitorService()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationDidChangeScreenParameters(_ notification: Notification) {
        listeningOverlay.screenDidChange()
    }

    func applicationWillTerminate(_ notification: Notification) {
        hotKey?.unregister()
        voice.shutDown()
    }
}

private struct VoiceWindow: View {
    private enum Page {
        case assistant, history
    }

    let voice: VoiceSession
    @State private var page: Page = .assistant
    @State private var isHoveringMic = false
    @State private var spotifyError: String?
    @State private var showingGmailInbox = false
    @FocusState private var isMicFocused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let background = Color(red: 0.063, green: 0.078, blue: 0.10)
    private let panel = Color(red: 0.046, green: 0.059, blue: 0.080)
    private let raised = Color(red: 0.106, green: 0.122, blue: 0.15)
    private let accent = Color(red: 0.36, green: 0.87, blue: 0.62)

    var body: some View {
        VStack(spacing: 0) {
            header
                .padding(.horizontal, 24)
                .padding(.top, 38)
                .padding(.bottom, 15)

            Picker("View", selection: $page) {
                Text("Assistant").tag(Page.assistant)
                Text("History").tag(Page.history)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 176)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 24)
            .padding(.bottom, 14)

            switch page {
            case .assistant:
                assistantContent
            case .history:
                HistoryView(voice: voice, accent: accent)
            }

            Divider().padding(.horizontal, 24)
            VStack(spacing: 8) {
                spotifyBar
                gmailBar
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 12)
        }
        .frame(minWidth: 470, minHeight: 410)
        .background(background.ignoresSafeArea())
        .preferredColorScheme(.dark)
        .alert("Spotify sign-in didn’t open", isPresented: Binding(
            get: { spotifyError != nil },
            set: { if !$0 { spotifyError = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(spotifyError ?? "")
        }
        .sheet(isPresented: $showingGmailInbox) {
            GmailInbox(voice: voice)
        }
    }

    private var assistantContent: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                microphonePanel
                if let error = voice.voiceError ?? voice.backendError {
                    feedback(error, icon: "exclamationmark.circle", color: .orange)
                }
                if let error = voice.shortcutError {
                    feedback(error, icon: "keyboard", color: .secondary)
                }
                if let session = voice.liveSession {
                    LiveTranscript(session: session)
                } else {
                    Transcript(messages: voice.previousMessages)
                }
                if !voice.actions.isEmpty { actionResults }
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 18)
        }
        .defaultScrollAnchor(.top)
    }

    private var header: some View {
        HStack(spacing: 10) {
            SoundBars(width: 24, height: 22)
            Text("Local Voice Agent")
                .font(.system(size: 18, weight: .semibold))
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: 12)
            HStack(spacing: 7) {
                Circle()
                    .fill(voice.serviceOnline ? accent : Color.secondary)
                    .frame(width: 7, height: 7)
                    .accessibilityHidden(true)
                Text(voice.serviceOnline ? "Service ready" : "Service offline")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
        .lineLimit(1)
        .accessibilityElement(children: .contain)
    }

    private var microphonePanel: some View {
        VStack(spacing: 14) {
            microphoneMeter

            VStack(spacing: 6) {
                Text(microphoneHeading)
                    .font(.system(size: 22, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Text(microphoneHint)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity, alignment: .center)

            Button(action: {}) {
                ZStack {
                    Text(microphoneButton)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 65)
                    HStack {
                        Image(systemName: voice.isTalkHeld ? "mic.slash" : "mic")
                        Spacer()
                        if voice.shortcutError == nil {
                            Text("⌘⇧Space")
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                    }
                    .accessibilityHidden(true)
                }
                .font(.system(size: 13, weight: .semibold))
                .padding(.horizontal, 16)
                .frame(minHeight: 40)
                .background(isHoveringMic ? raised.opacity(0.76) : raised, in: Capsule())
                .overlay { Capsule().strokeBorder(isMicFocused ? accent : Color.clear, lineWidth: 2) }
                .contentShape(Capsule())
            }
            .buttonStyle(HoldToTalkStyle { voice.setTalkHeld($0, by: .button) })
            .focused($isMicFocused)
            .onHover { isHoveringMic = $0 }
            .accessibilityLabel(microphoneButton)
            .accessibilityHint(voice.shortcutError == nil ? "Hold Command Shift Space to speak, then release" : "Hold to speak, then release")
        }
        .padding(22)
        .frame(maxWidth: .infinity)
        .background(panel, in: RoundedRectangle(cornerRadius: 18))
    }

    private var microphoneMeter: some View {
        GeometryReader { geometry in
            let width = max(1, geometry.size.width - 40)
            ZStack {
                if voice.isTalkHeld {
                    LiveSoundBars(voice: voice, width: width, height: 46, gain: 1.45)
                } else {
                    Image(systemName: "mic")
                        .font(.system(size: 24, weight: .medium))
                }
            }
            .frame(width: voice.isTalkHeld ? geometry.size.width : 70, height: 70)
            .background(raised, in: Capsule())
            .clipShape(Capsule())
            .frame(maxWidth: .infinity)
            .animation(reduceMotion ? nil : .easeOut(duration: 0.20), value: voice.isTalkHeld)
        }
        .frame(height: 70)
        .accessibilityHidden(true)
    }

    private var microphoneHeading: String {
        switch voice.microphone {
        case .off where voice.voiceError != nil || voice.backendError != nil: "Voice unavailable"
        case .off, .starting: voice.voiceReady ? "Ready to listen" : "Connecting voice…"
        case .listening: "Listening"
        case .stopping: "Microphone off"
        }
    }

    private var microphoneHint: String {
        switch voice.microphone {
        case .off: voice.voiceReady ? "Hold to speak, then release" : "The microphone is off"
        case .starting: "Keep holding"
        case .listening: "Release when you’re done speaking"
        case .stopping: voice.isTalkHeld ? "Keep holding" : "Sending your turn"
        }
    }

    private var microphoneButton: String {
        switch voice.microphone {
        case .off: "Hold to talk"
        case .starting: "Release to cancel"
        case .listening: "Release to stop"
        case .stopping: voice.isTalkHeld ? "Release to cancel" : "Hold to talk"
        }
    }

    private var actionResults: some View {
        VStack(alignment: .leading, spacing: 9) {
            SectionLabel(title: "Task results")
            ForEach(voice.actions) { update in
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: update.action.icon)
                        .frame(width: 20)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 5) {
                        Text(update.action.title)
                            .font(.system(size: 14, weight: .medium))
                        Text(update.message)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                        if let warning = update.historyWarning {
                            Label(warning, systemImage: "exclamationmark.triangle")
                                .foregroundStyle(.orange)
                        }
                    }
                    Spacer(minLength: 8)
                    ActionStatus(state: update.state, accent: accent)
                }
                .font(.subheadline)
                .padding(.vertical, 7)
                .accessibilityElement(children: .combine)
                Divider()
            }
        }
    }

    private var spotifyBar: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Label("Spotify", systemImage: "music.note")
                    .font(.subheadline.weight(.medium))
                Spacer()
                if voice.spotifyConnected {
                    Label("Connected", systemImage: "checkmark.circle")
                        .font(.subheadline)
                        .foregroundStyle(accent)
                } else if voice.spotifyConfigured {
                    Button(voice.isAuthorizingSpotify ? "Opening Spotify…" : "Connect Spotify") {
                        Task {
                            do {
                                try await voice.connectSpotify()
                            } catch {
                                spotifyError = error.localizedDescription
                            }
                        }
                    }
                    .disabled(voice.isAuthorizingSpotify)
                } else {
                    Text(voice.serviceOnline ? "Setup required" : "Service offline")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
            if let message = voice.spotifyMessage {
                Text(message).font(.subheadline).foregroundStyle(.secondary)
            }
        }
    }

    private var gmailBar: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Label("Gmail", systemImage: "envelope")
                    .font(.subheadline.weight(.medium))
                Spacer()
                if voice.gmailConnected {
                    Label("Connected", systemImage: "checkmark.circle")
                        .font(.subheadline)
                        .foregroundStyle(accent)
                    Button("Inbox") { showingGmailInbox = true }
                        .help("View your Gmail inbox")
                } else if voice.gmailConfigured {
                    Button(voice.isAuthorizingGmail ? "Opening Gmail…" : "Connect Gmail") {
                        Task { await voice.connectGmail() }
                    }
                    .disabled(voice.isAuthorizingGmail)
                } else {
                    Text(voice.serviceOnline ? "Setup required" : "Service offline")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .help("Save gmail-client.json in Local Voice Agent’s Application Support folder")
                }
            }
            if let message = voice.gmailMessage {
                Text(message).font(.subheadline).foregroundStyle(.secondary)
            }
        }
    }

    private func feedback(_ message: String, icon: String, color: Color) -> some View {
        Label(message, systemImage: icon)
            .font(.subheadline)
            .foregroundStyle(color)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

extension ActionUpdate.Action {
    var title: String {
        switch self {
        case .spotify: "Spotify playback"
        case .vscode: "Open IRDX"
        case .openApp: "Open app"
        }
    }

    var icon: String {
        switch self {
        case .spotify: "music.note"
        case .vscode: "chevron.left.forwardslash.chevron.right"
        case .openApp: "macwindow"
        }
    }
}

struct ActionStatus: View {
    let state: ActionUpdate.State
    let accent: Color

    var body: some View {
        switch state {
        case .running:
            Label("Running", systemImage: "clock").foregroundStyle(.orange)
        case .succeeded:
            Label("Completed", systemImage: "checkmark.circle").foregroundStyle(accent)
        case .failed:
            Label("Failed", systemImage: "xmark.circle").foregroundStyle(.red)
        }
    }
}

private struct HoldToTalkStyle: ButtonStyle {
    let onHoldChanged: (Bool) -> Void
    @GestureState private var isPointerHeld = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .simultaneousGesture(
                DragGesture(minimumDistance: 0)
                    .updating($isPointerHeld) { _, held, _ in held = true }
            )
            .onChange(of: configuration.isPressed || isPointerHeld) { _, held in onHoldChanged(held) }
            .onDisappear { onHoldChanged(false) }
    }
}

private struct LiveTranscript: View {
    @ObservedObject var session: Session

    var body: some View { Transcript(messages: session.messages) }
}

private struct Transcript: View {
    let messages: [ReceivedMessage]

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            SectionLabel(title: messages.isEmpty ? "Example request" : "Transcript")
            if messages.isEmpty {
                Text("“Play Dynamite by BTS and open IRDX in VS Code.”")
                    .font(.system(size: 15))
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background {
                        RoundedRectangle(cornerRadius: 12).strokeBorder(Color.primary.opacity(0.12))
                    }
            } else {
                ForEach(messages) { message in
                    switch message.content {
                    case let .agentTranscript(text):
                        line("Agent", text: text)
                    case let .userTranscript(text), let .userInput(text):
                        line("You", text: text)
                    }
                }
            }
        }
    }

    private func line(_ speaker: String, text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(speaker)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 40, alignment: .leading)
            Text(text)
                .font(.system(size: 15))
                .textSelection(.enabled)
        }
        .accessibilityElement(children: .combine)
    }
}

struct SectionLabel: View {
    let title: String

    var body: some View {
        Text(title)
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(.secondary)
            .accessibilityAddTraits(.isHeader)
    }
}
