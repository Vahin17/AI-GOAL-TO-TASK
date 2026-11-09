'use client';

import { create } from 'zustand';
import { ardoApi, UserProfile, MongoUserProfile, LoginPayload, RegisterPayload, OAuthStatus } from '@/lib/ardo-api';

interface AuthState {
  user: UserProfile | null;
  profile: MongoUserProfile | null;
  oauthStatus: OAuthStatus | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;

  checkAuth: () => Promise<boolean>;
  fetchOAuthStatus: () => Promise<OAuthStatus>;
  login: (payload: LoginPayload) => Promise<boolean>;
  signup: (payload: RegisterPayload) => Promise<boolean>;
  logout: () => Promise<void>;
  clearError: () => void;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  profile: null,
  oauthStatus: null,
  isAuthenticated: false,
  isLoading: true,
  error: null,

  fetchOAuthStatus: async () => {
    try {
      const status = await ardoApi.getOAuthStatus();
      set({ oauthStatus: status });
      return status;
    } catch {
      const fallback = { google_enabled: false, facebook_enabled: false };
      set({ oauthStatus: fallback });
      return fallback;
    }
  },

  checkAuth: async () => {
    try {
      set({ isLoading: true, error: null });
      const user = await ardoApi.getMe();
      let profile: MongoUserProfile | null = null;
      try {
        profile = await ardoApi.getUserProfile();
      } catch {
        // Fallback gracefully if MongoDB sync is in progress
      }
      set({ user, profile, isAuthenticated: true, isLoading: false, error: null });
      return true;
    } catch {
      set({ user: null, profile: null, isAuthenticated: false, isLoading: false });
      return false;
    }
  },

  login: async (payload: LoginPayload) => {
    try {
      set({ isLoading: true, error: null });
      const user = await ardoApi.login(payload);
      set({ user, isAuthenticated: true, isLoading: false, error: null });
      return true;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Login failed. Please check your credentials.';
      set({ isLoading: false, error: msg });
      return false;
    }
  },

  signup: async (payload: RegisterPayload) => {
    try {
      set({ isLoading: true, error: null });
      const user = await ardoApi.register(payload);
      set({ user, isAuthenticated: true, isLoading: false, error: null });
      return true;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Registration failed. Please check your inputs.';
      set({ isLoading: false, error: msg });
      return false;
    }
  },

  logout: async () => {
    try {
      set({ isLoading: true });
      await ardoApi.logout();
    } catch {
      // Ignore network errors on logout
    } finally {
      set({ user: null, isAuthenticated: false, isLoading: false, error: null });
    }
  },

  clearError: () => set({ error: null }),
}));

