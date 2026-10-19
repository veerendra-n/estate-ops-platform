import { useCallback, useEffect, useMemo, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { api, ApiError, onUnauthorized } from "./api";
import { AppContext, type AppState } from "./context";
import type { Health, Me, Tenant } from "./types";
import { Layout } from "./components/Layout";
import { SignIn } from "./components/SignIn";
import { Loading, NowProvider } from "./components/ui";
import Queue from "./pages/Queue";
import CaseWorkspace from "./pages/CaseWorkspace";
import Estate, { EstateIndex } from "./pages/Estate";
import AssetPage from "./pages/Asset";
import Skills from "./pages/Skills";
import Customers from "./pages/Customers";
import Integrations from "./pages/Integrations";
import Cost from "./pages/Cost";
import Lab from "./pages/Lab";

type Auth = "checking" | "in" | "out";

export default function App() {
  const [auth, setAuth] = useState<Auth>("checking");
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [me, setMe] = useState<Me | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [labEnabled, setLabEnabled] = useState(false);
  const [bootError, setBootError] = useState<string | null>(null);

  const boot = useCallback(async () => {
    setBootError(null);
    api<Me>("/auth/me").then(setMe, () => setMe(null));
    api<Health>("/healthz").then(setHealth, () => setHealth(null));
    try {
      const t = await api<Tenant[]>("/api/v1/tenants");
      setTenants(t);
      setAuth("in");
      api("/api/v1/lab/faults").then(
        () => setLabEnabled(true),
        () => setLabEnabled(false),
      );
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setAuth("out");
      else {
        setBootError((e as Error).message);
        setAuth("in");
      }
    }
  }, []);

  useEffect(() => {
    void boot();
    return onUnauthorized(() => setAuth("out"));
  }, [boot]);

  const state: AppState = useMemo(() => {
    const names = new Map(tenants.map((t) => [t.id, t.name]));
    return { tenants, me, health, labEnabled, tenantName: (id: string) => names.get(id) ?? id };
  }, [tenants, me, health, labEnabled]);

  if (auth === "checking")
    return (
      <div className="boot">
        <Loading label="Connecting to Estate Ops" />
      </div>
    );
  if (auth === "out") return <SignIn onSignedIn={() => void boot()} />;

  return (
    <AppContext.Provider value={state}>
      <NowProvider>
        <Layout onSignedOut={() => setAuth("out")} bootError={bootError}>
          <Routes>
            <Route path="/" element={<Queue />} />
            <Route path="/cases/:id" element={<CaseWorkspace />} />
            <Route path="/estate" element={<EstateIndex />} />
            <Route path="/estate/:tenant" element={<Estate />} />
            <Route path="/estate/:tenant/assets/:assetId" element={<AssetPage />} />
            <Route path="/skills" element={<Skills />} />
            <Route path="/customers" element={<Customers />} />
            <Route path="/integrations" element={<Integrations />} />
            <Route path="/cost" element={<Cost />} />
            <Route path="/lab" element={<Lab />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Layout>
      </NowProvider>
    </AppContext.Provider>
  );
}
