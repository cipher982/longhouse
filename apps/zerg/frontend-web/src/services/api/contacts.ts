/**
 * API functions for user contacts management.
 *
 * Approved contacts are used by agents to validate email/SMS recipients.
 */

import { request } from "./base";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface EmailContact {
  id: number;
  name: string;
  email: string;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface PhoneContact {
  id: number;
  name: string;
  phone: string;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface EmailContactCreate {
  name: string;
  email: string;
  notes?: string;
}

export interface EmailContactUpdate {
  name?: string;
  email?: string;
  notes?: string;
}

export interface PhoneContactCreate {
  name: string;
  phone: string;
  notes?: string;
}

export interface PhoneContactUpdate {
  name?: string;
  phone?: string;
  notes?: string;
}

// ---------------------------------------------------------------------------
// Email Contacts API
// ---------------------------------------------------------------------------

export async function listEmailContacts(): Promise<EmailContact[]> {
  return request<EmailContact[]>("/user/contacts/email");
}

export async function createEmailContact(contact: EmailContactCreate): Promise<EmailContact> {
  return request<EmailContact>("/user/contacts/email", {
    method: "POST",
    body: JSON.stringify(contact),
  });
}

export async function updateEmailContact(
  id: number,
  contact: EmailContactUpdate
): Promise<EmailContact> {
  return request<EmailContact>(`/user/contacts/email/${id}`, {
    method: "PUT",
    body: JSON.stringify(contact),
  });
}

export async function deleteEmailContact(id: number): Promise<void> {
  return request<void>(`/user/contacts/email/${id}`, {
    method: "DELETE",
  });
}

// ---------------------------------------------------------------------------
// Phone Contacts API
// ---------------------------------------------------------------------------

export async function listPhoneContacts(): Promise<PhoneContact[]> {
  return request<PhoneContact[]>("/user/contacts/phone");
}

export async function createPhoneContact(contact: PhoneContactCreate): Promise<PhoneContact> {
  return request<PhoneContact>("/user/contacts/phone", {
    method: "POST",
    body: JSON.stringify(contact),
  });
}

export async function updatePhoneContact(
  id: number,
  contact: PhoneContactUpdate
): Promise<PhoneContact> {
  return request<PhoneContact>(`/user/contacts/phone/${id}`, {
    method: "PUT",
    body: JSON.stringify(contact),
  });
}

export async function deletePhoneContact(id: number): Promise<void> {
  return request<void>(`/user/contacts/phone/${id}`, {
    method: "DELETE",
  });
}
