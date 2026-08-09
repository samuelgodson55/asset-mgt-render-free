import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import { LayoutGrid, Boxes, ClipboardList, Bell, Search, LogOut, ShieldCheck, PackageCheck, FileText, Menu, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api, quotationsApi, setCurrencyCode } from "../lib/api";
import { useAuth } from "../lib/useAuth";
import { isPrivileged } from "../lib/roles";
import { ThemeToggle } from "./ThemeToggle";
import { NotificationBell } from "./NotificationBell";

// "Checkouts" (system-wide overdue/due-soon + extension-request review) is
// deliberately left out of this base list -- it's built entirely on top of
// GET /checkouts/overdue, /checkouts/due-soon, and the extension-request
// endpoints, all require_privileged_role on the backend (see deps.py).
// Staff/Customer never had an equivalent tab in the legacy frontend either
// (staff.html/customer.html only ever had My Items + Quotation); it's
// appended below only for a privileged role (or demo), same gate as the
// Admin/Manager link further down, and App.tsx's <RequireRole> keeps a
// typed/bookmarked /checkouts URL from reaching a role that can't use it.
const nav: { to: string; label: string; icon: typeof LayoutGrid; end?: boolean }[] = [
  { to: "/", label: "Overview", icon: LayoutGrid, end: true },
  { to: "/assets", label: "Inventory", icon: Boxes },
  { to: "/my-items", label: "My Items", icon: PackageCheck },
  { to: "/quotations", label: "Quotations", icon: FileText },
  { to: "/notifications", label: "Notifications", icon: Bell },
];

const checkoutsNavItem: (typeof nav)[number] = { to: "/checkouts", label: "Checkouts", icon: ClipboardList };

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export function Layout() {
  const [unread, setUnread] = useState(0);
  const [live, setLive] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const { user, demo, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  // Below `lg` the sidebar becomes an off-canvas drawer -- close it
  // automatically whenever the route changes so a tap on a nav link
  // doesn't leave it hanging open over the new page.
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

  // "Admin"/"Manager" is only worth showing to accounts (or demo browsers)
  // that can actually reach something behind it. Each role gets routed to
  // its OWN page now (pages/Admin.tsx's <Admin/> at /admin vs <Manager/>
  // at /manager -- mirrors the legacy frontend's separate admin.html/
  // manager.html) rather than sharing one URL, so the label and the link
  // target both follow the signed-in role together.
  const isManagerRole = !demo && user?.role === "manager";
  const privileged = demo || isPrivileged(user?.role);
  // Checkouts slots in right after Inventory (its natural position in the
  // base list) only for a privileged role/demo; Admin/Manager stays last.
  const navItems: typeof nav = privileged
    ? [nav[0], nav[1], checkoutsNavItem, ...nav.slice(2), { to: isManagerRole ? "/manager" : "/admin", label: isManagerRole ? "Manager" : "Admin", icon: ShieldCheck }]
    : nav;

  // Resolves the deployment's real currency (settings.CURRENCY_CODE, via
  // GET /config/public) once per app load, here in the shared shell every
  // authenticated page renders inside -- rather than leaving lib/api.ts's
  // formatPrice() on its NGN fallback until whichever page happens to call
  // quotationsApi.publicConfig() first (previously only Quotations.tsx),
  // which meant a price shown on, say, /assets before ever visiting
  // /quotations in that session could be in the wrong currency entirely.
  useEffect(() => {
    quotationsApi.publicConfig().then((config) => setCurrencyCode(config.currency_code));
  }, []);

  useEffect(() => {
    api.getNotifications(privileged).then((n) => {
      setUnread(n.filter((x) => !x.read).length);
      setLive(api.isLive());
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="min-h-screen flex bg-ink">
      {/* Backdrop -- only ever mounted (and only ever relevant) below `lg`, where the sidebar is an overlay drawer rather than part of the flex layout. */}
      {navOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/50 lg:hidden"
          onClick={() => setNavOpen(false)}
          aria-hidden="true"
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-40 w-[228px] shrink-0 border-r border-border-soft flex flex-col justify-between py-6 px-4 h-screen bg-ink transition-transform duration-300 ease-out lg:sticky lg:top-0 lg:translate-x-0 ${
          navOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div>
          <div className="flex items-center justify-between gap-2.5 px-2 mb-8">
            <div className="flex items-center gap-2.5">
              <svg width="20" height="20" viewBox="0 0 32 32" className="shrink-0">
                <path d="M4 4h14l10 10-14 14L4 18z" fill="#C89B3C" />
                <circle cx="9" cy="9" r="2.4" fill="#0F1219" />
              </svg>
              <div>
                <p className="font-display font-semibold text-[15px] leading-none text-text">Ledger</p>
                <p className="text-[10px] text-text-faint tracking-wide mt-0.5">Asset Management</p>
              </div>
            </div>
            <button
              onClick={() => setNavOpen(false)}
              className="lg:hidden p-1.5 rounded-[3px] text-text-faint hover:text-text hover:bg-surface transition-colors"
              aria-label="Close navigation"
            >
              <X size={16} />
            </button>
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
        <header className="h-14 border-b border-border-soft flex items-center justify-between gap-3 px-4 sm:px-6 sticky top-0 bg-ink/85 backdrop-blur z-20">
          <div className="flex items-center gap-3 min-w-0 flex-1">
            <button
              onClick={() => setNavOpen(true)}
              className="lg:hidden shrink-0 p-1.5 -ml-1.5 rounded-[3px] text-text-muted hover:text-text hover:bg-surface transition-colors"
              aria-label="Open navigation"
            >
              <Menu size={18} />
            </button>
            <div className="relative w-full max-w-xs hidden sm:block">
              <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
              <input
                placeholder="Search inventory, tags, people…"
                className="w-full bg-surface border border-border-soft rounded-[3px] pl-8 pr-3 py-1.5 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors"
              />
            </div>
            <button
              className="sm:hidden shrink-0 p-1.5 rounded-[3px] text-text-muted hover:text-text hover:bg-surface transition-colors"
              aria-label="Search"
            >
              <Search size={16} />
            </button>
          </div>
          <div className="flex items-center gap-2 sm:gap-4 shrink-0">
            <div className="hidden md:flex items-center gap-2 text-[11px] text-text-faint whitespace-nowrap">
              <span className="relative flex h-1.5 w-1.5">
                <span className={`absolute inline-flex h-full w-full rounded-full opacity-60 animate-ping ${live ? "bg-moss" : "bg-brass"}`} />
                <span className={`relative inline-flex rounded-full h-1.5 w-1.5 ${live ? "bg-moss" : "bg-brass"}`} />
              </span>
              {live ? "Live — connected to backend" : "Demo data — backend unreachable"}
            </div>
            <span className="relative flex h-1.5 w-1.5 md:hidden" title={live ? "Live — connected to backend" : "Demo data — backend unreachable"}>
              <span className={`absolute inline-flex h-full w-full rounded-full opacity-60 animate-ping ${live ? "bg-moss" : "bg-brass"}`} />
              <span className={`relative inline-flex rounded-full h-1.5 w-1.5 ${live ? "bg-moss" : "bg-brass"}`} />
            </span>
            <NotificationBell />
            <ThemeToggle />
          </div>
        </header>

        <main className="flex-1 px-4 sm:px-6 py-6 max-w-[1400px] w-full mx-auto">
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
