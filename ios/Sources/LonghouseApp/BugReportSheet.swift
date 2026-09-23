import PhotosUI
import SwiftUI
import UIKit

@MainActor
struct BugReportSheet: View {
    @EnvironmentObject private var appState: AppState
    @Environment(\.dismiss) private var dismiss

    let sourceSessionID: String?
    let initialContextJSON: Data
    let initialScreenshot: Data?
    let onSent: ((String) -> Void)?
    let onSaved: (() -> Void)?
    let autoStartFix: Bool

    private enum FailureAction: Equatable {
        case retryHandoff
        case chooseTarget
        case reportInProgress
    }

    @State private var description = ""
    @State private var screenshotData: Data?
    @State private var additionalFiles: [BugReportUploadFile] = []
    @State private var photoItems: [PhotosPickerItem] = []
    @State private var reportID: String?
    @State private var clientReportID = UUID().uuidString
    @State private var targetSessionID: String?
    @State private var clientRequestID: String?
    @State private var showingLaunchPicker = false
    @State private var isUploading = false
    @State private var isSending = false
    @State private var didSend = false
    @State private var draftSaveTask: Task<Void, Never>?
    @State private var errorMessage: String?
    @State private var failureAction: FailureAction?

    @State private var statusMessage: String?
    @State private var showingDiscardConfirmation = false

    init(
        sourceSessionID: String?,
        contextJSON: Data,
        screenshotData: Data?,
        autoStartFix: Bool = false,
        onSent: ((String) -> Void)? = nil,
        onSaved: (() -> Void)? = nil
    ) {
        self.sourceSessionID = sourceSessionID
        self.initialContextJSON = contextJSON
        self.initialScreenshot = screenshotData
        self.autoStartFix = autoStartFix
        self.onSent = onSent
        self.onSaved = onSaved
        _screenshotData = State(initialValue: screenshotData)
    }

    private var canUpload: Bool {
        !description.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !isUploading && reportID == nil
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextEditor(text: $description)
                        .frame(minHeight: 130)
                        .overlay(alignment: .topLeading) {
                            if description.isEmpty {
                                Text("What went wrong? Include what you expected and what happened.")
                                    .foregroundStyle(.secondary)
                                    .padding(.top, 8)
                                    .allowsHitTesting(false)
                            }
                        }
                } header: {
                    Text("Describe the problem")
                }
                .listRowBackground(Ember.card)
                .disabled(reportID != nil)

                Section {
                    if let screenshotData, let image = BugReportScreenCapture.previewImage(from: screenshotData) {
                        Image(uiImage: image)
                            .resizable()
                            .scaledToFit()
                            .frame(maxHeight: 230)
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                            .accessibilityLabel("Captured Longhouse screen")
                        Button("Remove captured screenshot", role: .destructive) {
                            self.screenshotData = nil
                            saveDraft()
                        }
                        .foregroundStyle(Ember.ember)
                    } else {
                        Label("No screen capture", systemImage: "rectangle.slash")
                            .foregroundStyle(.secondary)
                    }
                    PhotosPicker(
                        selection: $photoItems,
                        maxSelectionCount: max(0, 4 - (screenshotData == nil ? 0 : 1)),
                        matching: .images
                    ) {
                        Label("Add screenshots from Photos", systemImage: "photo.on.rectangle")
                            .foregroundStyle(Ember.gold)
                    }
                    if !additionalFiles.isEmpty {
                        HStack {
                            Text("Added screenshots:")
                            Text(String(additionalFiles.count))
                        }
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    }
                } header: {
                    Text("Evidence")
                } footer: {
                    Text(
                        reportID == nil
                            ? "Recent iOS diagnostics and the visible Longhouse state are included. Review the screenshot and remove it if it contains anything sensitive."
                            : "This report is saved and immutable. Start a fix below when you are ready."
                    )
                }
                .listRowBackground(Ember.card)
                .disabled(reportID != nil)

