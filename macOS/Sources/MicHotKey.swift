import AppKit
import Carbon.HIToolbox

@MainActor
final class MicHotKey {
    private static let identity = EventHotKeyID(signature: 0x4B454E49, id: 1) // KENI
    private let onHoldChanged: @MainActor (Bool) -> Void
    private var handler: EventHandlerRef?
    private var registration: EventHotKeyRef?
    private var releaseWatch: Task<Void, Never>?
    private var isHeld = false

    init(onHoldChanged: @escaping @MainActor (Bool) -> Void) {
        self.onHoldChanged = onHoldChanged
    }

    func register() -> OSStatus {
        let events = [kEventHotKeyPressed, kEventHotKeyReleased].map {
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32($0))
        }
        let target = GetApplicationEventTarget()
        let result = InstallEventHandler(target, { _, event, context in
            guard let event, let context else { return OSStatus(eventNotHandledErr) }
            var key = EventHotKeyID()
            let result = GetEventParameter(event, OSType(kEventParamDirectObject), OSType(typeEventHotKeyID),
                                           nil, MemoryLayout<EventHotKeyID>.size, nil, &key)
            guard result == noErr else { return OSStatus(eventNotHandledErr) }
            let hotKey = Unmanaged<MicHotKey>.fromOpaque(context).takeUnretainedValue()
            return MainActor.assumeIsolated {
                hotKey.handle(key, pressed: GetEventKind(event) == UInt32(kEventHotKeyPressed))
            }
        }, events.count, events, Unmanaged.passUnretained(self).toOpaque(), &handler)
        guard result == noErr else { return result }

        let status = RegisterEventHotKey(UInt32(kVK_Space), UInt32(cmdKey | shiftKey),
                                        Self.identity, target, OptionBits(kEventHotKeyExclusive), &registration)
        if status != noErr { unregister() }
        return status
    }

    func unregister() {
        if let registration { UnregisterEventHotKey(registration) }
        if let handler { RemoveEventHandler(handler) }
        registration = nil
        handler = nil
        finishPress()
    }

    private func handle(_ key: EventHotKeyID, pressed: Bool) -> OSStatus {
        guard key.signature == Self.identity.signature, key.id == Self.identity.id else {
            return OSStatus(eventNotHandledErr)
        }
        if pressed {
            guard releaseWatch == nil else { return noErr }
            watchForRelease()
            setHeld(true)
        } else if Self.isSpaceDown {
            setHeld(false)
        } else {
            finishPress()
        }
        return noErr
    }

    private static var isSpaceDown: Bool {
        CGEventSource.keyState(.combinedSessionState, key: CGKeyCode(kVK_Space))
    }

    private func setHeld(_ held: Bool) {
        guard held != isHeld else { return }
        isHeld = held
        onHoldChanged(held)
    }

    private func finishPress() {
        releaseWatch?.cancel()
        releaseWatch = nil
        setHeld(false)
    }

    private func watchForRelease() {
        // A modifier can lift first, so keep blocking repeats until Space is up.
        releaseWatch = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(25))
                guard let self, !Task.isCancelled else { return }
                guard Self.isSpaceDown else {
                    finishPress()
                    return
                }
                if !NSEvent.modifierFlags.contains([.command, .shift]) {
                    setHeld(false)
                }
            }
        }
    }
}
