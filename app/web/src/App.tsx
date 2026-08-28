"use client";

import { AuthProvider, useAuth } from "./lib/auth";
import { ServerProvider } from "./lib/server";
import { RouterProvider, useRoute } from "./lib/router";
import { ToastProvider } from "./lib/toast";
import { UpdateProvider } from "./lib/update";
import { RatesProvider } from "./lib/rates";
import { ThemeProvider } from "./ui/theme";
import { AppShell } from "./components/AppShell";
import { UpdateDialog } from "./components/UpdateDialog";
import { Login } from "./screens/Login";
import { Dashboard } from "./screens/Dashboard";
import { AllUsers } from "./screens/AllUsers";
import { Connect } from "./screens/Connect";
import { ServerSettings } from "./screens/ServerSettings";
import { Connections } from "./screens/Connections";
import { Logs } from "./screens/Logs";
import { Console } from "./screens/Console";
import { HubDetail } from "./screens/HubDetail";
import { UserDetail } from "./screens/UserDetail";
import { Settings } from "./screens/Settings";

export default function App() {
  return (
    <RouterProvider>
      <ThemeProvider>
        <ToastProvider>
          <AuthProvider>
            <RatesProvider>
            <ServerProvider>
              <UpdateProvider>
                <Routed />
              </UpdateProvider>
            </ServerProvider>
            </RatesProvider>
          </AuthProvider>
        </ToastProvider>
      </ThemeProvider>
    </RouterProvider>
  );
}

/**
 * The route table. Hash paths:
 *
 *   /                         the dashboard: this machine + its SoftEther
 *   /connect                  connect the panel to the local SoftEther
 *   /users                    every user, across all hubs
 *   /hub/:hub[/:tab]          one Virtual Hub
 *   /hub/:hub/user/:name      one user
 *   /server-settings[/:sec]   server-wide SoftEther settings
 *   /connections              TCP connections into the server
 *   /logs                     log browser
 *   /console                  raw RPC console
 *   /settings[/:section]      the panel itself
 */
function Routed() {
  const { user, loading } = useAuth();
  const route = useRoute();

  if (loading) return <div className="loading" />;
  if (!user) return <Login />;

  const p = route.parts;
  let screen: React.ReactNode = <Dashboard />;

  if (p[0] === "connect") {
    screen = <Connect />;
  } else if (p[0] === "users") {
    screen = <AllUsers />;
  } else if (p[0] === "settings") {
    screen = <Settings section={p[1]} />;
  } else if (p[0] === "server-settings") {
    screen = <ServerSettings section={p[1]} />;
  } else if (p[0] === "connections") {
    screen = <Connections />;
  } else if (p[0] === "logs") {
    screen = <Logs />;
  } else if (p[0] === "console") {
    screen = <Console />;
  } else if (p[0] === "hub" && p[1]) {
    const hub = p[1];
    if (p[2] === "user" && p[3]) {
      screen = <UserDetail hub={hub} name={p[3]} />;
    } else {
      screen = <HubDetail hub={hub} tab={p[2] ?? "overview"} />;
    }
  }

  return (
    <>
      <AppShell>{screen}</AppShell>
      <UpdateDialog />
    </>
  );
}
