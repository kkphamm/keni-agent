import AppKit
import SwiftUI

@MainActor
final class ListeningOverlay {
    static let preferenceKey = "showListeningPill"
    private static let size = NSSize(width: 122, height: 46)
    private let voice: VoiceSession
    private var panel: NSPanel?

    init(voice: VoiceSession) {
        self.voice = voice
    }

    func update() {
        guard voice.isTalkHeld, UserDefaults.standard.object(forKey: Self.preferenceKey) as? Bool != false else {
            panel?.orderOut(nil)
            return
        }

        let panel = self.panel ?? makePanel()
        guard !panel.isVisible else { return }
        position(panel)
        panel.orderFrontRegardless()
    }

    func screenDidChange() {
        if let panel, panel.isVisible { position(panel) }
    }

    private func position(_ panel: NSPanel) {
        let pointer = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { $0.frame.contains(pointer) } ?? NSScreen.main
        guard let workArea = screen?.visibleFrame else { return }
        panel.setFrameOrigin(NSPoint(
            x: (workArea.midX - panel.frame.width / 2).rounded(),
            y: workArea.minY + 16,
        ))
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(
            contentRect: NSRect(origin: .zero, size: Self.size),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false,
        )
        panel.title = "Listening controls"
        panel.identifier = NSUserInterfaceItemIdentifier("listening-controls")
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.ignoresMouseEvents = true
        panel.isMovable = false
        panel.isExcludedFromWindowsMenu = true
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.contentView = NSHostingView(rootView: ListeningPill(voice: voice, size: Self.size))
        self.panel = panel
        return panel
    }
}

private struct ListeningPill: View {
    let voice: VoiceSession
    let size: NSSize

    private var isListening: Bool { voice.microphone == .listening }

    var body: some View {
        LiveSoundBars(voice: voice, width: 62, height: 17, barWidthLimit: 2)
        .frame(width: 96, height: 28)
        .background(Color(red: 0.055, green: 0.067, blue: 0.105), in: Capsule())
        .overlay { Capsule().strokeBorder(.white.opacity(0.14), lineWidth: 1) }
        .shadow(color: .black.opacity(0.38), radius: 7, y: 4)
        .frame(width: size.width, height: size.height)
        .preferredColorScheme(.dark)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(isListening ? "Microphone listening" : "Microphone starting")
    }
}

struct LiveSoundBars: View {
    let voice: VoiceSession
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let width: CGFloat
    let height: CGFloat
    var gain: CGFloat = 1
    var barWidthLimit: CGFloat = 4

    var body: some View {
        let activity = reduceMotion ? nil : min(1, gain * CGFloat(voice.microphoneLevel))
        SoundBars(width: width, height: height, activity: activity, barWidthLimit: barWidthLimit)
            .animation(reduceMotion ? nil : .linear(duration: 0.033), value: activity)
    }
}

struct SoundBars: View {
    let width: CGFloat
    let height: CGFloat
    var activity: CGFloat? = nil
    var barWidthLimit: CGFloat = 4

    private static let colors: [Color] = [
        Color(red: 0.34, green: 0.57, blue: 1),
        Color(red: 0.45, green: 0.67, blue: 1),
        Color(red: 0.62, green: 0.79, blue: 1),
        Color(red: 0.82, green: 0.90, blue: 1),
        .white,
        Color(red: 0.91, green: 0.85, blue: 1),
        Color(red: 0.80, green: 0.69, blue: 1),
        Color(red: 0.69, green: 0.54, blue: 1),
        Color(red: 0.60, green: 0.42, blue: 0.96),
    ]
    private static let shape: [CGFloat] = [0.40, 0.61, 0.52, 0.81, 1, 0.78, 0.60, 0.67, 0.42]

    var body: some View {
        let count = max(Self.colors.count, Int((width + 4) / 8))
        let barWidth = min(barWidthLimit, width / CGFloat(2 * count - 1))
        let spacing = (width - CGFloat(count) * barWidth) / CGFloat(count - 1)
        let strength = activity.map { 0.2 + 0.8 * $0 } ?? 0.84
        HStack(spacing: spacing) {
            ForEach(0..<count, id: \.self) { index in
                let color = Int((Double(index) * Double(Self.colors.count - 1) / Double(count - 1)).rounded())
                Capsule()
                    .fill(Self.colors[color])
                    .frame(width: barWidth, height: max(barWidth, height * Self.shape[index % Self.shape.count] * strength))
            }
        }
        .frame(width: width, height: height)
        .accessibilityHidden(true)
    }
}
