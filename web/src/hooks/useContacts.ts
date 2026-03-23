/**
 * React Query hooks for user contacts management.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  listEmailContacts,
  createEmailContact,
  updateEmailContact,
  deleteEmailContact,
  listPhoneContacts,
  createPhoneContact,
  updatePhoneContact,
  deletePhoneContact,
  type EmailContact,
  type EmailContactCreate,
  type EmailContactUpdate,
  type PhoneContact,
  type PhoneContactCreate,
  type PhoneContactUpdate,
} from "../services/api/contacts";
import toast from "react-hot-toast";

// ---------------------------------------------------------------------------
// Query Keys
// ---------------------------------------------------------------------------

export const contactKeys = {
  all: ["contacts"] as const,
  email: () => [...contactKeys.all, "email"] as const,
  phone: () => [...contactKeys.all, "phone"] as const,
};

// ---------------------------------------------------------------------------
// Email Contacts Hooks
// ---------------------------------------------------------------------------

export function useEmailContacts() {
  return useQuery<EmailContact[], Error>({
    queryKey: contactKeys.email(),
    queryFn: listEmailContacts,
  });
}

export function useCreateEmailContact() {
  const queryClient = useQueryClient();

  return useMutation<EmailContact, Error, EmailContactCreate>({
    mutationFn: createEmailContact,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: contactKeys.email() });
      toast.success("Email contact added");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to add contact");
    },
  });
}

export function useUpdateEmailContact() {
  const queryClient = useQueryClient();

  return useMutation<EmailContact, Error, { id: number; contact: EmailContactUpdate }>({
    mutationFn: ({ id, contact }) => updateEmailContact(id, contact),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: contactKeys.email() });
      toast.success("Email contact updated");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to update contact");
    },
  });
}

export function useDeleteEmailContact() {
  const queryClient = useQueryClient();

  return useMutation<void, Error, number>({
    mutationFn: deleteEmailContact,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: contactKeys.email() });
      toast.success("Email contact removed");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to remove contact");
    },
  });
}

// ---------------------------------------------------------------------------
// Phone Contacts Hooks
// ---------------------------------------------------------------------------

export function usePhoneContacts() {
  return useQuery<PhoneContact[], Error>({
    queryKey: contactKeys.phone(),
    queryFn: listPhoneContacts,
  });
}

export function useCreatePhoneContact() {
  const queryClient = useQueryClient();

  return useMutation<PhoneContact, Error, PhoneContactCreate>({
    mutationFn: createPhoneContact,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: contactKeys.phone() });
      toast.success("Phone contact added");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to add contact");
    },
  });
}

export function useUpdatePhoneContact() {
  const queryClient = useQueryClient();

  return useMutation<PhoneContact, Error, { id: number; contact: PhoneContactUpdate }>({
    mutationFn: ({ id, contact }) => updatePhoneContact(id, contact),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: contactKeys.phone() });
      toast.success("Phone contact updated");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to update contact");
    },
  });
}

export function useDeletePhoneContact() {
  const queryClient = useQueryClient();

  return useMutation<void, Error, number>({
    mutationFn: deletePhoneContact,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: contactKeys.phone() });
      toast.success("Phone contact removed");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to remove contact");
    },
  });
}