                if let errorMessage {
                    Section {
                        Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(Ember.ember)
                    }
                    .listRowBackground(Ember.card)
                }
                if let statusMessage {
                    Section {
                        Label(statusMessage, systemImage: "checkmark.circle.fill")
                            .foregroundStyle(Ember.sage)
                    }
                    .listRowBackground(Ember.card)
                }
            }
            .emberListGround()
            .navigationTitle("Report a problem")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                        .disabled(isUploading || isSending)
                }
                ToolbarItem(placement: .confirmationAction) {
                    if isUploading || isSending {
                        ProgressView()
                    } else if reportID == nil {
                        Button("Send report") { Task { await uploadReport() } }
                            .disabled(!canUpload)
                    } else if targetSessionID == nil {
                        Menu("Start a fix") {
                            Button("Start a fix") { showingLaunchPicker = true }
                            Button("Start a new report", role: .destructive) {
                                showingDiscardConfirmation = true
                            }
                        }
                    } else if didSend {
                        Button("Done") { dismiss() }
                    } else if failureAction == .reportInProgress {
                        Button("Done") { dismiss() }
                    } else if failureAction == .chooseTarget {
                        Button("Choose target") { showingLaunchPicker = true }
                    } else {
                        Button(failureAction == .retryHandoff ? "Try again" : "Send to Console") {
                            Task { await sendReport() }
                        }
                    }
                }
            }
            .task {
                restoreDraft()
                guard autoStartFix else { return }
                await Task.yield()
                if reportID != nil {
                    showingLaunchPicker = true
                }
            }
            .onChange(of: description) { _, _ in scheduleDraftSave() }
            .onChange(of: photoItems) { _, items in
                Task { await loadPhotos(items) }
            }
            .sheet(isPresented: $showingLaunchPicker) {
                LaunchSessionSheet(
                    onLaunchSelection: { selection in
                        targetSessionID = selection.sessionId
                        errorMessage = nil
                        statusMessage = nil
                        failureAction = nil
                        let requestID = "ios-report-\(UUID().uuidString)"
                        let handoff = BugReportHandoff(
                            serverURL: appState.serverURL,
                            sourceSessionID: sourceSessionID,
                            reportID: reportID ?? "",
                            sessionID: selection.sessionId,
                            deviceID: selection.deviceId,
                            provider: selection.provider,
                            cwd: selection.cwd,
                            clientRequestID: requestID
                        )
                        clientRequestID = requestID
                        BugReportLocalStore.saveHandoff(handoff)
                    },
                    onLaunched: { sessionID in
                        showingLaunchPicker = false
                        targetSessionID = sessionID
                        // Pass the launch result explicitly. SwiftUI state writes
                        // are not a safe synchronization point for the task
                        // that starts the first report turn.
                        Task { await sendReport(sessionID: sessionID) }
                    }
                )
            }
            .confirmationDialog(
                "Start a new report?",
                isPresented: $showingDiscardConfirmation,
                titleVisibility: .visible
            ) {
                Button("Start New Report", role: .destructive) {
                    startNewReport()
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("The saved report will remain available, but this screen will start a fresh draft.")
            }
            .interactiveDismissDisabled(isUploading || isSending)
        }
    }

    private func uploadReport() async {
        guard canUpload else { return }
        guard let api = LonghouseAPI(host: appState.serverURL) else {
            errorMessage = "Enter a valid Longhouse server before sending the report."
            return
        }
        isUploading = true
        errorMessage = nil
        statusMessage = nil
        failureAction = nil
        saveDraft()
        defer { isUploading = false }
        do {
            let response = try await api.uploadBugReport(
                description: description,
                contextJSON: initialContextJSON,
                sourceSessionID: sourceSessionID,
                clientReportID: clientReportID,
                files: try reportFiles()
            )
            reportID = response.reportId
            draftSaveTask?.cancel()
            draftSaveTask = nil
            saveDraft()
            // Let the parent mark the presentation as finished before the
            // environment dismissal settles. Its onDismiss callback owns the
            // confirmation surface, so it cannot race this callback.
            onSaved?()
            dismiss()
        } catch {
            errorMessage = reportErrorMessage(
                for: error,
                fallback: "Could not save the bug report."
            )
        }
    }

    private func sendReport(sessionID explicitSessionID: String? = nil) async {
        guard !isSending else { return }
        guard let reportID,
              let sessionID = explicitSessionID ?? targetSessionID,
              let api = LonghouseAPI(host: appState.serverURL)
        else {
            errorMessage = "The report handoff is incomplete. Choose a target again."
            failureAction = .chooseTarget
            return
        }
        isSending = true
        errorMessage = nil
        statusMessage = nil
        failureAction = nil
        let requestID = clientRequestID ?? "ios-report-\(UUID().uuidString)"
        clientRequestID = requestID
        if let handoff = BugReportLocalStore.loadHandoff(), handoff.reportID == reportID, handoff.sessionID == sessionID {
            BugReportLocalStore.saveHandoff(
                BugReportHandoff(
                    serverURL: handoff.serverURL,
                    sourceSessionID: handoff.sourceSessionID ?? sourceSessionID,
                    reportID: handoff.reportID,
                    sessionID: handoff.sessionID,
                    deviceID: handoff.deviceID,
                    provider: handoff.provider,
                    cwd: handoff.cwd,
                    clientRequestID: requestID
                )
            )
        }
        defer { isSending = false }
        do {
            _ = try await api.sendInput(
                id: sessionID,
                text: "Investigate this iOS bug report. Read the staged report evidence before changing code.",
                intent: "auto",
                clientRequestId: requestID,
                reportID: reportID
            )
            draftSaveTask?.cancel()
            draftSaveTask = nil
            BugReportLocalStore.clearDraft()
            BugReportLocalStore.clearHandoff()
            didSend = true
            // Queue navigation before dismissing. The parent performs the
            // actual push from the sheet's onDismiss callback.
            onSent?(sessionID)
            dismiss()
        } catch {
            if let apiError = error as? LonghouseAPIError,
               apiError.structuredCode?.lowercased() == "report_in_progress"
            {
                BugReportLocalStore.clearHandoff()
                failureAction = .reportInProgress
                statusMessage = "This report is already being handled. Open Timeline to follow it."
                errorMessage = nil
            } else {
                saveHandoffForRetry()
                failureAction = isRetryableHandoffError(error) ? .retryHandoff : .chooseTarget
                errorMessage = reportErrorMessage(
                    for: error,
                    fallback: "The report was saved, but the selected target could not be started."
                )
            }
        }
    }

    private func sendReport() async {
        await sendReport(sessionID: nil)
    }

    private func saveHandoffForRetry() {
        guard let reportID, let targetSessionID,
              let handoff = BugReportLocalStore.loadHandoff(),
              handoff.reportID == reportID,
              handoff.sessionID == targetSessionID,
              let clientRequestID
        else { return }
        BugReportLocalStore.saveHandoff(
            BugReportHandoff(
                serverURL: handoff.serverURL,
                sourceSessionID: handoff.sourceSessionID ?? sourceSessionID,
                reportID: handoff.reportID,
                sessionID: handoff.sessionID,
                deviceID: handoff.deviceID,
                provider: handoff.provider,
                cwd: handoff.cwd,
                clientRequestID: clientRequestID
            )
        )
    }

    private func isRetryableHandoffError(_ error: Error) -> Bool {
        if let apiError = error as? LonghouseAPIError {
            return apiError.isRetryableReportHandoff
        }
        guard let urlError = error as? URLError else { return false }
        return [
            .timedOut,
            .cannotFindHost,
            .cannotConnectToHost,
            .networkConnectionLost,
            .notConnectedToInternet
        ].contains(urlError.code)
    }

    private func reportErrorMessage(for error: Error, fallback: String) -> String {
        if let apiError = error as? LonghouseAPIError {
            switch apiError.structuredCode?.lowercased() {
            case "idempotency_conflict":
                return "This report request was already used."
            case "report_stage_failed":
                return "Longhouse couldn't prepare the report."
            default:
                break
            }
        }
        return (error as? LocalizedError)?.errorDescription ?? fallback
    }

    private func reportFiles() throws -> [BugReportUploadFile] {
        var files: [BugReportUploadFile] = []
        for file in additionalFiles {
            let compressed = try ImageCompression.compress(file.data)
            files.append(
                BugReportUploadFile(
                    filename: file.filename,
                    mimeType: compressed.mimeType,
                    data: compressed.data
                )
            )
        }
        if let screenshotData {
            let compressed = try ImageCompression.compress(screenshotData)
            files.insert(
                BugReportUploadFile(
                    filename: "captured-screen.jpg",
                    mimeType: compressed.mimeType,
                    data: compressed.data
                ),
                at: 0
            )
        }
        return Array(files.prefix(4))
    }

    private func loadPhotos(_ items: [PhotosPickerItem]) async {
        guard !items.isEmpty else { return }
        var loaded: [BugReportUploadFile] = []
        var failed = false
        for (index, item) in items.prefix(4).enumerated() {
            do {
                guard let data = try await item.loadTransferable(type: Data.self) else {
                    failed = true
                    continue
                }
                let compressed = try ImageCompression.compress(data)
                loaded.append(
                    BugReportUploadFile(
                        filename: "photo-\(index).jpg",
                        mimeType: compressed.mimeType,
                        data: compressed.data
                    )
                )
            } catch {
                failed = true
            }
        }
        additionalFiles = Array(loaded.prefix(max(0, 4 - (screenshotData == nil ? 0 : 1))))
        photoItems = []
        errorMessage = failed ? "Some selected images could not be attached. Try choosing them again." : nil
        saveDraft()
    }


    private func restoreDraft() {
        guard let draft = BugReportLocalStore.loadDraft(),
              draft.serverURL == appState.serverURL,
              draft.sourceSessionID == sourceSessionID
        else { return }
        clientReportID = draft.clientReportID ?? clientReportID
        reportID = draft.reportID
        if description.isEmpty { description = draft.description }
        if screenshotData == nil { screenshotData = draft.screenshotData }
        if additionalFiles.isEmpty {
            additionalFiles = draft.additionalImages.enumerated().map {
                BugReportUploadFile(filename: "saved-photo-\($0.offset).jpg", mimeType: "image/jpeg", data: $0.element)
            }
        }
        if let handoff = BugReportLocalStore.loadHandoff(),
           handoff.serverURL == appState.serverURL,
           handoff.sourceSessionID == sourceSessionID,
           handoff.sessionID.isEmpty == false {
            reportID = handoff.reportID
            targetSessionID = handoff.sessionID
            clientRequestID = handoff.clientRequestID
        }
    }

    private func makeDraft() -> BugReportDraft? {
        guard reportID != nil || !description.isEmpty || screenshotData != nil || !additionalFiles.isEmpty else { return nil }
        return BugReportDraft(
            serverURL: appState.serverURL,
            sourceSessionID: sourceSessionID,
            clientReportID: clientReportID,
            reportID: reportID,
            description: description,
            screenshotData: screenshotData,
            additionalImages: additionalFiles.map(\.data)
        )
    }

    private func saveDraft() {
        draftSaveTask?.cancel()
        guard let draft = makeDraft() else {
            BugReportLocalStore.clearDraft()
            draftSaveTask = nil
            return
        }
        draftSaveTask = nil
        BugReportLocalStore.saveDraft(draft)
    }

    private func scheduleDraftSave() {
        draftSaveTask?.cancel()
        guard let draft = makeDraft() else {
            BugReportLocalStore.clearDraft()
            draftSaveTask = nil
            return
        }
        draftSaveTask = Task {
            try? await Task.sleep(nanoseconds: 350_000_000)
            guard !Task.isCancelled else { return }
            await Task.detached(priority: .utility) {
                BugReportLocalStore.saveDraft(draft)
            }.value
        }
    }
    private func startNewReport() {
        draftSaveTask?.cancel()
        BugReportLocalStore.clearDraft()
        BugReportLocalStore.clearHandoff()
        reportID = nil
        clientReportID = UUID().uuidString
        targetSessionID = nil
        clientRequestID = nil
        description = ""
        screenshotData = nil
        additionalFiles = []
        photoItems = []
        errorMessage = nil
        statusMessage = nil
        failureAction = nil
        didSend = false
    }
}

