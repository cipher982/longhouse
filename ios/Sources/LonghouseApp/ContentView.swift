import SwiftUI

struct ContentView: View {
    @EnvironmentObject var appState: AppState
    @State private var showingServerConfig = false

    var body: some View {
        LonghouseWebView(serverURL: appState.serverURL)
            .ignoresSafeArea(.all, edges: .bottom)
            .overlay(alignment: .topTrailing) {
                Button {
                    showingServerConfig = true
                } label: {
                    Image(systemName: "gearshape")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundStyle(.secondary)
                        .padding(10)
                        .background(.ultraThinMaterial, in: Circle())
                }
                .padding(.trailing, 8)
                .padding(.top, 4)
            }
            .sheet(isPresented: $showingServerConfig) {
                ServerConfigSheet()
            }
    }
}

struct ServerConfigSheet: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var urlText = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Longhouse Server") {
                    TextField("https://your-instance.longhouse.ai", text: $urlText)
                        .textContentType(.URL)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                }
                Section {
                    Text("Enter the URL of your Longhouse instance. This is the same URL you use in your browser.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Server")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        let trimmed = urlText.trimmingCharacters(in: .whitespacesAndNewlines)
                        if !trimmed.isEmpty {
                            appState.setServer(trimmed)
                        }
                        dismiss()
                    }
                }
            }
            .onAppear {
                urlText = appState.serverURL
            }
        }
        .presentationDetents([.medium])
    }
}
