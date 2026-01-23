/**
 * Contacts Settings Page.
 *
 * Allows users to manage approved contacts for email and SMS.
 * Agents can only send to contacts in this list (or the user's own email).
 */

import { useState, useEffect, type FormEvent } from "react";
import {
  useEmailContacts,
  useCreateEmailContact,
  useUpdateEmailContact,
  useDeleteEmailContact,
  usePhoneContacts,
  useCreatePhoneContact,
  useUpdatePhoneContact,
  useDeletePhoneContact,
} from "../hooks/useContacts";
import type { EmailContact, PhoneContact } from "../services/api/contacts";
import { SectionHeader, EmptyState, Button, PageShell } from "../components/ui";
import { useConfirm } from "../components/confirm";
import "./ContactsPage.css";

type Tab = "email" | "phone";

interface ContactModalState {
  isOpen: boolean;
  mode: "create" | "edit";
  type: "email" | "phone";
  contact: EmailContact | PhoneContact | null;
  name: string;
  value: string; // email or phone
  notes: string;
}

const initialModalState: ContactModalState = {
  isOpen: false,
  mode: "create",
  type: "email",
  contact: null,
  name: "",
  value: "",
  notes: "",
};

export default function ContactsPage() {
  const [activeTab, setActiveTab] = useState<Tab>("email");
  const [modal, setModal] = useState<ContactModalState>(initialModalState);

  // Email contacts
  const { data: emailContacts, isLoading: emailLoading, error: emailError } = useEmailContacts();
  const createEmail = useCreateEmailContact();
  const updateEmail = useUpdateEmailContact();
  const deleteEmail = useDeleteEmailContact();

  // Phone contacts
  const { data: phoneContacts, isLoading: phoneLoading, error: phoneError } = usePhoneContacts();
  const createPhone = useCreatePhoneContact();
  const updatePhone = useUpdatePhoneContact();
  const deletePhone = useDeletePhoneContact();

  const confirm = useConfirm();

  const isLoading = emailLoading || phoneLoading;
  const error = emailError || phoneError;

  // Open modal for create
  const openCreateModal = (type: "email" | "phone") => {
    setModal({
      isOpen: true,
      mode: "create",
      type,
      contact: null,
      name: "",
      value: "",
      notes: "",
    });
  };

  // Open modal for edit
  const openEditModal = (contact: EmailContact | PhoneContact, type: "email" | "phone") => {
    setModal({
      isOpen: true,
      mode: "edit",
      type,
      contact,
      name: contact.name,
      value: type === "email" ? (contact as EmailContact).email : (contact as PhoneContact).phone,
      notes: contact.notes || "",
    });
  };

  const closeModal = () => {
    setModal(initialModalState);
  };

  const handleSave = async (e: FormEvent) => {
    e.preventDefault();

    if (modal.type === "email") {
      if (modal.mode === "create") {
        createEmail.mutate(
          { name: modal.name, email: modal.value, notes: modal.notes || undefined },
          { onSuccess: closeModal }
        );
      } else if (modal.contact) {
        updateEmail.mutate(
          {
            id: modal.contact.id,
            contact: { name: modal.name, email: modal.value, notes: modal.notes || undefined },
          },
          { onSuccess: closeModal }
        );
      }
    } else {
      if (modal.mode === "create") {
        createPhone.mutate(
          { name: modal.name, phone: modal.value, notes: modal.notes || undefined },
          { onSuccess: closeModal }
        );
      } else if (modal.contact) {
        updatePhone.mutate(
          {
            id: modal.contact.id,
            contact: { name: modal.name, phone: modal.value, notes: modal.notes || undefined },
          },
          { onSuccess: closeModal }
        );
      }
    }
  };

  const handleDelete = async (contact: EmailContact | PhoneContact, type: "email" | "phone") => {
    const confirmed = await confirm({
      title: `Remove "${contact.name}"?`,
      message:
        type === "email"
          ? "Your agents will no longer be able to send emails to this contact."
          : "Your agents will no longer be able to send SMS to this contact.",
      confirmLabel: "Remove",
      cancelLabel: "Keep",
      variant: "danger",
    });

    if (!confirmed) return;

    if (type === "email") {
      deleteEmail.mutate(contact.id);
    } else {
      deletePhone.mutate(contact.id);
    }
  };

  // Ready signal for tests
  useEffect(() => {
    if (!isLoading) {
      document.body.setAttribute("data-ready", "true");
    }
    return () => document.body.removeAttribute("data-ready");
  }, [isLoading]);

  if (error) {
    return (
      <PageShell size="narrow" className="contacts-page-container">
        <EmptyState variant="error" title="Error loading contacts" description={String(error)} />
      </PageShell>
    );
  }

  return (
    <PageShell size="narrow" className="contacts-page-container">
      <SectionHeader
        title="Approved Contacts"
        description="Manage contacts that your agents can send emails or SMS to. Agents can only contact people on this list."
      />

      <div className="contacts-tabs">
        <button
          className={`contacts-tab ${activeTab === "email" ? "active" : ""}`}
          onClick={() => setActiveTab("email")}
        >
          Email Contacts
          {emailContacts && emailContacts.length > 0 && (
            <span className="contacts-tab-count">{emailContacts.length}</span>
          )}
        </button>
        <button
          className={`contacts-tab ${activeTab === "phone" ? "active" : ""}`}
          onClick={() => setActiveTab("phone")}
        >
          Phone Contacts
          {phoneContacts && phoneContacts.length > 0 && (
            <span className="contacts-tab-count">{phoneContacts.length}</span>
          )}
        </button>
      </div>

      <div className="contacts-content">
        {isLoading ? (
          <EmptyState
            icon={<div className="spinner" style={{ width: 40, height: 40 }} />}
            title="Loading contacts..."
            description="Fetching your approved contacts."
          />
        ) : activeTab === "email" ? (
          <div className="contacts-section">
            <div className="contacts-toolbar">
              <Button variant="primary" onClick={() => openCreateModal("email")}>
                + Add Email Contact
              </Button>
            </div>

            {emailContacts && emailContacts.length > 0 ? (
              <table className="contacts-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Email</th>
                    <th>Notes</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {emailContacts.map((contact) => (
                    <tr key={contact.id}>
                      <td className="contact-name">{contact.name}</td>
                      <td className="contact-value">{contact.email}</td>
                      <td className="contact-notes">{contact.notes || "-"}</td>
                      <td className="contact-actions">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => openEditModal(contact, "email")}
                        >
                          Edit
                        </Button>
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() => handleDelete(contact, "email")}
                        >
                          Remove
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <EmptyState
                title="No email contacts yet"
                description="Add contacts to let your agents send emails on your behalf."
              />
            )}
          </div>
        ) : (
          <div className="contacts-section">
            <div className="contacts-toolbar">
              <Button variant="primary" onClick={() => openCreateModal("phone")}>
                + Add Phone Contact
              </Button>
            </div>

            {phoneContacts && phoneContacts.length > 0 ? (
              <table className="contacts-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Phone</th>
                    <th>Notes</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {phoneContacts.map((contact) => (
                    <tr key={contact.id}>
                      <td className="contact-name">{contact.name}</td>
                      <td className="contact-value">{contact.phone}</td>
                      <td className="contact-notes">{contact.notes || "-"}</td>
                      <td className="contact-actions">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => openEditModal(contact, "phone")}
                        >
                          Edit
                        </Button>
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() => handleDelete(contact, "phone")}
                        >
                          Remove
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <EmptyState
                title="No phone contacts yet"
                description="Add contacts to let your agents send SMS on your behalf."
              />
            )}
          </div>
        )}
      </div>

      {/* Contact Modal */}
      {modal.isOpen && (
        <div className="modal-overlay" onClick={closeModal}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>
                {modal.mode === "create" ? "Add" : "Edit"}{" "}
                {modal.type === "email" ? "Email" : "Phone"} Contact
              </h3>
              <button className="modal-close" onClick={closeModal}>
                &times;
              </button>
            </div>
            <form onSubmit={handleSave}>
              <div className="modal-body">
                <div className="form-group">
                  <label htmlFor="contact-name">Name</label>
                  <input
                    id="contact-name"
                    type="text"
                    value={modal.name}
                    onChange={(e) => setModal((m) => ({ ...m, name: e.target.value }))}
                    placeholder="John Doe"
                    required
                    maxLength={100}
                    autoFocus
                  />
                </div>

                <div className="form-group">
                  <label htmlFor="contact-value">
                    {modal.type === "email" ? "Email Address" : "Phone Number"}
                  </label>
                  <input
                    id="contact-value"
                    type={modal.type === "email" ? "email" : "tel"}
                    value={modal.value}
                    onChange={(e) => setModal((m) => ({ ...m, value: e.target.value }))}
                    placeholder={
                      modal.type === "email" ? "john@example.com" : "+14155552671"
                    }
                    required
                  />
                  {modal.type === "phone" && (
                    <p className="form-hint">
                      Use E.164 format with country code (e.g., +14155552671)
                    </p>
                  )}
                </div>

                <div className="form-group">
                  <label htmlFor="contact-notes">Notes (optional)</label>
                  <input
                    id="contact-notes"
                    type="text"
                    value={modal.notes}
                    onChange={(e) => setModal((m) => ({ ...m, notes: e.target.value }))}
                    placeholder="Work contact, personal, etc."
                    maxLength={500}
                  />
                </div>
              </div>

              <div className="modal-footer">
                <Button type="button" variant="ghost" onClick={closeModal}>
                  Cancel
                </Button>
                <Button
                  type="submit"
                  variant="primary"
                  disabled={
                    createEmail.isPending ||
                    updateEmail.isPending ||
                    createPhone.isPending ||
                    updatePhone.isPending
                  }
                >
                  {modal.mode === "create" ? "Add Contact" : "Save Changes"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </PageShell>
  );
}
