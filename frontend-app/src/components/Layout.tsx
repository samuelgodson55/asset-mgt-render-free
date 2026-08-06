import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import { LayoutGrid, Boxes, ClipboardList, Bell, Search, LogOut, ShieldCheck, PackageCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { isPrivileged } from "../lib/roles";
import { ThemeToggle } from "./ThemeToggle";

const nav = [
  { to: "/", label: "Overview", icon: LayoutGrid, end: true },
  { to: "/assets", label: "Inventory", icon: Boxes },
  { to: "/checkouts", label: "Checkouts", icon: ClipboardList },
  { to: "/my-items", label: "My Items", icon: PackageCheck },
  { to: "/notifications", label: "Notifications", icon: Bell },
];

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export function Layout() {
  const [unread, setUnread] = useState(0);
  const [live, setLive] = useState(false);
  const { user, demo, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  // "Admin" is only worth showing to accounts (or demo browsers) that can
  // actually reach something behind it -- inventory import or system
  // backups. See lib/roles.ts / pages/Admin.tsx for the finer-grained gate
  // within the page itself (backups stay Super-Admin-only even here).
  const navItems = demo || isPrivileged(user?.role) ? [...nav, { to: "/admin", label: "Admin", icon: ShieldCheck }] : nav;

  useEffect(() => {
    api.getNotifications().then((n) => {
      setUnread(n.filter((x) => !x.read).length);
      setLive(api.isLive());
    });
  }, []);

  return (
    <div className="min-h-screen flex bg-ink">
      <aside className="w-[228px] shrink-0 border-r border-border-soft flex flex-col justify-between py-6 px-4 sticky top-0 h-screen">
        <div>
          <div className="flex items-center gap-2.5 px-2 mb-8">
            <svg width="20" height="20" viewBox="0 0 32 32" className="shrink-0">
              <path d="M4 4h14l10 10-14 14L4 18z" fill="#C89B3C" />
              <circle cx="9" cy="9" r="2.4" fill="#0F1219" />
            </svg>
            <div>
              <p className="font-display font-semibold text-[15px] leading-none text-text">Ledger</p>
              <p className="text-[10px] text-text-faint tracking-wide mt-0.5">Asset Management</p>
            </div>
          </div>

          <nav className="flex flex-col gap-0.5">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `relative flex items-center gap-2.5 px-2.5 py-2 rounded-[3px] text-[13px] font-medium transition-colors ${
                    isActive ? "text-text bg-surface-raised" : "text-text-muted hover:text-text hover:bg-surface"
                  }`
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && (
                      <motion.span
                        layoutId="nav-indicator"
                        className="absolute left-0 top-1.5 bottom-1.5 w-[2px] bg-brass rounded-full"
                        transition={{ type: "spring", stiffness: 500, damping: 40 }}
                      />
                    )}
                    <item.icon size={15} strokeWidth={1.75} />
                    {item.label}
                    {item.label === "Notifications" && unread > 0 && (
                      <span className="ml-auto font-mono text-[10px] px-1.5 py-0.5 rounded-full bg-brass/15 text-brass-soft">
                        {unread}
                      </span>
                    )}
                  </>
                )}
              </NavLink>
            ))}
          </nav>
        </div>

        <div className="flex items-center gap-1 px-1">
          <button
            onClick={() => navigate(demo ? "/profile" : "/profile")}
            title="My Profile"
            disabled={demo}
            className="flex-1 flex items-center gap-2.5 px-1.5 py-2 rounded-[3px] hover:bg-surface transition-colors text-left disabled:cursor-default disabled:hover:bg-transparent min-w-0"
          >
            <div className="w-7 h-7 rounded-full bg-gradient-to-br from-brass to-rust flex items-center justify-center font-display text-[11px] font-semibold text-ink shrink-0">
              {user ? initials(user.name) : "?"}
            </div>
            <div className="min-w-0">
              <p className="text-[12px] text-text truncate leading-tight">{user?.name ?? (demo ? "Demo browsing" : "Signed out")}</p>
              <p className="text-[10px] text-text-faint leading-tight capitalize">{user?.role?.replace("_", " ") ?? (demo ? "no account" : "")}</p>
            </div>
          </button>
          <button
            onClick={async () => {
              await logout();
              navigate("/login");
            }}
            title="Sign out"
            className="shrink-0 p-2 rounded-[3px] hover:bg-surface transition-colors group"
          >
            <LogOut size={14} className="text-text-faint group-hover:text-rust-soft transition-colors" />
          </button>
        </div>
      </aside>

      <div className="flex-1 min-w-0 flex flex-col">
        <header className="h-14 border-b border-border-soft flex items-center justify-between px-6 sticky top-0 bg-ink/85 backdrop-blur z-20">
          <div className="relative w-72 max-w-full">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
            <input
              placeholder="Search inventory, tags, people…"
              className="w-full bg-surface border border-border-soft rounded-[3px] pl-8 pr-3 py-1.5 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors"
            />
          </div>
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2 text-[11px] text-text-faint">
              <span className="relative flex h-1.5 w-1.5">
                <span className={`absolute inline-flex h-full w-full rounded-full opacity-60 animate-ping ${live ? "bg-moss" : "bg-brass"}`} />
                <span className={`relative inline-flex rounded-full h-1.5 w-1.5 ${live ? "bg-moss" : "bg-brass"}`} />
              </span>
              {live ? "Live — connected to backend" : "Demo data — backend unreachable"}
            </div>
            <ThemeToggle />
          </div>
        </header>

        <main className="flex-1 px-6 py-6 max-w-[1400px] w-full mx-auto">
          <AnimatePresence mode="wait">
            <motion.div
              key={location.pathname}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
            >
              <Outlet />
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
    </div>
  );
}