struct BugReportSavedBanner: View {
    let onStartFix: () -> Void
    let onDone: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "checkmark.circle.fill")
                    .font(.title3)
                    .foregroundStyle(Ember.sage)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Report saved")
                        .font(.headline)
                    Text("Your screenshot and diagnostics are attached.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
            HStack(spacing: 10) {
                Spacer(minLength: 0)
                Button("Done", action: onDone)
                    .buttonStyle(.bordered)
                Button("Start a fix", action: onStartFix)
                    .buttonStyle(.bordered)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(.white.opacity(0.18))
        }
        .shadow(color: .black.opacity(0.2), radius: 18, y: 8)
        .accessibilityIdentifier("bug-report-saved-banner")
    }
}

#Preview("Bug report handoff") {
    BugReportSheet(
        sourceSessionID: "session-preview",
        contextJSON: Data("{}".utf8),
        screenshotData: nil
    )
    .environmentObject(AppState())
}

#Preview("Bug report from Timeline") {
    BugReportSheet(
        sourceSessionID: nil,
        contextJSON: Data("{}".utf8),
        screenshotData: nil
    )
    .environmentObject(AppState())
}

#Preview("Report saved confirmation") {
    ZStack(alignment: .bottom) {
        Ember.page
            .ignoresSafeArea()
        BugReportSavedBanner(
            onStartFix: {},
            onDone: {}
        )
        .padding(16)
    }
    .preferredColorScheme(.dark)
    .emberChrome()
    .frame(width: 390, height: 844)
}
