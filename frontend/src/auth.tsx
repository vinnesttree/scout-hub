import { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api/client";

/** Who is signed in. `user` is null for anonymous (read-only) visitors. */
export function useAuth() {
  const q = useQuery({ queryKey: ["me"], queryFn: api.me, staleTime: 60_000 });
  return {
    user: q.data?.user ?? null,
    setupRequired: q.data?.setup_required ?? false,
    isLoading: q.isLoading,
  };
}

/** Route guard for the admin area: anonymous visitors are sent to /login. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, isLoading } = useAuth();
  const location = useLocation();
  if (isLoading) return <p className="muted">Loading…</p>;
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <>{children}</>;
}
