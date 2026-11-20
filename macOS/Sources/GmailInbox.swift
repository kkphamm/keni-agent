import SwiftUI

struct GmailInbox: View {
    private enum LoadState {
        case loading
        case loaded([GmailMessage])
        case unavailable(String)
    }

    let voice: VoiceSession
    @Environment(\.dismiss) private var dismiss
    @State private var state = LoadState.loading
    @State private var search = ""
    @State private var submittedQuery = ""
    @State private var refresh = 0

    private var query: String { search.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var validQuery: Bool { query.unicodeScalars.count <= 256 }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                SectionLabel(title: "Gmail inbox")
                Spacer()
                Button("Done") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            HStack {
                TextField("Search Gmail", text: $search)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(submit)
                Button(query.isEmpty ? "Refresh" : "Search", action: submit)
                    .disabled(!validQuery)
            }
            if !validQuery {
                Text("Use 256 characters or fewer.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            switch state {
            case .loading:
                ProgressView("Loading messages…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .unavailable(let message):
                ContentUnavailableView("Gmail is unavailable", systemImage: "envelope", description: Text(message))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .loaded(let messages) where messages.isEmpty:
                ContentUnavailableView(submittedQuery.isEmpty ? "Inbox is empty" : "No matching messages", systemImage: "tray")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .loaded(let messages):
                List(messages) { message in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(verbatim: message.subject.isEmpty ? "(No subject)" : message.subject)
                            .font(.subheadline.weight(.semibold))
                            .lineLimit(2)
                        if !message.sender.isEmpty {
                            Text(verbatim: message.sender)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                        if !message.snippet.isEmpty {
                            Text(verbatim: message.snippet)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .lineLimit(3)
                        }
                    }
                    .padding(.vertical, 5)
                    .accessibilityElement(children: .combine)
                }
                .listStyle(.inset)
            }
        }
        .padding(20)
        .frame(minWidth: 440, idealWidth: 500, minHeight: 350, idealHeight: 440)
        .task(id: refresh) {
            state = .loading
            do {
                let messages = try await voice.loadGmailInbox(query: submittedQuery)
                if !Task.isCancelled { state = .loaded(messages) }
            } catch {
                if !Task.isCancelled { state = .unavailable(error.localizedDescription) }
            }
        }
    }

    private func submit() {
        guard validQuery else { return }
        submittedQuery = query
        refresh += 1
    }
}
