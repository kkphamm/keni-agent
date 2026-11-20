import SwiftUI

struct HistoryView: View {
    private enum LoadState {
        case loading
        case loaded([ActionRecord])
        case unavailable
    }

    let voice: VoiceSession
    let accent: Color
    @State private var state = LoadState.loading
    @State private var refresh = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                SectionLabel(title: "Action history")
                Spacer()
                Button("Refresh", systemImage: "arrow.clockwise") { refresh += 1 }
                    .help("Refresh action history")
            }

            switch state {
            case .loading:
                ProgressView("Loading history…")
                    .controlSize(.small)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .unavailable:
                ContentUnavailableView(
                    "History is unavailable",
                    systemImage: "exclamationmark.circle",
                    description: Text("History will refresh when PostgreSQL and the local service are available.")
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .loaded(let records) where records.isEmpty:
                ContentUnavailableView(
                    "No saved actions",
                    systemImage: "clock",
                    description: Text("Run a voice command to start your history.")
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .loaded(let records):
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        if records.contains(where: { $0.state == .running }) {
                            Text("An action may have run before its outcome could be saved.")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .padding(.bottom, 8)
                        }
                        ForEach(records) { record in
                            row(record)
                            Divider()
                        }
                    }
                }
                .defaultScrollAnchor(.top)
            }
        }
        .padding(.horizontal, 24)
        .padding(.bottom, 12)
        .task(id: refresh) {
            while !Task.isCancelled {
                do {
                    let records = try await voice.loadHistory()
                    if !Task.isCancelled { state = .loaded(records) }
                } catch {
                    if !Task.isCancelled { state = .unavailable }
                }
                // Reconcile saved rows even if the LiveKit update never arrives.
                try? await Task.sleep(for: .seconds(5))
            }
        }
        .onChange(of: voice.actions) { refresh += 1 }
        .onChange(of: voice.serviceOnline) { refresh += 1 }
    }

    private func row(_ record: ActionRecord) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: record.action.icon)
                .frame(width: 20)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(record.action.title)
                        .font(.system(size: 14, weight: .medium))
                    Spacer(minLength: 0)
                    Text(record.startedAt, format: .dateTime.year().month(.abbreviated).day().hour().minute())
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if record.state == .running {
                    Label("No final outcome recorded", systemImage: "exclamationmark.circle")
                        .foregroundStyle(.orange)
                } else {
                    ActionStatus(state: record.state, accent: accent)
                    if let message = record.message, !message.isEmpty {
                        Text(message)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                }
            }
        }
        .font(.subheadline)
        .padding(.vertical, 10)
        .accessibilityElement(children: .combine)
    }
}
