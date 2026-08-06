import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import type { ReactNode } from "react";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Assets } from "./pages/Assets";
import { Checkouts } from "./pages/Checkouts";
import { Notifications } from "./pages/Notifications";
import { Admin } from "./pages/Admin";
import { MyItems } from "./pages/MyItems";
import { Profile } from "./pages/Profile";
import { Login } from "./pages/Login";
import { AuthProvider, useAuth } from "./lib/auth";
import { ThemeProvider } from "./lib/theme";

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading, demo } = useAuth();
  if (loading) return null;
  if (!user && !demo) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <BrowserRouter basename="/app">
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
              <Route path="/checkouts" element={<Checkouts />} />
              <Route path="/notifications" element={<Notifications />} />
              <Route path="/my-items" element={<MyItems />} />
              <Route path="/profile" element={<Profile />} />
              <Route path="/admin" element={<Admin />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </ThemeProvider>
  );
}
