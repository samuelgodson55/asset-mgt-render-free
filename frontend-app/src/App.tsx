import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import type { ReactNode } from "react";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Assets } from "./pages/Assets";
import { Checkouts } from "./pages/Checkouts";
import { Notifications } from "./pages/Notifications";
import { Admin, Manager } from "./pages/Admin";
import { MyItems } from "./pages/MyItems";
import { Quotations } from "./pages/Quotations";
import { Profile } from "./pages/Profile";
import { Login } from "./pages/Login";
import { AuthProvider } from "./lib/auth";
import { useAuth } from "./lib/useAuth";
import { ThemeProvider } from "./lib/theme";
import { isFullAdmin, isPrivileged } from "./lib/roles";

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading, demo } = useAuth();
  if (loading) return null;
  if (!user && !demo) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

// Route-level equivalent of lib/roles.ts's backend gates -- keeps a role
// that can't use a page from ever landing on it via a typed/bookmarked
// URL, not just from the nav no longer linking there (see Layout.tsx).
// `redirectTo` is where that role's OWN page actually is, so a manager
// hitting /admin lands on /manager instead of a dead end.
function RequireRole({
  allow,
  redirectTo,
  children,
}: {
  allow: (role: string | null | undefined, demo: boolean) => boolean;
  redirectTo: string;
  children: ReactNode;
}) {
  const { user, demo, loading } = useAuth();
  if (loading) return null;
  if (!allow(user?.role, demo)) return <Navigate to={redirectTo} replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route
              element={
                <RequireAuth>
                  <Layout />
                </RequireAuth>
              }
            >
              <Route path="/" element={<Dashboard />} />
              <Route path="/assets" element={<Assets />} />
              <Route
                path="/checkouts"
                element={
                  // System-wide overdue/due-soon + extension-request review --
                  // built on require_privileged_role endpoints (see
                  // lib/api.ts's loadCheckouts()). Staff/Customer never had an
                  // equivalent page in the legacy frontend either, so a
                  // typed/bookmarked URL sends them back to their own
                  // Overview rather than a page that's all 403s underneath.
                  <RequireRole allow={(role, demo) => demo || isPrivileged(role)} redirectTo="/">
                    <Checkouts />
                  </RequireRole>
                }
              />
              <Route path="/notifications" element={<Notifications />} />
              <Route path="/my-items" element={<MyItems />} />
              <Route path="/quotations" element={<Quotations />} />
              <Route path="/profile" element={<Profile />} />
              <Route
                path="/admin"
                element={
                  <RequireRole allow={(role, demo) => demo || isFullAdmin(role)} redirectTo="/manager">
                    <Admin />
                  </RequireRole>
                }
              />
              <Route
                path="/manager"
                element={
                  <RequireRole allow={(role, demo) => demo || isPrivileged(role)} redirectTo="/">
                    <Manager />
                  </RequireRole>
                }
              />
            </Route>
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </ThemeProvider>
  );
}
