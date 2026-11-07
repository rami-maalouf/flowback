import Foundation
#if os(macOS)
import AppKit
#endif

let exactModule: ExactModule.Type = FlowbackModule.self

final class FlowbackModule: ExactModule {
    #if os(macOS)
    private var mixerScrolls: [ObjectIdentifier: MixerScrollRelay] = [:]

    required init(context: ExactModuleContext) {
        super.init(context: context)
        if !context.agent { EngineSupervisor.shared.start() }
    }

    override func call(_ request: [String: Any]) throws -> [String: Any] {
        guard request["op"] as? String == "managesEngine" else { throw ExactNativeRefusal("unknown flowback operation") }
        return ["managed": !context.agent]
    }

    override func later(_ request: [String: Any], reply: ExactReply) {
        guard request["op"] as? String == "engineReady" else {
            reply.fail("unknown flowback operation")
            return
        }
        if context.agent { reply.send(["error": ""]); return }
        EngineSupervisor.shared.whenReady { error in reply.send(["error": error]) }
    }

    override func willQuit(done: @escaping () -> Void) {
        if !context.agent { EngineSupervisor.shared.stop() }
        done()
    }

    override func element(_ element: ExactElement) {
        guard element.hatch?.name == "mixer-scroll", let view = element.view else { return }
        let key = ObjectIdentifier(element)
        guard mixerScrolls[key] == nil else { return }
        mixerScrolls[key] = MixerScrollRelay(view: view)
    }

    override func elementEnded(_ element: ExactElement) {
        mixerScrolls.removeValue(forKey: ObjectIdentifier(element))?.stop()
    }

    override func destroy() {
        for relay in mixerScrolls.values { relay.stop() }
        mixerScrolls.removeAll()
    }
    #endif
}

#if os(macOS)
private final class MixerScrollRelay {
    private weak var view: NSView?
    private weak var gestureScroll: NSScrollView?
    private var monitor: Any?

    init(view: NSView) {
        self.view = view
        monitor = NSEvent.addLocalMonitorForEvents(matching: .scrollWheel) { [weak self] event in
            guard let self else { return event }
            return self.route(event)
        }
    }

    func stop() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
        gestureScroll = nil
    }

    deinit { stop() }

    private func route(_ event: NSEvent) -> NSEvent? {
        guard let view, let window = view.window, event.window === window else { return event }
        guard !event.modifierFlags.contains(.control) else {
            gestureScroll = nil
            return event
        }
        let phased = !event.phase.isEmpty || !event.momentumPhase.isEmpty
        if !phased || event.phase.contains(.began) || event.phase.contains(.mayBegin) {
            gestureScroll = nil
        }

        var target = gestureScroll
        if target == nil {
            let vertical = event.scrollingDeltaY != 0 && abs(event.scrollingDeltaY) >= abs(event.scrollingDeltaX)
            let beginning = event.scrollingDeltaX == 0 && event.scrollingDeltaY == 0
                && (event.phase.contains(.began) || event.phase.contains(.mayBegin))
            guard vertical || beginning,
                  let content = window.contentView else { return event }
            let point = content.superview?.convert(event.locationInWindow, from: nil) ?? event.locationInWindow
            guard let hit = content.hitTest(point), hit === view || hit.isDescendant(of: view),
                  let outer = view.enclosingScrollView else { return event }
            target = outer
            if phased { gestureScroll = outer }
        }

        // keep the original phases and momentum on the outer native scroller.
        target?.scrollWheel(with: event)
        if event.phase.contains(.cancelled) || event.momentumPhase.contains(.ended)
            || event.momentumPhase.contains(.cancelled) {
            gestureScroll = nil
        }
        return nil
    }
}
#endif
