import { lazy } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import type { ReactNode } from "react";
import { Layout } from "./components/Layout";
import { Login } from "./pages/Login";
import { AuthProvider } from "./lib/auth";
import { useAuth } from "./lib/useAuth";
import { ThemeProvider } from "./lib/theme";
import { CustodyProvider } from "./lib/custodyContext";
import { isFullAdmin, isPrivileged } from "./lib/roles";

// Every authenticated page is loaded on demand (React.lazy + Vite's
// automatic code-splitting) instead of all landing in one ~1MB bundle
// that has to be downloaded and parsed before ANYTHING renders -- Vite's
// own build output was flagging exactly this ("Some chunks are larger
// than 500 kB after minification"). This has an outsized effect for two
// groups in particular:
//   - Dashboard is the only page that imports `recharts` (a genuinely
//     heavy charting library) -- lazy-loading it means that weight is no
//     longer forced onto EVERY page load, only whoever actually opens
//     the Overview.
//   - Admin/Manager alone pull in every panel under pages/admin/ (Users,
//     Outsiders, Quotes, System Backups, Settings, Audit, Deleted
//     Assets/Users, Inventory Import -- over 130KB of source) purely to
//     render two role-gated routes a Staff/Customer session can never
//     even reach. Splitting them out means that code no longer ships to
//     -- or has to be parsed by -- a browser that will never load them.
// Login stays eager: it's the very first screen an unauthenticated
// visitor sees, so deferring it would only add a blank-then-flash delay
// with nothing to show for it.
const Dashboard = lazy(() => import("./pages/Dashboard").then((m) => ({ default: m.Dashboard })));
const Assets = lazy(() => import("./pages/Assets").then((m) => ({ default: m.Assets })));
const Checkouts = lazy(() => import("./pages/Checkouts").then((m) => ({ default: m.Checkouts })));
const Notifications = lazy(() => import("./pages/Notifications").then((m) => ({ default: m.Notifications })));
const Admin = lazy(() => import("./pages/Admin").then((m) => ({ default: m.Admin })));
const Manager = lazy(() => import("./pages/Admin").then((m) => ({ default: m.Manager })));
const MyItems = lazy(() => import("./pages/MyItems").then((m) => ({ default: m.MyItems })));
const Quotations = lazy(() => import("./pages/Quotations").then((m) => ({ default: m.Quotations })));
const Profile = lazy(() => import("./pages/Profile").then((m) => ({ default: m.Profile })));

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
                  <CustodyProvider>
                    <Layout />
                  </CustodyProvider>
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
