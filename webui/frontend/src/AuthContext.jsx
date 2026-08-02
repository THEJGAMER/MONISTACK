import React, { createContext, useContext } from "react";

// The one deliberate exception to this app's usual prop-drilling - role
// needs to reach nearly every page's action buttons, and threading
// {username, role} through every intermediate component would just be
// noise. Cosmetic gating only: the server enforces RBAC independently on
// every route (see require_role in app.py), the same "server enforces
// independently" model this app already used for the comment-author check.
const AuthContext = createContext({ username: null, role: "viewer" });

export function AuthProvider({ user, children }) {
  return <AuthContext.Provider value={user}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  return useContext(AuthContext);
}

const ROLE_RANK = { viewer: 0, operator: 1, admin: 2 };

export function roleMeets(role, minRole) {
  return (ROLE_RANK[role] ?? -1) >= (ROLE_RANK[minRole] ?? 0);
}

export function useHasRole(minRole) {
  const { role } = useAuth();
  return roleMeets(role, minRole);
}
