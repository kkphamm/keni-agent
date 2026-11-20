import AVFoundation
import LiveKit
import os

final class MicrophoneMeter: NSObject, AudioRenderer {
    private struct Sample {
        var level: Double = 0
        var time: ContinuousClock.Instant?
    }

    private let latest = OSAllocatedUnfairLock(initialState: Sample())

    func render(pcmBuffer: AVAudioPCMBuffer) {
        let sample = Sample(level: Self.level(in: pcmBuffer), time: .now)
        latest.withLock { $0 = sample }
    }

    func recentLevel(at now: ContinuousClock.Instant = .now) -> Double {
        latest.withLock { sample in
            guard let time = sample.time, now - time < .milliseconds(150) else { return 0 }
            return sample.level
        }
    }

    static func level(in buffer: AVAudioPCMBuffer) -> Double {
        // LiveKit delivers non-interleaved Int16 blocks to its local renderer.
        let frames = Int(buffer.frameLength)
        let channels = Int(buffer.format.channelCount)
        guard let channelData = buffer.int16ChannelData, frames > 0, channels > 0 else { return 0 }
        let stride = buffer.stride
        var sumOfSquares = 0.0
        for channel in 0..<channels {
            let samples = channelData[channel]
            for frame in 0..<frames {
                let sample = Double(samples[frame * stride])
                sumOfSquares += sample * sample
            }
        }
        guard sumOfSquares > 0 else { return 0 }
        let rms = sqrt(sumOfSquares / (Double(frames) * Double(channels))) / 32_768
        let decibels = 20 * log10(rms)
        return min(1, max(0, (decibels + 52) / 46))
    }

    static func smooth(_ previous: Double, toward target: Double) -> Double {
        let fraction = target > previous ? 0.65 : 0.22
        let next = previous + (target - previous) * fraction
        return next < 0.003 ? 0 : next
    }
}
