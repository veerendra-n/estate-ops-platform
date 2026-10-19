import { createContext, useContext } from "react";
import type { Health, Me, Tenant } from "./types";

export interface AppState {
  tenants: Tenant[];
  me: Me | null;
  health: Health | null;
  labEnabled: boolean;
  tenantName: (id: string) => string;
}

export const AppContext = createContext<AppState>({
  tenants: [],
  me: null,
  health: null,
  labEnabled: false,
  tenantName: (id) => id,
});

export const useApp = () => useContext(AppContext);
