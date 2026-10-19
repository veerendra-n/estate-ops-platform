// Dev-only fixture estates for the two demo customers.
import type { Asset, EstateChange, JsonObject, Relation } from "../types";

export interface MockEstate {
  assets: Asset[];
  relations: Relation[];
  changes: EstateChange[];
}

type Def = [kind: string, name: string, sources: string[], attrs?: JsonObject];

function build(defs: Def[], seenAt: (i: number) => string): Asset[] {
  return defs.map(([kind, name, sources, attrs], i) => ({
    id: `${kind}:${name}`,
    kind,
    name,
    sources,
    attrs: attrs ?? {},
    seen_at: seenAt(i),
  }));
}

export function contosoEstate(ago: (min: number) => string): MockEstate {
  const shop = ["CTO-SHOP-API01", "CTO-SHOP-API02", "CTO-SHOP-API03", "CTO-SHOP-WEB01", "CTO-SHOP-WEB02", "CTO-SHOP-WEB03", "CTO-SHOP-WEB04"];
  const defs: Def[] = [
    ["service", "Checkout", ["declared"], { critical: true, users: 3200, owner: "Digital commerce", tier: "gold" }],
    ["service", "Online shop", ["declared"], { critical: true, users: 3640, owner: "Digital commerce", tier: "gold" }],
    ["service", "Sales analytics", ["declared"], { critical: false, users: 85, owner: "BI team", tier: "bronze" }],
    ["service", "Store POS", ["declared"], { critical: true, users: 1400, owner: "Retail ops", tier: "gold" }],
    ["m365_service", "Exchange Online", ["microsoft"], { health: "healthy" }],
    ["m365_service", "Teams", ["microsoft"], { health: "healthy" }],
    ["application", "Shop CMS", ["declared"], { vendor: "Umbraco", version: "13.4" }],
    ["database", "CheckoutDB", ["vcenter", "microsoft"], { size_gb: 412, recovery: "full", critical: true }],
    ["database", "SalesDW", ["vcenter"], { size_gb: 1830, recovery: "simple" }],
    ["sql_instance", "CTO-SHOP-SQL01\\MSSQL", ["vcenter"], { version: "SQL Server 2022 CU14", edition: "Enterprise" }],
    ["sql_agent_job", "IndexMaintenance", ["vcenter"], { schedule: "Sun 02:00", last_outcome: "succeeded" }],
    ["backup_job", "Shop SQL nightly", ["veeam"], { schedule: "daily 01:00", last_result: "success" }],
    ["server", "CTO-SHOP-SQL01", ["vcenter", "veeam", "edge"], { os: "Windows Server 2022", vcpu: 16, memory_gb: 128, ip: "10.20.4.11", datastore: "CTO-DS-CRIT-02", host: "CTO-ESX-02", critical: true }],
    ...shop.map((n, i): Def => ["server", n, ["vcenter", "edge"], { os: "Ubuntu 24.04", vcpu: 4, memory_gb: 16, ip: `10.20.4.${21 + i}`, datastore: "CTO-DS-CRIT-02", host: `CTO-ESX-0${(i % 3) + 1}` }]),
    ["server", "CTO-SHOP-CMS01", ["vcenter", "edge"], { os: "Windows Server 2022", vcpu: 4, memory_gb: 16, ip: "10.20.4.41", datastore: "CTO-DS-CRIT-02", host: "CTO-ESX-01" }],
    ["server", "CTO-SHOP-CMS02", ["vcenter", "edge"], { os: "Windows Server 2022", vcpu: 4, memory_gb: 16, ip: "10.20.4.42", datastore: "CTO-DS-CRIT-02", host: "CTO-ESX-03" }],
    ["server", "CTO-SHOP-SEARCH01", ["vcenter", "edge"], { os: "Ubuntu 24.04", vcpu: 8, memory_gb: 32, ip: "10.20.4.51", datastore: "CTO-DS-CRIT-02", host: "CTO-ESX-02" }],
    ["server", "CTO-SHOP-REDIS01", ["vcenter"], { os: "Ubuntu 24.04", vcpu: 2, memory_gb: 8, ip: "10.20.4.61", datastore: "CTO-DS-CRIT-01", host: "CTO-ESX-01" }],
    ["server", "CTO-BI-ETL01", ["vcenter", "edge"], { os: "Windows Server 2019", vcpu: 8, memory_gb: 64, ip: "10.20.7.15", datastore: "CTO-DS-CRIT-02", host: "CTO-ESX-03", critical: false }],
    ["server", "CTO-FS01", ["vcenter", "veeam"], { os: "Windows Server 2022", vcpu: 4, memory_gb: 16, ip: "10.20.2.10", datastore: "CTO-DS-GEN-01", host: "CTO-ESX-04" }],
    ["endpoint", "POS-114", ["microsoft", "edge"], { os: "Windows 11 IoT", store: "Store 14", last_user: "till14" }],
    ["endpoint", "POS-117", ["microsoft", "edge"], { os: "Windows 11 IoT", store: "Store 17", last_user: "till17" }],
    ["cloud_resource", "contoso-shop-cdn", ["microsoft"], { type: "Azure Front Door", region: "westeurope" }],
    ["hypervisor_cluster", "CTO-CL-PROD", ["vcenter"], { hosts: 4, ha: true, drs: "fully automated" }],
    ["hypervisor_host", "CTO-ESX-01", ["vcenter"], { version: "ESXi 8.0 U3", cpu_pct: 41, memory_pct: 63 }],
    ["hypervisor_host", "CTO-ESX-02", ["vcenter"], { version: "ESXi 8.0 U3", cpu_pct: 47, memory_pct: 71 }],
    ["hypervisor_host", "CTO-ESX-03", ["vcenter"], { version: "ESXi 8.0 U3", cpu_pct: 78, memory_pct: 69 }],
    ["hypervisor_host", "CTO-ESX-04", ["vcenter"], { version: "ESXi 8.0 U2", cpu_pct: 22, memory_pct: 48 }],
    ["network_device", "CTO-CORE-SW01", ["edge"], { model: "Cisco C9500", location: "DC1" }],
    ["network_device", "CTO-FW-EDGE01", ["edge"], { model: "FortiGate 200F", location: "DC1" }],
    ["datastore", "CTO-DS-CRIT-01", ["vcenter"], { capacity_gb: 8192, free_gb: 3110, latency_ms: 2.1, tier: "critical" }],
    ["datastore", "CTO-DS-CRIT-02", ["vcenter"], { capacity_gb: 8192, free_gb: 1744, latency_ms: 38, tier: "critical" }],
    ["datastore", "CTO-DS-GEN-01", ["vcenter"], { capacity_gb: 32768, free_gb: 9120, latency_ms: 3.0, tier: "general" }],
    ["datastore", "CTO-DS-GEN-02", ["vcenter"], { capacity_gb: 32768, free_gb: 20268, latency_ms: 2.4, tier: "general" }],
    ["storage_array", "CTO-SAN-01", ["vcenter"], { model: "PowerStore 1200T", firmware: "3.6.1" }],
    ["backup_repository", "CTO-REPO-01", ["veeam"], { capacity_gb: 40960, free_gb: 11204 }],
    ["power", "CTO-UPS-A", ["edge"], { load_pct: 46, runtime_min: 31 }],
  ];
  const assets = build(defs, (i) => ago(2 + (i % 7)));
  const R = (s: string, rel: string, d: string): Relation => ({ src: s, rel, dst: d });
  const rel: Relation[] = [
    R("service:Online shop", "DEPENDS_ON", "service:Checkout"),
    R("service:Online shop", "DEPENDS_ON", "application:Shop CMS"),
    R("service:Online shop", "DEPENDS_ON", "cloud_resource:contoso-shop-cdn"),
    R("service:Checkout", "DEPENDS_ON", "database:CheckoutDB"),
    R("service:Sales analytics", "DEPENDS_ON", "database:SalesDW"),
    R("service:Sales analytics", "DEPENDS_ON", "server:CTO-BI-ETL01"),
    R("service:Store POS", "DEPENDS_ON", "endpoint:POS-114"),
    R("service:Store POS", "DEPENDS_ON", "endpoint:POS-117"),
    R("service:Store POS", "DEPENDS_ON", "service:Checkout"),
    R("application:Shop CMS", "RUNS_ON", "server:CTO-SHOP-CMS01"),
    R("application:Shop CMS", "RUNS_ON", "server:CTO-SHOP-CMS02"),
    R("database:CheckoutDB", "HOSTED_ON", "sql_instance:CTO-SHOP-SQL01\\MSSQL"),
    R("database:SalesDW", "HOSTED_ON", "sql_instance:CTO-SHOP-SQL01\\MSSQL"),
    R("sql_instance:CTO-SHOP-SQL01\\MSSQL", "RUNS_ON", "server:CTO-SHOP-SQL01"),
    R("sql_agent_job:IndexMaintenance", "RUNS_ON", "sql_instance:CTO-SHOP-SQL01\\MSSQL"),
    R("backup_job:Shop SQL nightly", "PROTECTS", "server:CTO-SHOP-SQL01"),
    R("backup_job:Shop SQL nightly", "WRITES_TO", "backup_repository:CTO-REPO-01"),
    R("server:CTO-SHOP-SEARCH01", "STORED_ON", "datastore:CTO-DS-CRIT-02"),
    R("server:CTO-SHOP-REDIS01", "STORED_ON", "datastore:CTO-DS-CRIT-01"),
    R("server:CTO-FS01", "STORED_ON", "datastore:CTO-DS-GEN-01"),
    R("hypervisor_host:CTO-ESX-01", "MEMBER_OF", "hypervisor_cluster:CTO-CL-PROD"),
    R("hypervisor_host:CTO-ESX-02", "MEMBER_OF", "hypervisor_cluster:CTO-CL-PROD"),
    R("hypervisor_host:CTO-ESX-03", "MEMBER_OF", "hypervisor_cluster:CTO-CL-PROD"),
    R("hypervisor_host:CTO-ESX-04", "MEMBER_OF", "hypervisor_cluster:CTO-CL-PROD"),
    R("hypervisor_host:CTO-ESX-02", "MOUNTS", "datastore:CTO-DS-CRIT-02"),
    R("hypervisor_host:CTO-ESX-03", "MOUNTS", "datastore:CTO-DS-CRIT-02"),
    R("hypervisor_host:CTO-ESX-03", "MOUNTS", "datastore:CTO-DS-GEN-02"),
    R("datastore:CTO-DS-CRIT-01", "BACKED_BY", "storage_array:CTO-SAN-01"),
    R("datastore:CTO-DS-CRIT-02", "BACKED_BY", "storage_array:CTO-SAN-01"),
    R("datastore:CTO-DS-GEN-01", "BACKED_BY", "storage_array:CTO-SAN-01"),
    R("datastore:CTO-DS-GEN-02", "BACKED_BY", "storage_array:CTO-SAN-01"),
    R("storage_array:CTO-SAN-01", "POWERED_BY", "power:CTO-UPS-A"),
    R("hypervisor_host:CTO-ESX-01", "CONNECTS_TO", "network_device:CTO-CORE-SW01"),
    R("network_device:CTO-CORE-SW01", "CONNECTS_TO", "network_device:CTO-FW-EDGE01"),
  ];
  for (const a of assets) {
    if (a.kind !== "server") continue;
    if (shop.includes(a.name)) rel.push(R("service:Checkout", "DEPENDS_ON", a.id));
    if (typeof a.attrs.host === "string") rel.push(R(a.id, "RUNS_ON", `hypervisor_host:${a.attrs.host}`));
    if (typeof a.attrs.datastore === "string" && a.name !== "CTO-SHOP-SEARCH01" && a.name !== "CTO-SHOP-REDIS01" && a.name !== "CTO-FS01")
      rel.push(R(a.id, "STORED_ON", `datastore:${a.attrs.datastore}`));
  }
  const changes: EstateChange[] = [
    { id: 9101, at: ago(185), source: "vcenter", asset_id: "server:CTO-SHOP-WEB04", asset_name: "CTO-SHOP-WEB04", change: "added", before: null, after: { os: "Ubuntu 24.04", vcpu: 4, memory_gb: 16, datastore: "CTO-DS-CRIT-02" } },
    { id: 9102, at: ago(60 * 26), source: "vcenter", asset_id: "hypervisor_host:CTO-ESX-03", asset_name: "CTO-ESX-03", change: "changed", before: { version: "ESXi 8.0 U2" }, after: { version: "ESXi 8.0 U3" } },
    { id: 9103, at: ago(60 * 30), source: "microsoft", asset_id: "endpoint:POS-117", asset_name: "POS-117", change: "changed", before: { compliance: "noncompliant" }, after: { compliance: "compliant" } },
    { id: 9104, at: ago(60 * 51), source: "edge", asset_id: "network_device:CTO-FW-EDGE01", asset_name: "CTO-FW-EDGE01", change: "changed", before: { firmware: "7.4.3" }, after: { firmware: "7.4.5" } },
    { id: 9105, at: ago(60 * 74), source: "vcenter", asset_id: "server:CTO-OLD-WEB09", asset_name: "CTO-OLD-WEB09", change: "removed", before: { os: "Ubuntu 18.04", datastore: "CTO-DS-GEN-01" }, after: null },
  ];
  return { assets, relations: rel, changes };
}

