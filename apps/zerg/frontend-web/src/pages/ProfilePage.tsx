import React, { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { useAuth } from "../lib/auth";
import {
  Button,
  Card,
  SectionHeader,
  EmptyState,
  Input
} from "../components/ui";

interface UserUpdatePayload {
  display_name?: string | null;
  avatar_url?: string | null;
  prefs?: Record<string, unknown> | null;
}

// API function for updating user profile
async function updateUserProfile(data: UserUpdatePayload): Promise<{ id: number; email: string; display_name?: string; avatar_url?: string }> {
  const response = await fetch("/api/users/me", {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
    },
    credentials: 'include', // Cookie auth
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || "Failed to update profile");
  }

  return response.json();
}

// API function for uploading avatar
async function uploadAvatar(file: File): Promise<{ id: number; email: string; display_name?: string; avatar_url?: string }> {
  const formData = new FormData();
  formData.append("file", file);

  const response = await fetch("/api/users/me/avatar", {
    method: "POST",
    credentials: 'include', // Cookie auth
    body: formData,
  });

  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || "Failed to upload avatar");
  }

  return response.json();
}

export default function ProfilePage() {
  const { user } = useAuth();
  const queryClient = useQueryClient();

  // Form state
  const [displayName, setDisplayName] = useState(user?.display_name || "");
  const [avatarUrl, setAvatarUrl] = useState(user?.avatar_url || "");

  // Update profile mutation
  const updateProfileMutation = useMutation({
    mutationFn: updateUserProfile,
    onSuccess: (updatedUser) => {
      toast.success("Profile updated successfully!");
      // Update the auth context and queries
      queryClient.setQueryData(['current-user'], updatedUser);
      queryClient.invalidateQueries({ queryKey: ['current-user'] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to update profile: ${error.message}`);
    },
  });

  // Upload avatar mutation
  const uploadAvatarMutation = useMutation({
    mutationFn: uploadAvatar,
    onSuccess: (updatedUser) => {
      toast.success("Avatar uploaded successfully!");
      setAvatarUrl(updatedUser.avatar_url || "");
      // Update the auth context and queries
      queryClient.setQueryData(['current-user'], updatedUser);
      queryClient.invalidateQueries({ queryKey: ['current-user'] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to upload avatar: ${error.message}`);
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();

    const updateData: UserUpdatePayload = {};

    // Only include changed fields
    if (displayName !== (user?.display_name || "")) {
      updateData.display_name = displayName || null;
    }

    if (avatarUrl !== (user?.avatar_url || "")) {
      updateData.avatar_url = avatarUrl || null;
    }

    // Only make request if something changed
    if (Object.keys(updateData).length > 0) {
      updateProfileMutation.mutate(updateData);
    } else {
      toast.success("No changes to save");
    }
  };

  const handleAvatarFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    // Validate file size (2MB limit)
    if (file.size > 2 * 1024 * 1024) {
      toast.error("Avatar file must be smaller than 2MB");
      return;
    }

    // Validate file type
    if (!file.type.startsWith('image/')) {
      toast.error("Avatar must be an image file");
      return;
    }

    uploadAvatarMutation.mutate(file);
  };

  const handleReset = () => {
    setDisplayName(user?.display_name || "");
    setAvatarUrl(user?.avatar_url || "");
  };

  if (!user) {
    return (
      <div className="profile-page-container">
        <EmptyState
          icon={<div className="spinner" style={{ width: 40, height: 40 }} />}
          title="Loading profile..."
          description="Fetching your account details."
        />
      </div>
    );
  }

  return (
    <div className="profile-page-container">
      <SectionHeader
        title="User Profile"
        description="Manage your account settings and preferences."
      />

      <form onSubmit={handleSubmit} className="profile-form">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-6)' }}>
          {/* Avatar Section */}
          <Card>
            <Card.Header>
              <h3 style={{ margin: 0 }}>Avatar</h3>
            </Card.Header>
            <Card.Body>
              <div className="avatar-section">
                <div className="current-avatar">
                  {user.avatar_url ? (
                    <img
                      src={user.avatar_url}
                      alt="Current avatar"
                      className="avatar-preview"
                    />
                  ) : (
                    <div className="avatar-placeholder">
                      {user.display_name?.[0]?.toUpperCase() || user.email[0].toUpperCase()}
                    </div>
                  )}
                </div>
                <div className="avatar-upload">
                  <input
                    type="file"
                    id="avatar-file"
                    accept="image/*"
                    onChange={handleAvatarFileChange}
                    disabled={uploadAvatarMutation.isPending}
                  />
                  <Button
                    variant="secondary"
                    as="label"
                    htmlFor="avatar-file"
                    disabled={uploadAvatarMutation.isPending}
                  >
                    {uploadAvatarMutation.isPending ? "Uploading..." : "Choose Avatar"}
                  </Button>
                  <small>PNG, JPEG, or WebP. Max 2MB.</small>
                </div>
              </div>
            </Card.Body>
          </Card>

          {/* Profile Information */}
          <Card>
            <Card.Header>
              <h3 style={{ margin: 0 }}>Profile Information</h3>
            </Card.Header>
            <Card.Body>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
                <div className="form-group">
                  <label htmlFor="email" className="form-label">Email Address</label>
                  <Input
                    id="email"
                    value={user.email}
                    disabled
                    style={{ opacity: 0.6, cursor: 'not-allowed' }}
                  />
                  <small>Email cannot be changed</small>
                </div>

                <div className="form-group">
                  <label htmlFor="display-name" className="form-label">Display Name</label>
                  <Input
                    id="display-name"
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    placeholder="Enter your display name"
                    maxLength={100}
                  />
                </div>

                <div className="form-group">
                  <label htmlFor="avatar-url" className="form-label">Avatar URL</label>
                  <Input
                    id="avatar-url"
                    value={avatarUrl}
                    onChange={(e) => setAvatarUrl(e.target.value)}
                    placeholder="https://example.com/avatar.jpg"
                  />
                  <small>Optional: Direct URL to your avatar image</small>
                </div>
              </div>
            </Card.Body>
          </Card>

          {/* Account Information */}
          <Card>
            <Card.Header>
              <h3 style={{ margin: 0 }}>Account Information</h3>
            </Card.Header>
            <Card.Body>
              <div className="info-grid">
                <div className="info-item">
                  <span className="info-label">User ID:</span>
                  <span className="info-value">{user.id}</span>
                </div>
                <div className="info-item">
                  <span className="info-label">Member Since:</span>
                  <span className="info-value">
                    {new Date(user.created_at).toLocaleDateString()}
                  </span>
                </div>
                <div className="info-item">
                  <span className="info-label">Last Login:</span>
                  <span className="info-value">
                    {user.last_login
                      ? new Date(user.last_login).toLocaleString()
                      : "Never"
                    }
                  </span>
                </div>
              </div>
            </Card.Body>
          </Card>

          {/* Form Actions */}
          <div className="form-actions" style={{ display: 'flex', gap: 'var(--space-4)', justifyContent: 'flex-end', padding: 'var(--space-6) 0' }}>
            <Button
              type="button"
              variant="ghost"
              onClick={handleReset}
              disabled={updateProfileMutation.isPending}
            >
              Reset Changes
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="lg"
              disabled={updateProfileMutation.isPending}
            >
              {updateProfileMutation.isPending ? "Saving..." : "Save Changes"}
            </Button>
          </div>
        </div>
      </form>
    </div>
  );
}
