import AVFoundation
import Observation

/// Spoken guidance.
///
/// Uses `.duckOthers` rather than interrupting, so a prompt talks over music
/// or a podcast without stopping it — and `.mixWithOthers` would let the
/// instruction be drowned out, which defeats the point when someone is
/// listening to something loud.
@Observable
@MainActor
final class SpeechService: NSObject {
    var isEnabled = true

    private let synthesizer = AVSpeechSynthesizer()
    private var sessionActive = false

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func speak(_ text: String, priority: Priority = .normal) {
        guard isEnabled, !text.isEmpty else { return }

        // A high-priority prompt (the turn is imminent) cuts off a lower one
        // still being read; a normal one waits its turn rather than clipping.
        if priority == .high, synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .word)
        }

        activateSession()

        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        utterance.prefersAssistiveTechnologySettings = true
        synthesizer.speak(utterance)
    }

    func stop() {
        synthesizer.stopSpeaking(at: .immediate)
        deactivateSession()
    }

    enum Priority {
        case normal, high
    }

    private func activateSession() {
        guard !sessionActive else { return }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(
                .playback, mode: .voicePrompt, options: [.duckOthers]
            )
            try session.setActive(true)
            sessionActive = true
        } catch {
            // Guidance is still useful on screen, so a failed audio session
            // must not take the navigation session down with it.
            sessionActive = false
        }
    }

    private func deactivateSession() {
        guard sessionActive else { return }
        try? AVAudioSession.sharedInstance().setActive(
            false, options: .notifyOthersOnDeactivation
        )
        sessionActive = false
    }
}

extension SpeechService: AVSpeechSynthesizerDelegate {
    nonisolated func speechSynthesizer(
        _ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance
    ) {
        Task { @MainActor in
            // Release the audio session between prompts so music un-ducks.
            guard !synthesizer.isSpeaking else { return }
            self.deactivateSession()
        }
    }
}