export function fabrikamEstate(ago: (min: number) => string): MockEstate {
  const defs: Def[] = [
    ["service", "EHR", ["declared"], { critical: true, users: 2100, owner: "Clinical systems", tier: "platinum" }],
    ["service", "Patient portal", ["declared"], { critical: true, users: 18500, owner: "Digital", tier: "gold" }],
    ["service", "PACS imaging", ["declared"], { critical: true, users: 310, owner: "Radiology", tier: "gold" }],
    ["m365_service", "Exchange Online", ["microsoft"], { health: "healthy" }],
    ["m365_service", "Intune", ["microsoft"], { health: "degraded" }],
    ["application", "Epic Hyperspace", ["declared"], { vendor: "Epic", version: "Nov 2025" }],
    ["database", "EHR", ["vcenter", "veeam"], { size_gb: 940, recovery: "full", critical: true }],
    ["database", "PACSMeta", ["vcenter"], { size_gb: 210, recovery: "full" }],
    ["sql_instance", "FAB-EHR-SQL01\\EHR", ["vcenter"], { version: "SQL Server 2022 CU13", edition: "Enterprise" }],
    ["backup_job", "EHR SQL nightly", ["veeam"], { schedule: "daily 00:30", last_result: "success", retention_days: 30 }],
    ["backup_job", "PACS weekly", ["veeam"], { schedule: "Sat 22:00", last_result: "success" }],
    ["server", "FAB-EHR-SQL01", ["vcenter", "veeam", "edge"], { os: "Windows Server 2022", vcpu: 16, memory_gb: 256, ip: "10.40.1.11", host: "FAB-HV-01", critical: true }],
    ["server", "FAB-EHR-APP01", ["vcenter", "edge"], { os: "Windows Server 2022", vcpu: 8, memory_gb: 32, ip: "10.40.1.21", host: "FAB-HV-01" }],
    ["server", "FAB-EHR-APP02", ["vcenter", "edge"], { os: "Windows Server 2022", vcpu: 8, memory_gb: 32, ip: "10.40.1.22", host: "FAB-HV-02" }],
    ["server", "FAB-PORTAL-WEB01", ["vcenter", "edge"], { os: "Ubuntu 24.04", vcpu: 4, memory_gb: 16, ip: "10.40.3.11", host: "FAB-HV-02" }],
    ["server", "FAB-PACS-DB01", ["vcenter", "edge"], { os: "Windows Server 2019", vcpu: 8, memory_gb: 64, ip: "10.40.5.11", host: "FAB-HV-02" }],
    ["endpoint", "FAB-WS-ER-04", ["microsoft"], { os: "Windows 11", department: "Emergency", compliance: "compliant" }],
    ["cloud_resource", "fabrikam-portal-appgw", ["microsoft"], { type: "Application Gateway", region: "northeurope" }],
    ["hypervisor_cluster", "FAB-CL-01", ["vcenter"], { hosts: 2, ha: true }],
    ["hypervisor_host", "FAB-HV-01", ["vcenter"], { version: "ESXi 8.0 U3", cpu_pct: 38 }],
    ["hypervisor_host", "FAB-HV-02", ["vcenter"], { version: "ESXi 8.0 U3", cpu_pct: 44 }],
    ["network_device", "FAB-CORE-SW01", ["edge"], { model: "Aruba CX 8360", location: "Main hospital" }],
    ["datastore", "FAB-DS-01", ["vcenter"], { capacity_gb: 16384, free_gb: 6120, latency_ms: 2.2 }],
    ["backup_repository", "FAB-REPO-01", ["veeam"], { capacity_gb: 16384, free_gb: 1459, immutable: false }],
    ["storage_array", "FAB-SAN-01", ["vcenter"], { model: "NetApp AFF A250" }],
  ];
  const assets = build(defs, (i) => ago(3 + (i % 6)));
  const R = (s: string, rel: string, d: string): Relation => ({ src: s, rel, dst: d });
  const relations: Relation[] = [
    R("service:EHR", "DEPENDS_ON", "application:Epic Hyperspace"),
    R("service:EHR", "DEPENDS_ON", "database:EHR"),
    R("service:Patient portal", "DEPENDS_ON", "server:FAB-PORTAL-WEB01"),
    R("service:Patient portal", "DEPENDS_ON", "cloud_resource:fabrikam-portal-appgw"),
    R("service:Patient portal", "DEPENDS_ON", "service:EHR"),
    R("service:PACS imaging", "DEPENDS_ON", "database:PACSMeta"),
    R("application:Epic Hyperspace", "RUNS_ON", "server:FAB-EHR-APP01"),
    R("application:Epic Hyperspace", "RUNS_ON", "server:FAB-EHR-APP02"),
    R("database:EHR", "HOSTED_ON", "sql_instance:FAB-EHR-SQL01\\EHR"),
    R("database:PACSMeta", "HOSTED_ON", "server:FAB-PACS-DB01"),
    R("sql_instance:FAB-EHR-SQL01\\EHR", "RUNS_ON", "server:FAB-EHR-SQL01"),
    R("backup_job:EHR SQL nightly", "PROTECTS", "database:EHR"),
    R("backup_job:EHR SQL nightly", "WRITES_TO", "backup_repository:FAB-REPO-01"),
    R("backup_job:PACS weekly", "PROTECTS", "database:PACSMeta"),
    R("backup_job:PACS weekly", "WRITES_TO", "backup_repository:FAB-REPO-01"),
    R("hypervisor_host:FAB-HV-01", "MEMBER_OF", "hypervisor_cluster:FAB-CL-01"),
    R("hypervisor_host:FAB-HV-02", "MEMBER_OF", "hypervisor_cluster:FAB-CL-01"),
    R("hypervisor_host:FAB-HV-01", "MOUNTS", "datastore:FAB-DS-01"),
    R("hypervisor_host:FAB-HV-02", "MOUNTS", "datastore:FAB-DS-01"),
    R("datastore:FAB-DS-01", "BACKED_BY", "storage_array:FAB-SAN-01"),
    R("backup_repository:FAB-REPO-01", "BACKED_BY", "storage_array:FAB-SAN-01"),
    R("hypervisor_host:FAB-HV-01", "CONNECTS_TO", "network_device:FAB-CORE-SW01"),
  ];
  for (const a of assets) {
    if (a.kind === "server" && typeof a.attrs.host === "string") {
      relations.push(R(a.id, "RUNS_ON", `hypervisor_host:${a.attrs.host}`));
      relations.push(R(a.id, "STORED_ON", "datastore:FAB-DS-01"));
    }
  }
  const changes: EstateChange[] = [
    { id: 8201, at: ago(19), source: "veeam", asset_id: "backup_repository:FAB-REPO-01", asset_name: "FAB-REPO-01", change: "changed", before: { free_gb: 66 }, after: { free_gb: 1459 } },
    { id: 8202, at: ago(20), source: "veeam", asset_id: "server:FAB-OLD-LIS01", asset_name: "FAB-OLD-LIS01", change: "removed", before: { status: "decommissioned", restore_points_gb: 720 }, after: null },
    { id: 8203, at: ago(20), source: "veeam", asset_id: "server:FAB-OLD-LIS02", asset_name: "FAB-OLD-LIS02", change: "removed", before: { status: "decommissioned", restore_points_gb: 690 }, after: null },
    { id: 8204, at: ago(60 * 20), source: "microsoft", asset_id: "endpoint:FAB-WS-ER-04", asset_name: "FAB-WS-ER-04", change: "added", before: null, after: { os: "Windows 11", department: "Emergency" } },
  ];
  return { assets, relations, changes };
}
