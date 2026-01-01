import React, { useState, useRef, useCallback } from "react";
import "./AddContextModal.css";

interface AddContextModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSubmit: (title: string, content: string) => Promise<void>;
  existingDocsCount?: number;
}

type Tab = "paste" | "upload";

export function AddContextModal({
  isOpen,
  onClose,
  onSubmit,
  existingDocsCount = 0,
}: AddContextModalProps) {
  const [activeTab, setActiveTab] = useState<Tab>("paste");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const resetForm = () => {
    setTitle("");
    setContent("");
    setError(null);
    setSuccessMessage(null);
  };

  const handleClose = () => {
    resetForm();
    setActiveTab("paste");
    onClose();
  };

  const handleSubmit = async () => {
    if (!title.trim()) {
      setError("Title is required");
      return;
    }
    if (!content.trim()) {
      setError("Content is required");
      return;
    }

    setIsSubmitting(true);
    setError(null);

    try {
      await onSubmit(title.trim(), content.trim());
      const savedTitle = title;
      setTitle("");
      setContent("");
      setError(null);
      setSuccessMessage(`"${savedTitle}" saved! Add another?`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save document");
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleFileSelect = useCallback((file: File) => {
    // Only accept text files for now
    if (!file.name.endsWith(".txt") && !file.name.endsWith(".md")) {
      setError("Only .txt and .md files are supported");
      return;
    }

    const reader = new FileReader();
    reader.onload = (e) => {
      const text = e.target?.result as string;
      setContent(text);
      // Auto-fill title from filename (without extension)
      const baseName = file.name.replace(/\.(txt|md)$/, "");
      setTitle(baseName);
      setError(null);
      setSuccessMessage(null);
    };
    reader.onerror = () => {
      setError("Failed to read file");
    };
    reader.readAsText(file);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragging(false);

      const file = e.dataTransfer.files[0];
      if (file) {
        handleFileSelect(file);
      }
    },
    [handleFileSelect]
  );

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
  }, []);

  const handleFileInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      handleFileSelect(file);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={handleClose}>
      <div
        className="modal-container add-context-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2>Add Context</h2>
          <button
            className="modal-close-button"
            onClick={handleClose}
            aria-label="Close"
          >
            &times;
          </button>
        </div>

        <div className="modal-content">
          {/* Tabs */}
          <div className="context-tabs">
            <button
              className={`context-tab ${activeTab === "paste" ? "active" : ""}`}
              onClick={() => {
                setActiveTab("paste");
                setError(null);
                setSuccessMessage(null);
              }}
            >
              Paste Text
            </button>
            <button
              className={`context-tab ${activeTab === "upload" ? "active" : ""}`}
              onClick={() => {
                setActiveTab("upload");
                setError(null);
                setSuccessMessage(null);
              }}
            >
              Upload File
            </button>
          </div>

          {/* Success Message */}
          {successMessage && (
            <div className="context-success">{successMessage}</div>
          )}

          {/* Error Message */}
          {error && <div className="context-error">{error}</div>}

          {/* Upload Tab */}
          {activeTab === "upload" && (
            <div
              className={`drop-zone ${isDragging ? "dragging" : ""} ${content ? "has-content" : ""}`}
              onDrop={handleDrop}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onClick={() => fileInputRef.current?.click()}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept=".txt,.md"
                onChange={handleFileInputChange}
                style={{ display: "none" }}
              />
              {content ? (
                <div className="drop-zone-content">
                  <span className="drop-zone-icon">&#10003;</span>
                  <p>File loaded: {title || "Untitled"}</p>
                  <small>Click or drop to replace</small>
                </div>
              ) : (
                <div className="drop-zone-content">
                  <span className="drop-zone-icon">&#8593;</span>
                  <p>Drop a file here or click to browse</p>
                  <small>.txt and .md files supported</small>
                </div>
              )}
            </div>
          )}

          {/* Form Fields */}
          <div className="context-form">
            <div className="form-group">
              <label className="form-label" htmlFor="context-title">
                Title
              </label>
              <input
                type="text"
                id="context-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="e.g., My Servers, Project Notes, Preferences"
                className="form-input"
              />
            </div>

            {activeTab === "paste" && (
              <div className="form-group">
                <label className="form-label" htmlFor="context-content">
                  Content
                </label>
                <textarea
                  id="context-content"
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  placeholder="Paste your notes, server details, preferences, or any context that would help your AI assistant..."
                  className="form-input context-textarea"
                  rows={10}
                />
              </div>
            )}

            {activeTab === "upload" && content && (
              <div className="form-group">
                <label className="form-label">Preview</label>
                <pre className="context-preview">{content.slice(0, 500)}{content.length > 500 ? "..." : ""}</pre>
              </div>
            )}
          </div>

          {/* Existing docs count */}
          <div className="context-docs-count">
            {existingDocsCount === 0
              ? "No context docs yet"
              : `You have ${existingDocsCount} context doc${existingDocsCount !== 1 ? "s" : ""}`}
          </div>
        </div>

        <div className="modal-actions">
          <button
            className="modal-button modal-button-secondary"
            onClick={handleClose}
          >
            Cancel
          </button>
          <button
            className="modal-button modal-button-primary"
            onClick={handleSubmit}
            disabled={isSubmitting || !title.trim() || !content.trim()}
          >
            {isSubmitting ? "Saving..." : "Save Document"}
          </button>
        </div>
      </div>
    </div>
  );
}
